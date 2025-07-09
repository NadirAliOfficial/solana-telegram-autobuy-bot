import re
import os
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
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === Load config from .env ===
load_dotenv()  # Loads WALLET_PRIVATE_KEY from .env

# Hardcoded values
API_ID = 26820360
API_HASH = "79c18a74d33d25d2d18ca9cf8000e4f6"
PHONE = "+41796129161"
GROUP = -1001993316422
BOT_TOKEN = "7655208071:AAFYp08k9F8a7Tb3Z0GXp5mhQa48hAyr53s"
BUY_AMOUNT_SOL = 0.05
SLIPPAGE_PCT = 10.0
RPC_URL = "https://api.mainnet-beta.solana.com"

# Private key from .env
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

# Constants
WSOL_MINT    = "So11111111111111111111111111111111111111112"
SLIPPAGE_BPS = int(SLIPPAGE_PCT * 100)

# === Wallet setup ===
def setup_wallet(pk_str: str):
    try:
        wallet = Keypair.from_base58_string(pk_str)
        print(f"[✓] Wallet Public Key: {wallet.pubkey()}")
        return wallet
    except Exception as e:
        print(f"[x] Wallet setup error: {e}")
        return None

# === Create Solana & Jupiter clients ===
async def create_clients(wallet):
    sol_client = AsyncClient(RPC_URL)
    jup_client = Jupiter(sol_client, wallet)
    return sol_client, jup_client

# === Mint extractor ===
def extract_mint(text: str):
    matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{43,44}', text)
    return max(matches, key=len) if matches else None

# === Auto-buy via Jupiter SDK ===
async def auto_buy(mint: str, wallet, sol_client, jup_client):
    print(f"\n[→] Attempting swap for {mint} …")
    try:
        swap_b64 = await jup_client.swap(
            input_mint=WSOL_MINT,
            output_mint=mint,
            amount=int(BUY_AMOUNT_SOL * 1e9),
            slippage_bps=SLIPPAGE_BPS
        )
    except Exception as e:
        if "not tradable" in str(e):
            print(f"[!] {mint} not tradable — skipping.")
            return False
        print(f"[x] Jupiter error for {mint}: {e}")
        return False

    # Deserialize & sign
    raw_tx    = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    sig       = wallet.sign_message(message.to_bytes_versioned(raw_tx.message))
    signed_tx = VersionedTransaction.populate(raw_tx.message, [sig])

    # Send & confirm
    print(f"[→] Sending transaction for {mint} …")
    resp = await sol_client.send_raw_transaction(
        txn=bytes(signed_tx),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)
    )
    txid = getattr(resp, 'result', getattr(resp, 'value', str(resp)))
    print(f"[✓] Swap succeeded for {mint}: {txid}")
    return True

# === Telethon listener ===
user_client = TelegramClient('session', API_ID, API_HASH)

@user_client.on(events.NewMessage(chats=GROUP))
async def live_listener(evt):
    text = evt.raw_text or ""
    mint = extract_mint(text)
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

    print("[✓] Listening for live mints in Quartz [SOL]…")
    await user_client.run_until_disconnected()

# === Bot commands (python-telegram-bot v20+) ===
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to MemeMachine Bot!\n"
        "/status – show current settings\n"
        "/trade <mint_address> – manually trigger a trade"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🔧 Settings:\n"
        f"- Listening to GROUP ID: {GROUP}\n"
        f"- Wallet: {wallet.pubkey()}\n"
        f"- Buy amount: {BUY_AMOUNT_SOL} SOL\n"
        f"- Slippage: {SLIPPAGE_PCT}%"
    )

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /trade <mint_address>")
    mint = context.args[0].strip()
    if not extract_mint(mint):
        return await update.message.reply_text("❌ Invalid mint address.")
    await update.message.reply_text(f"🚀 Trading {mint} …")
    success = await auto_buy(mint, wallet, sol_client, jup_client)
    await update.message.reply_text("✅ Trade successful!" if success else "❌ Trade failed/skipped.")

def run_bot():
    # Create and set a dedicated event loop for this thread
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("trade",  trade_cmd))

    print("[✓] Bot commands running…")
    # Disable signal handling in this thread
    app.run_polling(stop_signals=())

# === Entry point ===
if __name__ == "__main__":
    # 1) Start Telegram bot in its own thread
    threading.Thread(target=run_bot, daemon=True).start()
    # 2) Run Telethon listener in main asyncio loop
    asyncio.run(run_listener())
