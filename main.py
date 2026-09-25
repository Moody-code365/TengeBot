import asyncio
import re
import time
import aiohttp
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
EXCHANGE_API_KEY = os.getenv("EXCHANGE_API_KEY")  # ключ с exchangerate-api.com (v6)
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # раз в сутки — фри-тариф всё равно обновляет раз в день

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Фолбэк-курсы к USD, если API недоступен и кэш пустой.
# Раз в пару месяцев стоит обновлять руками.
FALLBACK_RATES = {
    "KZT": 450.0,
    "MDL": 17.7,
    "PLN": 3.9,
    "UAH": 41.5,
    "NGN": 1530.0,
    "MXN": 18.4,
    "ETB": 123.0,
}

CURRENCIES = {
    "KZT": {"flag": "🇰🇿", "label": "тенге", "names": ["тг", "тенге", "тнг", "₸"]},
    "MDL": {"flag": "🇲🇩", "label": "молд. лей", "names": ["лей", "леи", "лея", "mdl"]},
    "PLN": {"flag": "🇵🇱", "label": "злотый", "names": ["злотых", "злотый", "zł", "zl", "pln"]},
    "UAH": {"flag": "🇺🇦", "label": "гривна", "names": ["гривен", "гривны", "гривна", "грн", "₴", "uah"]},
    "NGN": {"flag": "🇳🇬", "label": "найра", "names": ["найра", "наира", "₦", "ngn"]},
    "MXN": {"flag": "🇲🇽", "label": "мекс. песо", "names": ["песо", "mxn"]},
    "ETB": {"flag": "🇪🇹", "label": "быр", "names": ["быр", "birr", "etb"]},
}


def build_patterns() -> dict:
    """Число + один из вариантов написания валюты, самые длинные варианты проверяем первыми."""
    patterns = {}
    for code, info in CURRENCIES.items():
        names_sorted = sorted(info["names"], key=len, reverse=True)
        names_re = "|".join(re.escape(n) for n in names_sorted)
        patterns[code] = re.compile(rf"(\d[\d\s,.']*)\s*(?:{names_re})", re.IGNORECASE | re.UNICODE)
    return patterns


PATTERNS = build_patterns()

_rates_cache = {"rates": None, "ts": 0.0}


async def get_rates() -> dict:
    """Курсы всех валют разом (база USD), кэш на CACHE_TTL секунд — экономим лимит API."""
    now = time.time()
    if _rates_cache["rates"] and now - _rates_cache["ts"] < CACHE_TTL:
        return _rates_cache["rates"]

    if EXCHANGE_API_KEY:
        url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/latest/USD"
    else:
        url = "https://api.exchangerate-api.com/v4/latest/USD"  # бесплатный фолбэк без ключа

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("conversion_rates") or data.get("rates")
                    if rates:
                        _rates_cache["rates"] = rates
                        _rates_cache["ts"] = now
                        return rates
    except Exception as e:
        logger.warning(f"Не удалось получить курсы: {e}")

    if _rates_cache["rates"]:
        logger.warning("Отдаю устаревший кэш курсов")
        return _rates_cache["rates"]

    logger.warning("Использую фолбэк-курсы")
    return FALLBACK_RATES


def parse_amount(raw: str) -> float:
    """Разбираем число, где пробел/апостроф — разделитель тысяч, а , или . — либо тысячи, либо десятичные."""
    s = re.sub(r"[\s']", "", raw)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.split(",")[-1]
        s = s.replace(",", ".") if len(tail) in (1, 2) else s.replace(",", "")
    return float(s)


def format_number(x: float) -> str:
    s = f"{x:,.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return s.replace(",", " ")


def format_usd(amount: float) -> str:
    if amount < 0.01:
        return f"{amount:.4f} $"
    if amount < 1:
        return f"{amount:.2f} $"
    return f"{format_number(amount)} $"


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    lines = ["👋 Пишите сумму в валюте — переведу в доллары.", "", "Понимаю:"]
    for code, info in CURRENCIES.items():
        example = info["names"][0]
        lines.append(f"{info['flag']} <b>{code}</b> — например «500 {example}»")
    await message.reply("\n".join(lines), parse_mode="HTML")


@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text or ""
    found = []  # (code, raw_amount)

    for code, pattern in PATTERNS.items():
        for raw in pattern.findall(text):
            found.append((code, raw))

    if not found:
        return

    rates = await get_rates()
    lines = []

    for code, raw in found:
        rate = rates.get(code)
        if not rate:
            continue
        try:
            amount = parse_amount(raw)
        except ValueError:
            continue
        usd = amount / rate
        info = CURRENCIES[code]
        lines.append(f"{info['flag']} <b>{format_number(amount)} {info['label']}</b> = <b>{format_usd(usd)}</b>")

    if lines:
        result = "\n".join(lines)
        result += f"\n\n<i>📈 Курс обновлён не позднее часа назад</i>"
        await message.reply(result, parse_mode="HTML")


async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())