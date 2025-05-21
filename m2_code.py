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
GROUP              = int(os.getenv("GROUP"))       # e.g. -1001993316422
BUY_AMOUNT_SOL     = float(os.getenv("BUY_AMOUNT_SOL"))
SLIPPAGE_PCT       = float(os.getenv("SLIPPAGE"))  # as percent, e.g. 10

# === wallet setup stub ===
def setup_wallet(pk_str: str):
    try:
        wallet = Keypair.from_base58_string(pk_str)
        return wallet
    except:
        return None

# === extract mint addresses ===
def extract_mint(text: str):
    matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{43,44}', text)
    return max(matches, key=len) if matches else None

# === stub order logic ===
async def place_order(mint: str):
    print(f"[→] Placing order: {BUY_AMOUNT_SOL} SOL → {mint}")
    # simulate a tx signature
    sig = "<stubbed_tx_signature>"
    print(f"[✓] Order placed, tx: {sig}")
    # calculate stop-loss price stub (notional)
    print(f"[↳] Stop-loss set at {SLIPPAGE_PCT:.1f}% below fill price\n")

# === Telegram setup ===
client = TelegramClient('session', API_ID, API_HASH)

@client.on(events.NewMessage(chats=GROUP))
async def on_live(evt):
    text = evt.raw_text or ""
    mint = extract_mint(text)
    print(f"\n🕒 Live message: {text}")
    if mint:
        print(f"[+] Detected mint: {mint}")
        await place_order(mint)
    else:
        print("[!] No mint address found")

# === fetch last 3 messages ===
async def fetch_last(limit=3):
    # print(f"[*] Fetching last {limit} messages…")
    async for msg in client.iter_messages(GROUP, limit=limit):
        text = msg.raw_text or ""
        mint = extract_mint(text)
        # print(f"\n📜 Past message: {text}")
        if mint:
            print(f"[+] Past mint: {mint}")
            await place_order(mint)
        else:
            print("[!] No mint address in past message")

# === main ===
async def main():
    wallet = setup_wallet(os.getenv("WALLET_PRIVATE_KEY"))
    if not wallet:
        print("⚠️ Wallet setup failed")
        return

    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(PHONE)
        code = input("Enter Telegram code: ")
        await client.sign_in(PHONE, code)

    await fetch_last(3)
    print("[✓] Now listening for new messages…")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
