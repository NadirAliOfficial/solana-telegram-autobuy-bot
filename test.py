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
    "BUY_AMOUNT_SOL":         0.05,
    "SLIPPAGE_PCT":           10.0,
    "AUTO_SELL_ENABLED":      True,
    "SELL_TIERS": [
        {"profit_pct":  50, "sell_pct": 50},
        {"profit_pct": 100, "sell_pct": 20},
        {"profit_pct": 200, "sell_pct": 20},
        {"profit_pct": 500, "sell_pct": 10}
    ],
    "TRAILING_STOP_DROP_PCT": 10.0,
    "POLL_INTERVAL":          15
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
        "BUY_AMOUNT_SOL":         float(cfg.get("BUY_AMOUNT_SOL",  DEFAULT_CONFIG["BUY_AMOUNT_SOL"])),
        "SLIPPAGE_PCT":           float(cfg.get("SLIPPAGE_PCT",    DEFAULT_CONFIG["SLIPPAGE_PCT"])),
        "AUTO_SELL_ENABLED":      bool(cfg.get("AUTO_SELL_ENABLED",DEFAULT_CONFIG["AUTO_SELL_ENABLED"])),
        "SELL_TIERS":             cfg.get("SELL_TIERS",          DEFAULT_CONFIG["SELL_TIERS"]),
        "TRAILING_STOP_DROP_PCT": float(cfg.get("TRAILING_STOP_DROP_PCT",DEFAULT_CONFIG["TRAILING_STOP_DROP_PCT"])),
        "POLL_INTERVAL":          int(cfg.get("POLL_INTERVAL",     DEFAULT_CONFIG["POLL_INTERVAL"]))
    }

# ─── ENV & CLIENT SETUP ─────────────────────────────────────────────────────
load_dotenv()
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")
BOT_TOKEN          = os.getenv("BOT_TOKEN")
RPC_URL            = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
WSOL_MINT          = os.getenv(
    "WSOL_MINT",
    "So11111111111111111111111111111111111111112"
)

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

# ─── SWAP & MONITOR LOGIC ───────────────────────────────────────────────────
async def auto_buy(
    mint: str,
    wallet_keypair,
    sol,
    jup,
    purchase_amount=None,
    chat_id=None,
    bot=None
):
    s           = get_settings()
    amount_sol  = purchase_amount if purchase_amount is not None else s["BUY_AMOUNT_SOL"]
    lamports_in = int(amount_sol * 1e9)
    slippage    = int(s["SLIPPAGE_PCT"] * 100)

    # fetch a quote (dict) instead of get_routes()
    quote = await jup.quote(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount=lamports_in,
        slippage_bps=slippage
    )
    # now safely extract routes from dict or object
    if isinstance(quote, dict):
        routes = quote.get("routes") or quote.get("data", {}).get("routes", [])
    else:
        routes = getattr(quote, "routes", [])

    if not routes:
        await bot.send_message(chat_id, f"❌ No route to buy `{mint}`")
        return
    lamports_out = routes[0].out_amount
    buy_price    = lamports_in / lamports_out

    # execute the buy swap
    swap_b64 = await jup.swap(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount=lamports_in,
        slippage_bps=slippage
    )
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    sig    = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
    txn    = VersionedTransaction.populate(raw_tx.message, [sig])
    resp   = await sol.send_raw_transaction(
        txn=bytes(txn),
        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)
    )
    txid   = getattr(resp, "result", getattr(resp, "value", str(resp)))

    await bot.send_message(
        chat_id,
        f"✅ Bought {amount_sol} SOL of `{mint}` @ {buy_price:.6f} SOL/token → tx `{txid}`",
        parse_mode="Markdown"
    )

    if s["AUTO_SELL_ENABLED"]:
        asyncio.create_task(
            monitor_and_sell(
                mint,
                lamports_out,
                buy_price,
                wallet_keypair,
                sol,
                jup,
                chat_id,
                bot
            )
        )

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
    s         = get_settings()
    tiers     = sorted(s["SELL_TIERS"], key=lambda t: t["profit_pct"])
    remaining = lamports_out
    highest   = buy_price

    while remaining > 0 and tiers:
        # fetch reverse quote
        quote = await jup.quote(
            input_mint=mint,
            output_mint=WSOL_MINT,
            amount=remaining,
            slippage_bps=int(s["SLIPPAGE_PCT"] * 100)
        )
        if isinstance(quote, dict):
            routes = quote.get("routes") or quote.get("data", {}).get("routes", [])
        else:
            routes = getattr(quote, "routes", [])

        if not routes:
            await bot.send_message(chat_id, f"❌ Price fetch failed for `{mint}`")
            await asyncio.sleep(s["POLL_INTERVAL"])
            continue

        current_price = routes[0].out_amount / remaining
        highest       = max(highest, current_price)

        # profit tiers
        for tier in list(tiers):
            if current_price >= buy_price * (1 + tier["profit_pct"]/100):
                sell_amt = int(remaining * (tier["sell_pct"]/100))
                if sell_amt:
                    swap_b64 = await jup.swap(
                        input_mint=mint,
                        output_mint=WSOL_MINT,
                        amount=sell_amt,
                        slippage_bps=int(s["SLIPPAGE_PCT"] * 100)
                    )
                    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
                    sig    = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
                    txn    = VersionedTransaction.populate(raw_tx.message, [sig])
                    resp   = await sol.send_raw_transaction(
                        txn=bytes(txn),
                        opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)
                    )
                    txid = getattr(resp, "result", getattr(resp, "value", str(resp)))
                    await bot.send_message(
                        chat_id,
                        f"🔸 Sold {tier['sell_pct']}% @ +{tier['profit_pct']}% → tx `{txid}`",
                        parse_mode="Markdown"
                    )
                    remaining -= sell_amt
                tiers.remove(tier)

        # trailing stop:
        drop_thr = highest * (1 - s["TRAILING_STOP_DROP_PCT"]/100)
        if current_price <= drop_thr:
            swap_b64 = await jup.swap(
                input_mint=mint,
                output_mint=WSOL_MINT,
                amount=remaining,
                slippage_bps=int(s["SLIPPAGE_PCT"] * 100)
            )
            raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
            sig    = wallet_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
            txn    = VersionedTransaction.populate(raw_tx.message, [sig])
            resp   = await sol.send_raw_transaction(
                txn=bytes(txn),
                opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)
            )
            txid = getattr(resp, "result", getattr(resp, "value", str(resp)))
            await bot.send_message(
                chat_id,
                f"🛑 Trailing stop sold remaining → tx `{txid}`",
                parse_mode="Markdown"
            )
            break

        await asyncio.sleep(s["POLL_INTERVAL"])

