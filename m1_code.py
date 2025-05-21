import re
import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events
from solders.keypair import Keypair

# === load .env ===
load_dotenv()
API_ID             = int(os.getenv("API_ID"))
API_HASH           = os.getenv("API_HASH")
PHONE              = os.getenv("PHONE")
GROUP              = int(os.getenv("GROUP"))            # e.g. -1001993316422
BUY_AMOUNT_SOL     = float(os.getenv("BUY_AMOUNT_SOL"))
SLIPPAGE           = float(os.getenv("SLIPPAGE"))
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

# === wallet setup ===
def setup_wallet(pk_str: str):
    try:
        wallet = Keypair.from_base58_string(pk_str)
        print(f"[✓] Wallet Public Key: {wallet.pubkey()}")
        return wallet
    except Exception as e:
        print(f"[x] Wallet setup error: {e}")
        return None

# === parser ===
def extract_contract(text: str):
    m = re.search(r'(?:0x)?[A-Fa-f0-9]{32,44}', text)
    return m.group(0) if m else None

# === Telegram client (reuse session) ===
client = TelegramClient('session', API_ID, API_HASH)

@client.on(events.NewMessage(chats=GROUP))
async def live_handler(evt):
    text = evt.raw_text or "<no text>"
    print(f"\n🕒 Live: {text}")
    contract = extract_contract(text)
    if contract:
        print(f"[+] Signal: {contract}")
        print(f"[~] Buy {BUY_AMOUNT_SOL} SOL @ {SLIPPAGE}% slip")

async def fetch_last_messages(limit: int = 3):
    print(f"[*] Fetching last {limit} messages...")
    async for message in client.iter_messages(GROUP, limit=limit):
        text = message.raw_text or "<no text>"
        print(f"\n📜 Past: {text}")
        contract = extract_contract(text)
        if contract:
            print(f"[+] Past Signal: {contract}")

async def main():
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet:
        return

    print("[*] Connecting with existing session…")
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(PHONE)
        code = input("Enter Telegram code: ")
        await client.sign_in(PHONE, code)

    await fetch_last_messages(limit=3)
    print("[✓] Listening for live messages…")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
