import re, os, asyncio, base64, threading
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
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from settings import load_config, save_config

# ─── Load .env ────────────────────────────────────────────────────────────────
load_dotenv()  # Only WALLET_PRIVATE_KEY lives here
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

# ─── Hard-coded Telegram/Solana creds ─────────────────────────────────────────
API_ID    = 26820360
API_HASH  = "79c18a74d33d25d2d18ca9cf8000e4f6"
PHONE     = "+41796129161"
GROUP     = -1001993316422
BOT_TOKEN = "7655208071:AAFYp08k9F8a7Tb3Z0GXp5mhQa48hAyr53s"
RPC_URL   = "https://api.mainnet-beta.solana.com"

# ─── Reloadable settings ──────────────────────────────────────────────────────
def get_settings():
    cfg = load_config()
    return {
        "BUY_AMOUNT_SOL":    float(cfg.get("BUY_AMOUNT_SOL", 0.05)),
        "SLIPPAGE_PCT":      float(cfg.get("SLIPPAGE_PCT", 10.0)),
        "STOP_LOSS_PCT":     float(cfg.get("STOP_LOSS_PCT", 30.0)),
        "AUTO_SELL_ENABLED": bool(cfg.get("AUTO_SELL_ENABLED", True)),
        "SELL_AFTER_SECONDS": int(cfg.get("SELL_AFTER_SECONDS", 180)),
    }

# ─── Wallet & Clients ─────────────────────────────────────────────────────────
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

# ─── Mint extractor ───────────────────────────────────────────────────────────
def extract_mint(text: str):
    matches = re.findall(r"[1-9A-HJ-NP-Za-km-z]{43,44}", text)
    return max(matches, key=len) if matches else None

# ─── Auto-buy & (placeholder) auto-sell ────────────────────────────────────────
async def auto_buy(mint: str, wallet, sol, jup):
    s = get_settings()
    print(f"\n[→] Attempting swap for {mint} …")
    try:
        swap_b64 = await jup.swap(
            input_mint="So11111111111111111111111111111111111111112",  # WSOL
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

    # sign & send
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

    # ─── Schedule an auto-sell after X seconds (placeholder) ───────────────────
    if s["AUTO_SELL_ENABLED"]:
        asyncio.create_task(schedule_sell(mint, wallet, sol, jup, s["SELL_AFTER_SECONDS"]))

    return True

async def schedule_sell(mint, wallet, sol, jup, delay):
    await asyncio.sleep(delay)
    print(f"[→] (AUTO-SELL) swapping {mint} back to WSOL…")
    # TODO: implement reverse swap logic here
    # e.g.: await jup.swap(input_mint=mint, output_mint=WSOL, amount=…)
    print(f"[!] AUTO-SELL for {mint} is a placeholder — implement your logic.")

# ─── Telethon listener ────────────────────────────────────────────────────────
user_client = TelegramClient("session", API_ID, API_HASH)

@user_client.on(events.NewMessage(chats=GROUP))
async def live_listener(evt):
    mint = extract_mint(evt.raw_text or "")
    if mint:
        await auto_buy(mint, wallet, sol_client, jup_client)

async def run_listener():
    global wallet, sol_client, jup_client
    wallet, sol_client, jup_client = None, None, None

    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet: return
    sol_client, jup_client = await create_clients(wallet)

    await user_client.connect()
    if not await user_client.is_user_authorized():
        await user_client.send_code_request(PHONE)
        code = input("Enter Telegram code for user session: ")
        await user_client.sign_in(PHONE, code)

    print("[✓] Listening for live mints…")
    await user_client.run_until_disconnected()

# ─── Telegram-Bot Command Handlers ────────────────────────────────────────────
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "MemeMachine Bot ready!\n"
        "/get – show settings\n"
        "/set <key> <value> – update setting"
    )

async def get_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    txt = "\n".join(f"{k}: {v}" for k, v in cfg.items())
    await update.message.reply_text(f"⚙️ Current settings:\n{txt}")

async def set_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /set <key> <value>")
    key, val = ctx.args
    cfg = load_config()
    if key not in cfg:
        return await update.message.reply_text(f"❌ Unknown key: {key}")
    try:
        # bool or float
        if val.lower() in ("true","false"):
            cfg[key] = val.lower()=="true"
        else:
            cfg[key] = float(val)
        save_config(cfg)
        await update.message.reply_text(f"✅ `{key}` updated to `{cfg[key]}`")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

def run_bot():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("get",   get_cmd))
    app.add_handler(CommandHandler("set",   set_cmd))
    print("[✓] Bot commands running…")
    app.run_polling(stop_signals=())

# ─── Entry Point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1) Telegram-Bot commands in a thread
    threading.Thread(target=run_bot, daemon=True).start()
    # 2) Telethon listener
    asyncio.run(run_listener())
