# TengeBot

Telegram bot for converting KZT to USD automatically.

## Features

- Automatically detects amounts in KZT (тенге) in messages
- Converts them to USD using real-time exchange rates
- Supports various formats: `500тг`, `1500 тенге`, `10000₸`
- Uses fallback rate if API is unavailable

## Docker Deployment

### Prerequisites

1. Docker and Docker Compose installed
2. Telegram bot token from [@BotFather](https://t.me/BotFather)

### Setup

1. Create `.env` file with your bot token:
   ```
   BOT_TOKEN=your_telegram_bot_token_here
   ```

2. Run with Docker Compose:
   ```bash
   docker-compose up -d
   ```

### Commands

- Start the bot: `docker-compose up -d`
- Stop the bot: `docker-compose down`
- View logs: `docker-compose logs -f tengebot`

## Local Development

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Create `.env` file with `BOT_TOKEN`

3. Run the bot:
   ```bash
   python main.py
   ```

## Usage

Send any message containing amounts in KZT to the bot, and it will automatically convert them to USD.

Examples:
- `500тг` → `💰 500 ₸ = 1.11 $`
- `1500 тенге` → `💰 1 500 ₸ = 3.33 $`
- `10000₸` → `💰 10 000 ₸ = 22.22 $`
