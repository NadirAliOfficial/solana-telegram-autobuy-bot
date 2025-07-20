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

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "BUY_AMOUNT_SOL":    0.05,
    "SLIPPAGE_PCT":      10.0,
    "STOP_LOSS_PCT":     30.0,
    "AUTO_SELL_ENABLED": True,
    "AUTO_SELL_PCT":     10.0,    # percent profit target for auto-sell
    "POLL_INTERVAL":     15       # seconds between price checks
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
        "BUY_AMOUNT_SOL":    float(cfg.get("BUY_AMOUNT_SOL", DEFAULT_CONFIG["BUY_AMOUNT_SOL"])),
        "SLIPPAGE_PCT":      float(cfg.get("SLIPPAGE_PCT", DEFAULT_CONFIG["SLIPPAGE_PCT"])),
        "STOP_LOSS_PCT":     float(cfg.get("STOP_LOSS_PCT", DEFAULT_CONFIG["STOP_LOSS_PCT"])),
        "AUTO_SELL_ENABLED": bool(cfg.get("AUTO_SELL_ENABLED", DEFAULT_CONFIG["AUTO_SELL_ENABLED"])),
        "AUTO_SELL_PCT":     float(cfg.get("AUTO_SELL_PCT", DEFAULT_CONFIG["AUTO_SELL_PCT"])),
        "POLL_INTERVAL":     int(cfg.get("POLL_INTERVAL", DEFAULT_CONFIG["POLL_INTERVAL"]))
    }

# ─── ENV & CLIENT SETUP ─────────────────────────────────────────────────────
load_dotenv()
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
WSOL_MINT = os.getenv("WSOL_MINT", "So11111111111111111111111111111111111111112")

wallet = None
sol_client: AsyncClient = None
jup_client: Jupiter = None


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

# ─── SWAP & PRICE-MONITORING LOGIC ───────────────────────────────────────────
async def auto_buy(mint: str, wallet_keypair, sol, jup, purchase_amount=None, chat_id=None, bot=None):
    s = get_settings()
    amount_sol = purchase_amount if purchase_amount is not None else s["BUY_AMOUNT_SOL"]
    lamports_in = int(amount_sol * 1e9)
    slippage_bps = int(s["SLIPPAGE_PCT"] * 100)

    # fetch route to determine token amount
    routes = await jup.get_routes(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount=lamports_in,
        slippage_bps=slippage_bps
    )
    if not routes:
        await bot.send_message(chat_id, f"❌ No route for {mint}")
        return None
    route = routes[0]
    lamports_out = route.out_amount  # token lamports received

    # perform the swap
    swap_b64 = await jup.swap(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount=lamports_in,
        slippage_bps=slippage_bps
    )
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    sig = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
    txn = VersionedTransaction.populate(raw_tx.message, [sig])
    resp = await sol.send_raw_transaction(
        txn=bytes(txn),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)
    )
    txid_buy = getattr(resp, "result", getattr(resp, "value", str(resp)))
    await bot.send_message(chat_id, f"✅ Bought {amount_sol} SOL of `{mint}` → tx `{txid_buy}`", parse_mode="Markdown")

    # schedule profit/stop-loss monitor
    if s["AUTO_SELL_ENABLED"]:
        asyncio.create_task(
            monitor_and_sell(
                mint,
                lamports_out,
                lamports_in / lamports_out,  # SOL per token
                wallet_keypair,
                sol,
                jup,
                chat_id,
                bot
            )
        )
    return txid_buy

