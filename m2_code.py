import re
import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient, events
from solders.keypair import Keypair

# === load .env ===
load_dotenv()
API_ID             = int(os.getenv("API_ID"))
API_HASH           = os.getenv("API_HASH")
PHONE              = os.getenv("PHONE")
GROUP              = os.getenv("GROUP")
BUY_AMOUNT_SOL     = float(os.getenv("BUY_AMOUNT_SOL"))
SLIPPAGE           = float(os.getenv("SLIPPAGE"))
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

print("Loaded WALLET_PRIVATE_KEY:", WALLET_PRIVATE_KEY)

# === wallet setup ===
def setup_wallet(private_key_str: str):
    try:
        wallet = Keypair.from_base58_string(private_key_str)
        print(f"[✓] Wallet Public Key: {wallet.pubkey()}")
        return wallet
    except Exception as e:
        print(f"[x] Wallet setup error: {e}")
        return None

# === parser ===
def extract_contract_address(text):
    m = re.search(r'(?:0x)?[A-Fa-f0-9]{32,44}', text)
    return m.group(0) if m else None

# === telegram ===
client = TelegramClient('session', API_ID, API_HASH)
@client.on(events.NewMessage(chats=GROUP))
async def handler(event):
    msg = event.raw_text
    c = extract_contract_address(msg)
    if c:
        print(f"[+] Signal: {c}")
        print(f"[~] Buy: {BUY_AMOUNT_SOL} SOL | Slip: {SLIPPAGE}%")
        print("[✓] Ready for auto-buy (Milestone 2)")
    else:
        print("[!] No contract found")

def main():
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet: return
    print("[*] Connecting to Telegram…")
    client.start(phone=PHONE)
    client.run_until_disconnected()

if __name__ == '__main__':
    main()
