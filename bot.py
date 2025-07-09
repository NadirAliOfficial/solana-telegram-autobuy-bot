import os
import re
import json
import asyncio
import base64
import threading
import nest_asyncio
nest_asyncio.apply()  # allow nested event loops in Jupyter
from dotenv import load_dotenv
from telethon import TelegramClient, events
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
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─── CONFIG FILE HELPERS ─────────────────────────────────────────────────────
CONFIG_FILE     = "config.json"
DEFAULT_CONFIG  = {
    "BUY_AMOUNT_SOL":     0.05,
    "SLIPPAGE_PCT":       10.0,
    "STOP_LOSS_PCT":      30.0,
    "AUTO_SELL_ENABLED":  True,
    "SELL_AFTER_SECONDS": 180
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
        "BUY_AMOUNT_SOL":     float(cfg["BUY_AMOUNT_SOL"]),
        "SLIPPAGE_PCT":       float(cfg["SLIPPAGE_PCT"]),
        "STOP_LOSS_PCT":      float(cfg["STOP_LOSS_PCT"]),
        "AUTO_SELL_ENABLED":  bool(cfg["AUTO_SELL_ENABLED"]),
        "SELL_AFTER_SECONDS": int(cfg["SELL_AFTER_SECONDS"]),
    }

# ─── LOAD ENV & CONSTANTS ────────────────────────────────────────────────────
load_dotenv()  # only WALLET_PRIVATE_KEY lives here
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

API_ID    = 26820360
API_HASH  = "79c18a74d33d25d2d18ca9cf8000e4f6"
PHONE     = "+41796129161"
GROUP     = -1001993316422
BOT_TOKEN = "7655208071:AAFYp08k9F8a7Tb3Z0GXp5mhQa48hAyr53s"
RPC_URL   = "https://api.mainnet-beta.solana.com"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# ─── WALLET & CLIENT SETUP ───────────────────────────────────────────────────
def setup_wallet(pk_str: str):
    try:
        w = Keypair.from_base58_string(pk_str)
        print(f"[✓] Wallet Public Key: {w.pubkey()}")
        return w
    except Exception as e:
        print(f"[x] Wallet setup error: {e}")
        return None

async def create_clients(wallet):
    sol = AsyncClient(RPC_URL)
    jup = Jupiter(sol, wallet)
    return sol, jup

# ─── MINT EXTRACTION ─────────────────────────────────────────────────────────
def extract_mint(text: str):
    matches = re.findall(r"[1-9A-HJ-NP-Za-km-z]{43,44}", text)
    return max(matches, key=len) if matches else None

# ─── AUTO-BUY & AUTO-SELL PLACEHOLDER ────────────────────────────────────────
async def auto_buy(mint: str, wallet, sol, jup):
    s = get_settings()
    print(f"\n[→] Attempting swap for {mint} …")
    try:
        swap_b64 = await jup.swap(
            input_mint=WSOL_MINT,
            output_mint=mint,
            amount=int(s["BUY_AMOUNT_SOL"] * 1e9),
            slippage_bps=int(s["SLIPPAGE_PCT"] * 100),
        )
    except Exception as e:
        if "not tradable" in str(e):
            print(f"[!] {mint} not tradable — skipping.")
            return False
        print(f"[x] Jupiter error for {mint}: {e}")
        return False

    raw = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    sig = wallet.sign_message(message.to_bytes_versioned(raw.message))
    txn = VersionedTransaction.populate(raw.message, [sig])

    print(f"[→] Sending transaction for {mint} …")
    resp = await sol.send_raw_transaction(
        txn=bytes(txn),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed),
    )
    txid = getattr(resp, "result", getattr(resp, "value", str(resp)))
    print(f"[✓] Swap succeeded for {mint}: {txid}")

    if s["AUTO_SELL_ENABLED"]:
        asyncio.create_task(
            schedule_sell(mint, wallet, sol, jup, s["SELL_AFTER_SECONDS"])
        )
    return True

async def schedule_sell(mint, wallet, sol, jup, delay):
    await asyncio.sleep(delay)
    print(f"[→] (AUTO-SELL) swapping {mint} back to WSOL…")
    # TODO: implement reverse-swap logic here
    print(f"[!] AUTO-SELL for {mint} is placeholder — add your logic.")

# ─── TELETHON LISTENER ────────────────────────────────────────────────────────
user_client = TelegramClient("session", API_ID, API_HASH)

@user_client.on(events.NewMessage(chats=GROUP))
async def live_listener(evt):
    mint = extract_mint(evt.raw_text or "")
    if mint:
        await auto_buy(mint, wallet, sol_client, jup_client)

async def run_listener():
    global wallet, sol_client, jup_client
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet:
        return
    sol_client, jup_client = await create_clients(wallet)

    await user_client.connect()
    if not await user_client.is_user_authorized():
        await user_client.send_code_request(PHONE)
        code = input("Enter Telegram code for user session: ")
        await user_client.sign_in(PHONE, code)

    print("[✓] Listening for live mints…")
    await user_client.run_until_disconnected()

# ─── TELEGRAM-BOT COMMANDS ───────────────────────────────────────────────────
CHOOSING_KEY, TYPING_VALUE = range(2)

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "MemeMachine Bot ready!\n"
        "/get – show settings\n"
        "/set – change a setting"
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
        if val.lower() in ("true","false"):
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

def run_bot():
    # create and attach a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_start)],
        states={
            CHOOSING_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_key)],
            TYPING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_value)],
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
    app.add_handler(conv)

    print("[✓] Bot commands running…")
    # disable signal handlers when running in a non-main thread
    app.run_polling(stop_signals=())


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_config()
    # 1) start bot commands in thread
    threading.Thread(target=run_bot, daemon=True).start()
    # 2) start Telethon listener
    asyncio.run(run_listener())