async def monitor_and_sell(
    mint: str,
    lamports_out: int,
    buy_price: float,
    wallet_keypair,
    sol,
    jup,
    chat_id,
    bot
):
    s = get_settings()
    target_price = buy_price * (1 + s["AUTO_SELL_PCT"] / 100)
    stop_price = buy_price * (1 - s["STOP_LOSS_PCT"] / 100)
    interval = s["POLL_INTERVAL"]

    while True:
        # fetch current price immediately
        routes = await jup.get_routes(
            input_mint=mint,
            output_mint=WSOL_MINT,
            amount=lamports_out,
            slippage_bps=int(s["SLIPPAGE_PCT"] * 100)
        )
        if not routes:
            await bot.send_message(chat_id, f"❌ Price fetch failed for {mint}")
        else:
            route = routes[0]
            current_price = route.out_amount / lamports_out  # SOL per token
            if current_price >= target_price or current_price <= stop_price:
                swap_b64 = await jup.swap(
                    input_mint=mint,
                    output_mint=WSOL_MINT,
                    amount=lamports_out,
                    slippage_bps=int(s["SLIPPAGE_PCT"] * 100)
                )
                raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
                sig = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
                txn = VersionedTransaction.populate(raw_tx.message, [sig])
                resp = await sol.send_raw_transaction(
                    txn=bytes(txn),
                    opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)
                )
                txid_sell = getattr(resp, "result", getattr(resp, "value", str(resp)))
                reason = "profit target" if current_price >= target_price else "stop loss"
                await bot.send_message(
                    chat_id,
                    f"🔄 Auto-sell ({reason}) for `{mint}` → tx `{txid_sell}`",
                    parse_mode="Markdown"
                )
                break
        # pause before next check
        await asyncio.sleep(interval)

# ─── TELEGRAM BOT SETUP ─────────────────────────────────────────────────────
ST_KEY, ST_VAL = range(2)
BT_MINT, BT_AMT = range(2)

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot ready!\n/get – show settings\n/set – change settings\n/buy – manual buy"
    )

async def get_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    text = "\n".join(f"{k}: {v}" for k, v in cfg.items())
    await update.message.reply_text(f"Settings:\n{text}")

async def set_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keys = ", ".join(DEFAULT_CONFIG.keys())
    await update.message.reply_text(f"Which setting? ({keys})")
    return ST_KEY

async def set_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    cfg = load_config()
    if key not in cfg:
        await update.message.reply_text("Unknown setting.")
        return ConversationHandler.END
    ctx.user_data['set_key'] = key
    await update.message.reply_text(f"Enter new value for {key}:")
    return ST_VAL

async def set_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = ctx.user_data['set_key']
    val = update.message.text.strip()
    cfg = load_config()
    try:
        cfg[key] = float(val) if '.' in val or val.isdigit() else val
        save_config(cfg)
        await update.message.reply_text(f"{key} = {cfg[key]}")
    except:
        await update.message.reply_text("Invalid value.")
    return ConversationHandler.END

async def buy_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter mint address:")
    return BT_MINT

async def buy_mint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['mint'] = update.message.text.strip()
    await update.message.reply_text("Enter SOL amount to spend:")
    return BT_AMT

async def buy_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip())
    except:
        await update.message.reply_text("Invalid amount.")
        return BT_AMT
    chat_id = update.effective_chat.id
    mint = ctx.user_data['mint']
    await update.message.reply_text(f"Buying {amt} SOL of {mint}...")
    await auto_buy(mint, wallet, sol_client, jup_client, purchase_amount=amt, chat_id=chat_id, bot=ctx.bot)
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def run_bot():
    conv_set = ConversationHandler(
        entry_points=[CommandHandler('set', set_start)],
        states={ST_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_key)],
                ST_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_val)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    conv_buy = ConversationHandler(
        entry_points=[CommandHandler('buy', buy_start)],
        states={BT_MINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_mint)],
                BT_AMT : [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amt)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start_cmd))
    app.add_handler(CommandHandler('get',   get_cmd))
    app.add_handler(conv_set)
    app.add_handler(conv_buy)
    app.run_polling()

if __name__ == '__main__':
    ensure_config()
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet:
        exit(1)
    sol_client, jup_client = asyncio.run(create_clients(wallet))
    run_bot()
