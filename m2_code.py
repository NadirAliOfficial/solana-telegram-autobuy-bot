import re
import os
import asyncio
import base64
from dotenv import load_dotenv
from telethon import TelegramClient, events
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed
from jupiter_python_sdk.jupiter import Jupiter

# === Load config ===
load_dotenv()
API_ID             = int(os.getenv("API_ID"))
API_HASH           = os.getenv("API_HASH")
PHONE              = os.getenv("PHONE")
GROUP              = int(os.getenv("GROUP"))       # e.g. -1001993316422
RPC_URL            = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
BUY_AMOUNT_SOL     = float(os.getenv("BUY_AMOUNT_SOL"))
SLIPPAGE_PCT       = float(os.getenv("SLIPPAGE"))  # e.g. 10 (%)
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

# === Auto-buy via Jupiter SDK with graceful skip ===
async def auto_buy(mint: str, wallet, sol_client, jup_client):
    print(f"\n[→] Attempting Jupiter swap for {mint} …")
    try:
        swap_b64 = await jup_client.swap(
            input_mint=WSOL_MINT,
            output_mint=mint,
            amount=int(BUY_AMOUNT_SOL * 1e9),
            slippage_bps=SLIPPAGE_BPS
        )
    except Exception as e:
        if "not tradable" in str(e):
            print(f"[!] {mint} is not tradable on Jupiter — skipping.")
            return
        print(f"[x] Jupiter error for {mint}: {e}")
        return

    # Deserialize & sign
    raw_tx    = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    sig       = wallet.sign_message(message.to_bytes_versioned(raw_tx.message))
    signed_tx = VersionedTransaction.populate(raw_tx.message, [sig])

    # Send & confirm
    print(f"[→] Sending swap txn for {mint} …")
    resp = await sol_client.send_raw_transaction(
        txn=bytes(signed_tx),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)
    )
    # resp may be a dict or SendTransactionResp
    if hasattr(resp, 'result'):
        txid = resp.result
    elif isinstance(resp, dict) and 'result' in resp:
        txid = resp['result']
    elif hasattr(resp, 'value'):
        txid = resp.value
    else:
        txid = str(resp)

    print(f"[✓] Swap succeeded for {mint}: {txid}")

# === Telegram setup ===
client = TelegramClient('session', API_ID, API_HASH)

@client.on(events.NewMessage(chats=GROUP))
async def live_handler(evt):
    text = evt.raw_text or ""
    mint = extract_mint(text)
    if mint:
        print(f"🕒 New mint detected: {mint}")
        await auto_buy(mint, wallet, sol_client, jup_client)

# === Fetch last 3 messages silently ===
async def fetch_last(limit=3):
    async for msg in client.iter_messages(GROUP, limit=limit):
        mint = extract_mint(msg.raw_text or "")
        if mint:
            print(f"[↑] Processing past mint: {mint}")
            await auto_buy(mint, wallet, sol_client, jup_client)

# === Main entrypoint ===
async def main():
    global wallet, sol_client, jup_client
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet:
        return

    sol_client, jup_client = await create_clients(wallet)

    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(PHONE)
        code = input("Enter Telegram code: ")
        await client.sign_in(PHONE, code)

    await fetch_last(3)
    print("[✓] Listening for live mints…")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
