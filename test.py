import os
import json
import asyncio
import base64
import logging

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed
from jupiter_python_sdk.jupiter import Jupiter

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import nest_asyncio
nest_asyncio.apply()  # allow nested event loops in Jupyter

# ─── LOGGING SETUP ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
CONFIG_FILE    = "config.json"
DEFAULT_CONFIG = {
    "BUY_AMOUNT_SOL":     0.05,
    "SLIPPAGE_PCT":       10.0,
    "STOP_LOSS_PCT":      30.0,
    "AUTO_SELL_ENABLED":  True,
    "SELL_AFTER_SECONDS": 180,
}

def ensure_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        logger.info("Created default config.json")

def load_config():
    return json.load(open(CONFIG_FILE, "r"))

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    logger.info("Config saved")

def get_settings():
    cfg = load_config()
    return {
        "BUY_AMOUNT_SOL":     float(cfg.get("BUY_AMOUNT_SOL",     DEFAULT_CONFIG["BUY_AMOUNT_SOL"])),
        "SLIPPAGE_PCT":       float(cfg.get("SLIPPAGE_PCT",       DEFAULT_CONFIG["SLIPPAGE_PCT"])),
        "STOP_LOSS_PCT":      float(cfg.get("STOP_LOSS_PCT",      DEFAULT_CONFIG["STOP_LOSS_PCT"])),
        "AUTO_SELL_ENABLED":  bool(cfg.get("AUTO_SELL_ENABLED",  DEFAULT_CONFIG["AUTO_SELL_ENABLED"])),
        "SELL_AFTER_SECONDS": int(cfg.get("SELL_AFTER_SECONDS", DEFAULT_CONFIG["SELL_AFTER_SECONDS"])),
    }

# ─── ENV & CLIENT SETUP ─────────────────────────────────────────────────────
load_dotenv()
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")
BOT_TOKEN          = os.getenv("BOT_TOKEN")
RPC_URL            = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
WSOL_MINT          = os.getenv(
    "WSOL_MINT",
    "So11111111111111111111111111111111111111112"
)

wallet     = None
sol_client = None
jup_client = None

def setup_wallet(pk_str: str):
    try:
        w = Keypair.from_base58_string(pk_str)
        logger.info(f"Wallet Public Key: {w.pubkey()}")
        return w
    except Exception as e:
        logger.error(f"Wallet setup error: {e}")
        return None

async def create_clients(wallet_keypair):
    sol = AsyncClient(RPC_URL)
    jup = Jupiter(sol, wallet_keypair)
    logger.info("Solana & Jupiter clients created")
    return sol, jup

# ─── SWAP LOGIC ──────────────────────────────────────────────────────────────
async def auto_buy(
    mint: str,
    wallet_keypair,
    sol,
    jup,
    purchase_amount: float = None,
    chat_id: int = None,
    bot=None
):
    s = get_settings()
    if purchase_amount is not None:
        s["BUY_AMOUNT_SOL"] = purchase_amount

    lamports_in  = int(s["BUY_AMOUNT_SOL"] * 1e9)
    slippage_bps = int(s["SLIPPAGE_PCT"] * 100)

    logger.info(f"Quoting route for {mint}: {lamports_in=} lamports, {slippage_bps=} bps")
    # 1) Fetch a quote
    try:
        quote = await jup.quote(
            input_mint   = WSOL_MINT,
            output_mint  = mint,
            amount       = lamports_in,
            slippage_bps = slippage_bps,
        )
    except Exception as e:
        logger.error(f"Quote fetch failed for {mint}: {e}")
        await bot.send_message(chat_id, f"❌ Error fetching quote for `{mint}`:\n`{e}`")
        return None, None

    # extract routes (handles dict or object)
    if isinstance(quote, dict):
        routes = quote.get("routes") or quote.get("data", {}).get("routes", [])
    else:
        routes = getattr(quote, "routes", [])

    if not routes:
        logger.warning(f"No routing path found for {mint}")
        await bot.send_message(chat_id, f"❌ No trading route found for `{mint}`.")
        return None, None

    lamports_out = routes[0].out_amount
    buy_price    = lamports_in / lamports_out
    logger.info(f"{mint} price quoted: {buy_price:.6f} SOL/token")

    # 2) Execute swap
    try:
        swap_b64 = await jup.swap(
            input_mint   = WSOL_MINT,
            output_mint  = mint,
            amount       = lamports_in,
            slippage_bps = slippage_bps,
        )
    except Exception as e:
        logger.error(f"Jupiter swap error for {mint}: {e}")
        await bot.send_message(chat_id, f"❌ Swap failed for `{mint}`:\n`{e}`")
        return None, None

    # decode, sign, send
    try:
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
        sig    = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
        txn    = VersionedTransaction.populate(raw_tx.message, [sig])
        resp   = await sol.send_raw_transaction(
            txn=bytes(txn),
            opts=TxOpts(skip_preflight=True, preflight_commitment=Processed),
        )
        txid = getattr(resp, "result", getattr(resp, "value", str(resp)))
        logger.info(f"Swap succeeded for {mint}: {txid}")
        await bot.send_message(
            chat_id,
            f"✅ Bought {s['BUY_AMOUNT_SOL']} SOL of `{mint}` @ {buy_price:.6f} SOL/token → tx `{txid}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Transaction send error for {mint}: {e}")
        await bot.send_message(chat_id, f"❌ Failed to send transaction:\n`{e}`")
        return None, None

    # schedule auto-sell if enabled
    if s["AUTO_SELL_ENABLED"]:
        logger.info(f"Scheduling auto-sell in {s['SELL_AFTER_SECONDS']}s for {mint}")
        asyncio.create_task(
            schedule_sell(
                mint,
                lamports_out,
                wallet_keypair,
                sol,
                jup,
                s["SELL_AFTER_SECONDS"],
            )
        )

    return txid, buy_price

