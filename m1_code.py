import os
import re
import json
import asyncio
import base64
import threading

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
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── CONFIG HELPERS ───────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

def ensure_config():
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(f"Missing {CONFIG_FILE}. Please create it with your settings.")

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def get_settings():
    c = load_config()
    return {
        "BUY_AMOUNT_SOL":     float(c["BUY_AMOUNT_SOL"]),
        "SLIPPAGE_PCT_BPS":   int(c["SLIPPAGE_PCT"] * 100),
        "AUTO_SELL":          bool(c["AUTO_SELL_ENABLED"]),
        "SELL_TIERS":         c["SELL_TIERS"],
        "TRAILING_DROP":      float(c["TRAILING_STOP_DROP_PCT"]),
    }

# ─── ENV & CONSTANTS ───────────────────────────────────────────────────────────
load_dotenv()
WALLET_KEY    = os.getenv("WALLET_PRIVATE_KEY", "").strip()
API_ID        = 26820360
API_HASH      = "79c18a74d33d25d2d18ca9cf8000e4f6"
PHONE         = "+41796129161"
GROUP_ID      = -1001993316422  # Telegram Bot API style
BOT_TOKEN     = "7655208071:AAFYp08k9F8a7Tb3Z0GXp5mhQa48hAyr53s"
RPC_URL       = "https://api.mainnet-beta.solana.com"
WSOL_MINT     = "So11111111111111111111111111111111111111112"

# ─── WALLET & CLIENT SETUP ────────────────────────────────────────────────────
def setup_wallet(key_b58):
    w = Keypair.from_base58_string(key_b58)
    print(f"[✓] Wallet Public Key: {w.pubkey()}")
    return w

async def create_clients(wallet):
    sol = AsyncClient(RPC_URL)
    jup = Jupiter(sol, wallet)
    return sol, jup

# ─── UTILS ─────────────────────────────────────────────────────────────────────
def extract_mint(text):
    ms = re.findall(r"[1-9A-HJ-NP-Za-km-z]{43,44}", text)
    return max(ms, key=len) if ms else None

# ─── AUTO-BUY & TIERED AUTO-SELL ───────────────────────────────────────────────
async def auto_buy(mint, wallet, sol, jup):
    s = get_settings()
    print(f"\n[→] Buying {mint}…")
    try:
        b64 = await jup.swap(
            input_mint=WSOL_MINT,
            output_mint=mint,
            amount=int(s["BUY_AMOUNT_SOL"] * 1e9),
            slippage_bps=s["SLIPPAGE_PCT_BPS"],
        )
    except Exception as e:
        print(f"[!] Swap error: {e}")
        return

    raw = VersionedTransaction.from_bytes(base64.b64decode(b64))
    sig = wallet.sign_message(message.to_bytes_versioned(raw.message))
    signed = VersionedTransaction.populate(raw.message, [sig])
    resp = await sol.send_raw_transaction(
        txn=bytes(signed),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed),
    )
    txid = getattr(resp, "result", getattr(resp, "value", str(resp)))
    print(f"[✓] Bought {mint}: {txid}")

    if s["AUTO_SELL"]:
        purchase_price = None  # TODO: fetch actual price
        asyncio.create_task(
            run_sell_strategy(mint, wallet, sol, jup, purchase_price, s)
        )

async def run_sell_strategy(mint, wallet, sol, jup, purchase_price, settings):
    tiers = settings["SELL_TIERS"]
    drop = settings["TRAILING_DROP"]
    remaining = 1.0
    peak = purchase_price

    # Tiered sells
    for tier in tiers:
        target = purchase_price * (1 + tier["profit_pct"] / 100)
        while True:
            await asyncio.sleep(5)
            current = None  # TODO: fetch real-time price
            if current and current >= target:
                amt = remaining * tier["sell_pct"] / 100
                print(f"[→] Tier {tier['profit_pct']}% reached: sell {amt*100:.1f}%")
                # TODO: reverse-swap amt
                remaining -= amt
                peak = current
                break

    # Trailing stop
    while remaining > 0:
        await asyncio.sleep(5)
        current = None  # TODO: fetch real-time price
        if current and current > peak:
            peak = current
        elif current and current <= peak * (1 - drop / 100):
            print(f"[→] Trailing drop {drop}%: sell remaining")
            # TODO: reverse-swap remaining
            break

# ─── TELETHON STARTUP SCAN & LIVE LISTENER ────────────────────────────────────
user_client = TelegramClient("session", API_ID, API_HASH)

async def scan_recent(n, entity):
    msgs = await user_client.get_messages(entity, limit=n)
    for m in reversed(msgs):
        mint = extract_mint(m.raw_text or "")
        if mint:
            print(f"[→] Startup mint: {mint}")
            await auto_buy(mint, wallet, sol_client, jup_client)

async def run_listener():
    global wallet, sol_client, jup_client
    wallet = setup_wallet(WALLET_KEY)
    sol_client, jup_client = await create_clients(wallet)

    await user_client.connect()
    if not await user_client.is_user_authorized():
        await user_client.send_code_request(PHONE)
        code = input("Enter Telegram code: ")
        await user_client.sign_in(PHONE, code)

    # derive raw channel id from -100xxx
    raw_id = int(str(GROUP_ID)[4:])
    dialogs = await user_client.get_dialogs()
    entity = next(
        (d.entity for d in dialogs if getattr(d.entity, "id", None) == raw_id),
        None
    )
    if not entity:
        print(f"[x] Could not find dialog with raw id {raw_id}")
        return

    print("[✓] Scanning last 2 messages…")
    await scan_recent(2, entity)

    print("[✓] Listening for new mints…")
    @user_client.on(events.NewMessage(chats=entity))
    async def handler(evt):
        mint = extract_mint(evt.raw_text or "")
        if mint:
            print(f"[→] New mint: {mint}")
            await auto_buy(mint, wallet, sol_client, jup_client)

    await user_client.run_until_disconnected()

# ─── TELEGRAM-BOT COMMANDS ────────────────────────────────────────────────────
CHOOSING, TYPING = range(2)

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot ready! Use /get and /set.")

async def get_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = load_config()
    lines = [f"{k}: {v}" for k, v in c.items()]
    await update.message.reply_text("⚙️ Current settings:\n" + "\n".join(lines))

async def set_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keys = ", ".join(load_config().keys())
    await update.message.reply_text(f"Which setting? ({keys})")
    return CHOOSING

async def choose_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    c = load_config()
    if key not in c:
        await update.message.reply_text("❌ Unknown key—send /set again.")
        return ConversationHandler.END
    ctx.user_data["key"] = key
    await update.message.reply_text(f"Enter new value for `{key}`:")
    return TYPING

async def receive_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key, val = ctx.user_data["key"], update.message.text.strip()
    c = load_config()
    try:
        c[key] = (val.lower() in ("true","false") and val.lower()=="true") or float(val)
        save_config(c)
        await update.message.reply_text(f"✅ `{key}` set to `{c[key]}`")
    except:
        await update.message.reply_text("❌ Invalid value.")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_start)],
        states={
            CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_key)],
            TYPING:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_value)],
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
    app.run_polling(stop_signals=())

# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_config()
    threading.Thread(target=run_bot, daemon=True).start()
    asyncio.run(run_listener())
