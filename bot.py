import re
import base58
import asyncio
import os
from telethon.sync import TelegramClient, events
from solders.keypair import Keypair
from dotenv import load_dotenv

# === LOAD CONFIG FROM .env ===
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")
GROUP = os.getenv("GROUP")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL"))
SLIPPAGE = float(os.getenv("SLIPPAGE"))
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

# === WALLET SETUP ===
def setup_wallet(private_key_str):
    try:
        private_key = base58.b58decode(private_key_str)
        wallet = Keypair.from_secret_key(private_key)
        print(f"[✓] Wallet Public Key: {wallet.public_key}")
        return wallet
    except Exception as e:
        print(f"[x] Wallet setup error: {e}")
        return None

# === SIGNAL PARSER ===
def extract_contract_address(text):
    match = re.search(r'(?:0x)?[a-fA-F0-9]{32,44}', text)
    return match.group(0) if match else None

# === MAIN LOGIC ===
client = TelegramClient('session', API_ID, API_HASH)

@client.on(events.NewMessage(chats=GROUP))
async def handler(event):
    message = event.raw_text
    contract = extract_contract_address(message)
    if contract:
        print(f"[+] Signal received. Contract: {contract}")
        print(f"[~] Buy amount: {BUY_AMOUNT_SOL} SOL | Slippage: {SLIPPAGE}%")
        print("[✓] Preparing to auto-buy (coming in next milestone)")
    else:
        print("[!] No contract address found in message.")

def main():
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if wallet is None:
        return
    print("[*] Connecting to Telegram...")
    client.start(phone=PHONE)
    print("[✓] Connected. Listening for signals...")
    client.run_until_disconnected()

if __name__ == '__main__':
    main()
