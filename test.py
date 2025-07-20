import os
import json
import asyncio
import base64

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
nest_asyncio.apply()  # allow nested loops in Jupyter/non-main threads

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
CONFIG_FILE     = "config.json"
DEFAULT_CONFIG  = {
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

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def get_settings():
    cfg = load_config()
    return {
        "BUY_AMOUNT_SOL":     float(cfg.get("BUY_AMOUNT_SOL", DEFAULT_CONFIG["BUY_AMOUNT_SOL"])),
        "SLIPPAGE_PCT":       float(cfg.get("SLIPPAGE_PCT", DEFAULT_CONFIG["SLIPPAGE_PCT"])),
        "STOP_LOSS_PCT":      float(cfg.get("STOP_LOSS_PCT", DEFAULT_CONFIG["STOP_LOSS_PCT"])),
        "AUTO_SELL_ENABLED":  bool(cfg.get("AUTO_SELL_ENABLED", DEFAULT_CONFIG["AUTO_SELL_ENABLED"])),
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

wallet      = None
sol_client  = None
jup_client  = None

def setup_wallet(pk_str: str):
    try:
        w = Keypair.from_base58_string(pk_str)
        print(f"[✓] Wallet Public Key: {w.pubkey()}")
        return w
    except Exception as e:
        print(f"[x] Wallet setup error: {e}")
        return None

async def create_clients(wallet_keypair):
    sol = AsyncClient(RPC_URL)
    jup = Jupiter(sol, wallet_keypair)
    return sol, jup

# ─── SWAP LOGIC ──────────────────────────────────────────────────────────────
async def auto_buy(
    mint: str,
    wallet_keypair,
    sol,
    jup,
    buy_amount_override=None
):
    s = get_settings()
    if buy_amount_override is not None:
        s["BUY_AMOUNT_SOL"] = buy_amount_override

    lamports_in = int(s["BUY_AMOUNT_SOL"] * 1e9)
    slippage_bps = int(s["SLIPPAGE_PCT"] * 100)

    # 1) Quote routes so we can compute price
    quote = await jup.quote(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount=lamports_in,
        slippage_bps=slippage_bps,
    )
    # extract routes whether quote is dict or object
    if isinstance(quote, dict):
        routes = quote.get("routes") or quote.get("data", {}).get("routes", [])
    else:
        routes = getattr(quote, "routes", [])

    if not routes:
        return None, None

    lamports_out = routes[0].out_amount
    buy_price    = lamports_in / lamports_out

    # 2) Execute the swap
    swap_b64 = await jup.swap(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount=lamports_in,
        slippage_bps=slippage_bps,
    )
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    sig    = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
    txn    = VersionedTransaction.populate(raw_tx.message, [sig])
    resp   = await sol.send_raw_transaction(
        txn=bytes(txn),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed),
    )
    txid   = getattr(resp, "result", getattr(resp, "value", str(resp)))

    # 3) Schedule the time-based sell if enabled
    if s["AUTO_SELL_ENABLED"]:
        asyncio.create_task(
            schedule_sell(mint, lamports_out, wallet_keypair, sol, jup, s["SELL_AFTER_SECONDS"])
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
    print(f"[→] (AUTO-SELL) swapping {mint} back to WSOL…")

    # reverse swap logic
    s = get_settings()
    slippage_bps = int(s["SLIPPAGE_PCT"] * 100)
    swap_b64 = await jup.swap(
        input_mint=mint,
        output_mint=WSOL_MINT,
        amount=lamports_out,
        slippage_bps=slippage_bps,
    )
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    sig    = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
    txn    = VersionedTransaction.populate(raw_tx.message, [sig])
    resp   = await sol.send_raw_transaction(
        txn=bytes(txn),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed),
    )
    txid   = getattr(resp, "result", getattr(resp, "value", str(resp)))
    print(f"[✓] AUTO-SELL succeeded for {mint}: {txid}")

# ─── TELEGRAM BOT COMMANDS ──────────────────────────────────────────────────
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
        await update.message.reply_text(f"❌ Unknown key: {key}\nTry /set again.")
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

# ─── BUY CONVERSATION ───────────────────────────────────────────────────────
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

    mint = ctx.user_data["buy_mint"]
    await update.message.reply_text(f"🔄 Buying {amount} SOL of {mint}…")
    txid, price = await auto_buy(mint, wallet, sol_client, jup_client, buy_amount_override=amount)

    if txid:
        await update.message.reply_text(
            f"✅ Swap succeeded: `{txid}`\n"
            f"Price: {price:.6f} SOL per token",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Swap failed. Check logs.")

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

    print("[✓] Bot commands running…")
    app.run_polling()

if __name__ == "__main__":
    ensure_config()
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet:
        exit(1)
    sol_client, jup_client = asyncio.run(create_clients(wallet))
    run_bot()