# ─── TELEGRAM BOT HANDLERS ───────────────────────────────────────────────────
ST_KEY, ST_VAL = range(2)
BT_MINT, BT_AMT = range(2)

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot ready!\n"
        "/get – show settings\n"
        "/set – change settings\n"
        "/buy – manual buy"
    )

async def get_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg  = load_config()
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
        cfg[key] = json.loads(val)
    except json.JSONDecodeError:
        try:
            cfg[key] = float(val) if '.' in val else int(val)
        except:
            cfg[key] = val
    save_config(cfg)
    await update.message.reply_text(f"✅ {key} set to {cfg[key]}")
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
        await update.message.reply_text("Invalid amount. Try again:")
        return BT_AMT
    mint    = ctx.user_data['mint']
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🔄 Buying {amt} SOL of {mint}...")
    await auto_buy(
        mint,
        wallet,
        sol_client,
        jup_client,
        purchase_amount=amt,
        chat_id=chat_id,
        bot=ctx.bot
    )
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

def run_bot():
    conv_set = ConversationHandler(
        entry_points=[CommandHandler('set', set_start)],
        states={
            ST_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_key)],
            ST_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_val)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    conv_buy = ConversationHandler(
        entry_points=[CommandHandler('buy', buy_start)],
        states={
            BT_MINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_mint)],
            BT_AMT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amt)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app = ApplicationBuilder()\
        .token(BOT_TOKEN)\
        .concurrent_updates(True)\
        .build()

    app.add_handler(CommandHandler('start', start_cmd))
    app.add_handler(CommandHandler('get',   get_cmd))
    app.add_handler(conv_set)
    app.add_handler(conv_buy)

    print('[✓] Bot running...')
    app.run_polling()

if __name__ == '__main__':
    ensure_config()
    wallet = setup_wallet(WALLET_PRIVATE_KEY)
    if not wallet:
        print('[x] Wallet setup failed. Check WALLET_PRIVATE_KEY.')
        exit(1)
    sol_client, jup_client = asyncio.run(create_clients(wallet))
    run_bot()
