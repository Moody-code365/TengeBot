import asyncio
import re
import aiohttp
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FALLBACK_RATE = 450.0  # курс на случай если API недоступно

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def get_usd_rate() -> float:
    """Берём актуальный курс KZT/USD с открытого API."""
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.exchangerate-api.com/v4/latest/USD"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rate = data["rates"].get("KZT")
                    if rate:
                        return float(rate)
    except Exception as e:
        logger.warning(f"Не удалось получить курс: {e}. Используем fallback {FALLBACK_RATE}")
    return FALLBACK_RATE


# Ловим: 500тг / 500 тг / 500тенге / 500 тенге / 500₸ / 500 ₸
PATTERN = re.compile(
    r"(\d[\d\s,.']*)\s*(тг|тенге|₸)",
    re.IGNORECASE | re.UNICODE
)


def parse_amount(raw: str) -> float:
    """Чистим строку от пробелов и запятых, возвращаем число."""
    cleaned = re.sub(r"[\s,']", "", raw).replace(".", "")
    return float(cleaned)


def format_usd(amount: float) -> str:
    if amount < 0.01:
        return f"{amount:.4f} $"
    elif amount < 1:
        return f"{amount:.2f} $"
    return f"{amount:,.2f} $".replace(",", " ")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        "👋 Пишите суммы в тенге — переведу в доллары автоматически.\n\n"
        "Примеры: <b>500тг</b>, <b>1500 тенге</b>, <b>10000₸</b>",
        parse_mode="HTML"
    )


@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text or ""
    matches = PATTERN.findall(text)

    if not matches:
        return  # нет суммы в тенге — молчим

    rate = await get_usd_rate()
    lines = []

    for raw_amount, _ in matches:
        try:
            kzt = parse_amount(raw_amount)
            usd = kzt / rate
            kzt_str = f"{kzt:,.0f}".replace(",", " ")
            lines.append(f"💰 <b>{kzt_str} ₸</b> = <b>{format_usd(usd)}</b>")
        except ValueError:
            continue

    if lines:
        result = "\n".join(lines)
        result += f"\n\n<i>📈 Курс: 1 $ = {rate:.1f} ₸</i>"
        await message.reply(result, parse_mode="HTML")


async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())