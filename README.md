# Solana Telegram AutoBuy Bot

A Python bot that listens to private Telegram channels for trading signals (Solana contract addresses) and automatically executes buy transactions on the Solana blockchain via Jupiter aggregator.

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat&logo=python&logoColor=white)
![Solana](https://img.shields.io/badge/Solana-9945FF?style=flat&logo=solana&logoColor=white)

## Features

- Listens to Telegram channels/groups via Telethon
- Detects Solana contract addresses from messages
- Auto-buys using Jupiter swap API
- Configurable buy amount per signal
- Deduplication — skips already-bought contracts in the session
- Logs all signals and transaction signatures

## Setup

```bash
pip install telethon solana requests python-dotenv
```

Create a `.env` file:

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+1234567890
TARGET_CHANNELS=channel_username1,channel_username2
WALLET_PRIVATE_KEY=your_base58_private_key
BUY_AMOUNT_SOL=0.1
SLIPPAGE_BPS=100
```

## Usage

```bash
python main.py
```

The bot connects to Telegram, monitors the configured channels, and executes a swap whenever a valid Solana token address is detected.

## Files

| File      | Purpose                             |
|-----------|-------------------------------------|
| `main.py` | Entry point — connects and listens  |
| `bot.py`  | Core signal detection and buy logic |
| `test.py` | Integration tests                   |

## Warning

Use a dedicated wallet with only the funds you intend to trade. Never use your main wallet private key.

## License

MIT