async def schedule_sell(
    mint: str,
    lamports_out: int,
    wallet_keypair,
    sol,
    jup,
    delay: int
):
    await asyncio.sleep(delay)
    logger.info(f"Auto-sell triggered for {mint}, amount: {lamports_out} lamports")
    s = get_settings()
    slippage_bps = int(s["SLIPPAGE_PCT"] * 100)
    try:
        swap_b64 = await jup.swap(
            input_mint   = mint,
            output_mint  = WSOL_MINT,
            amount       = lamports_out,
            slippage_bps = slippage_bps,
        )
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
        sig    = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
        txn    = VersionedTransaction.populate(raw_tx.message, [sig])
        resp   = await sol.send_raw_transaction(
            txn=bytes(txn),
            opts=TxOpts(skip_preflight=True, preflight_commitment=Processed),
        )
        txid = getattr(resp, "result", getattr(resp, "value", str(resp)))
        logger.info(f"AUTO-SELL succeeded for {mint}: {txid}")
    except Exception as e:
        logger.error(f"AUTO-SELL failed for {mint}: {e}")

# ─── TELEGRAM BOT HANDLERS ───────────────────────────────────────────────────
CHOOSING_KEY, TYPING_VALUE = range(2)
BUY_MINT, BUY_AMOUNT       = range(2)

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "MemeMachine Bot ready!\n"
        "/get – show settings\n"
        "/set – change a setting\n"
        "/buy – initiate manual buy"
    )

async def get_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    txt = "\n".join(f"{k}: {v}" for k, v in cfg.items())
    await update.message.reply_text(f"⚙️ Current settings:\n{txt}")

async def set_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keys = ", ".join(DEFAULT_CONFIG.keys())
    await update.message.reply_text(f"Which setting? ({keys})")
    return CHOOSING_KEY

async def choose_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    cfg = load_config()
    if key not in cfg:
        await update.message.reply_text("❌ Unknown setting.")
        return ConversationHandler.END
    ctx.user_data["key"] = key
    await update.message.reply_text(f"Enter new value for `{key}`:")
    return TYPING_VALUE

async def receive_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = ctx.user_data["key"]
    val = update.message.text.strip()
    cfg = load_config()
    try:
        if val.lower() in ("true", "false"):
            cfg[key] = val.lower() == "true"
        else:
            cfg[key] = float(val)
        save_config(cfg)
        await update.message.reply_text(f"✅ `{key}` set to `{cfg[key]}`")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def buy_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter the mint address you want to buy:")
    return BUY_MINT

async def buy_mint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["buy_mint"] = update.message.text.strip()
    await update.message.reply_text("Now enter the amount in SOL to spend (e.g. 0.05):")
    return BUY_AMOUNT

async def buy_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Invalid number. Try again:")
        return BUY_AMOUNT

    mint   = ctx.user_data["buy_mint"]
    chat_id= update.effective_chat.id
    await update.message.reply_text(f"🔄 Buying {amount} SOL of {mint}…")
    txid, price = await auto_buy(
        mint,
        wallet,
        sol_client,
        jup_client,
        purchase_amount=amount,
        chat_id=chat_id,
        bot=ctx.bot
    )

    if txid:
        await update.message.reply_text(
            f"✅ Swap succeeded: `{txid}`\n"
            f"Price: {price:.6f} SOL per token",
            parse_mode="Markdown"
        )
    # errors are already reported in auto_buy
    return ConversationHandler.END

def run_bot():
    conv_set = ConversationHandler(
        entry_points=[CommandHandler("set", set_start)],
        states={
            CHOOSING_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_key)],
            TYPING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    conv_buy = ConversationHandler(
        entry_points=[CommandHandler("buy", buy_start)],
        states={
            BUY_MINT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_mint)],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("get",   get_cmd))
    app.add_handler(conv_set)
    app.add_handler(conv_buy)

    logger.info("Bot commands running…")
    app.run_polling()

if __name__ == "__main__":
    ensure_config()
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet:
        logger.error("Wallet initialization failed. Exiting.")
        exit(1)
    sol_client, jup_client = asyncio.run(create_clients(wallet))
    run_bot()
