import asyncio
import html
import logging
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
EXCHANGE_API_KEY = (os.getenv("EXCHANGE_API_KEY") or "").strip()
BANXICO_TOKEN = (os.getenv("BANXICO_TOKEN") or "").strip()

try:
    CACHE_TTL = max(60, int(os.getenv("CACHE_TTL_SECONDS", "43200")))  # 12 часов = раз в полдня
except ValueError:
    CACHE_TTL = 43200

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
MAX_MESSAGE_RESULTS = 20

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tengebot")

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
    "KZT": {
        "flag": "🇰🇿",
        "label": "тенге",
        "names": ["тг", "тенге", "тнг", "₸", "tenge", "kzt"],
        "cb_name": "НБК (Казахстан)",
    },
    "MDL": {
        "flag": "🇲🇩",
        "label": "молд. лей",
        "names": ["леев", "леи", "лея", "лей", "леїв", "леї", "leu", "lei", "mdl"],
        "cb_name": "НБМ (Молдова)",
    },
    "PLN": {
        "flag": "🇵🇱",
        "label": "злотых",
        "names": ["злотых", "злотый", "злотого", "злотих", "злотий", "злоті", "zloty", "zlotys", "zł", "zl", "pln"],
        "cb_name": "НБП (Польша)",
    },
    "UAH": {
        "flag": "🇺🇦",
        "label": "гривен",
        "names": ["гривен", "гривны", "гривна", "гривень", "гривня", "гривні", "грн", "₴", "uah", "hryvnia", "hryvnias", "hrn"],
        "cb_name": "НБУ (Украина)",
    },
    "NGN": {
        "flag": "🇳🇬",
        "label": "найр",
        "names": ["найра", "найры", "найри", "найр", "наира", "naira", "₦", "ngn"],
        "cb_name": None,
    },
    "MXN": {
        "flag": "🇲🇽",
        "label": "мекс. песо",
        "names": ["песо", "peso", "pesos", "mxn"],
        "cb_name": "Banxico (Мексика)",
    },
    "ETB": {
        "flag": "🇪🇹",
        "label": "быр",
        "names": ["быр", "быра", "бир", "бирр", "birr", "etb"],
        "cb_name": None,
    },
}

HEADERS_HTTP = {
    "User-Agent": "TengeBot/2.0 (+https://telegram.org/)",
    "Accept": "application/json, application/xml, text/xml, */*",
}

# One regex preserves the order in which currencies occur in a message.
_currency_names = sorted(
    {name for info in CURRENCIES.values() for name in info["names"]},
    key=len,
    reverse=True,
)
_name_to_code = {
    name.casefold(): code
    for code, info in CURRENCIES.items()
    for name in info["names"]
}
_names_re = "|".join(re.escape(name) for name in _currency_names)
# Currency name must not be glued to a preceding/following LETTER (so it
# doesn't match inside an unrelated word), but a digit right before it is
# fine — that's exactly how people write "100тг", "500₸" without a space.
_NOT_LETTER_BEFORE = r"(?<![^\W\d_])"
_NOT_LETTER_AFTER = r"(?![^\W\d_])"
AMOUNT_CURRENCY_RE = re.compile(
    rf"(?P<amount>\d[\d\s.,']*)(?P<space>\s*)"
    rf"(?P<currency>{_NOT_LETTER_BEFORE}(?:{_names_re}){_NOT_LETTER_AFTER})",
    re.IGNORECASE | re.UNICODE,
)

_rates_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_rates_lock = asyncio.Lock()


def code_for_currency(name: str) -> str:
    return _name_to_code[name.casefold()]


def parse_amount(raw: str) -> float:
    """Parse common integer/decimal/thousands-separated money formats."""
    s = re.sub(r"[\s\u00a0']", "", raw)
    if not s:
        raise ValueError("empty amount")

    if not re.fullmatch(r"\d+(?:[.,]\d+)*", s):
        raise ValueError(f"invalid amount: {raw!r}")

    if "," in s and "." in s:
        # The last separator is treated as the decimal separator.
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        integer_part, decimal_part = s.rsplit(decimal_sep, 1)
        integer_part = integer_part.replace(thousands_sep, "")
        normalized = f"{integer_part}.{decimal_part}"
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        # 1,234 / 1.234 -> 1234; 12,50 / 12.50 -> 12.50
        if len(parts) > 2:
            if not all(len(part) == 3 for part in parts[1:]):
                raise ValueError(f"invalid grouped amount: {raw!r}")
            normalized = "".join(parts)
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) >= 1:
            normalized = "".join(parts)
        else:
            normalized = f"{parts[0]}.{parts[1]}"
    else:
        normalized = s

    value = float(normalized)
    if not math.isfinite(value) or value < 0:
        raise ValueError("amount must be a finite non-negative number")
    if value > 1_000_000_000_000_000:
        raise ValueError("amount is too large")
    return value


def format_number(value: float) -> str:
    if value.is_integer():
        return f"{value:,.0f}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def format_usd(amount: float) -> str:
    if amount < 0.01:
        return f"{amount:.4f} $"
    return f"{format_number(amount)} $"


async def fetch_nbk_kzt(session: aiohttp.ClientSession) -> float | None:
    try:
        async with session.get(
            "https://nationalbank.kz/rss/rates_all.xml",
            headers=HEADERS_HTTP,
        ) as resp:
            resp.raise_for_status()
            root = ET.fromstring(await resp.text())
            for item in root.findall(".//item"):
                title = item.findtext("title")
                if title and title.strip().upper() == "USD":
                    value = item.findtext("description")
                    if value:
                        return float(value.replace(",", ".").strip())
    except Exception as exc:
        logger.warning("НБК: %s", exc)
    return None


async def fetch_nbu_uah(session: aiohttp.ClientSession) -> float | None:
    try:
        url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
        async with session.get(url, headers=HEADERS_HTTP) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            return float(data[0]["rate"]) if data else None
    except Exception as exc:
        logger.warning("НБУ: %s", exc)
    return None


async def fetch_nbp_pln(session: aiohttp.ClientSession) -> float | None:
    try:
        url = "https://api.nbp.pl/api/exchangerates/rates/a/usd/?format=json"
        async with session.get(url, headers=HEADERS_HTTP) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            return float(data["rates"][0]["mid"])
    except Exception as exc:
        logger.warning("НБП: %s", exc)
    return None


async def fetch_bnm_mdl(session: aiohttp.ClientSession) -> float | None:
    try:
        # BNM's date parameter is local-calendar based; use UTC date only
        # as a stable server-side default. If today's data is unavailable,
        # the API may return the latest available rate.
        today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        url = f"https://bnm.md/ru/official_exchange_rates?get_xml=1&date={today}"
        async with session.get(url, headers=HEADERS_HTTP) as resp:
            resp.raise_for_status()
            root = ET.fromstring(await resp.text())
            for valute in root.findall(".//Valute"):
                if valute.findtext("CharCode") == "USD":
                    value = valute.findtext("Value")
                    nominal = valute.findtext("Nominal") or "1"
                    if value:
                        return float(value.replace(",", ".")) / float(nominal)
    except Exception as exc:
        logger.warning("BNM: %s", exc)
    return None


async def fetch_banxico_mxn(session: aiohttp.ClientSession) -> float | None:
    if not BANXICO_TOKEN:
        logger.info("BANXICO_TOKEN не задан — MXN будет взят из fallback/API.")
        return None
    try:
        url = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF63528/datos/oportuno"
        headers = {
            **HEADERS_HTTP,
            "Bmx-Token": BANXICO_TOKEN,
        }
        params = {"token": BANXICO_TOKEN}
        async with session.get(url, headers=headers, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            series = data.get("bmx", {}).get("series", [])
            datos = series[0].get("datos", []) if series else []
            value = datos[0].get("dato") if datos else None
            if value and value != "N/E":
                return float(value)
    except Exception as exc:
        logger.warning("Banxico: %s", exc)
    return None


async def fetch_exchangerate_api(session: aiohttp.ClientSession) -> dict[str, float]:
    if EXCHANGE_API_KEY:
        url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/latest/USD"
    else:
        url = "https://api.exchangerate-api.com/v4/latest/USD"

    try:
        async with session.get(url, headers=HEADERS_HTTP) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            raw_rates = data.get("conversion_rates") or data.get("rates") or {}
            return {str(k): float(v) for k, v in raw_rates.items()}
    except Exception as exc:
        logger.warning("ExchangeRate-API: %s", exc)
        return {}


async def _load_rates() -> dict[str, dict[str, Any]]:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        results = await asyncio.gather(
            fetch_nbk_kzt(session),
            fetch_nbu_uah(session),
            fetch_nbp_pln(session),
            fetch_bnm_mdl(session),
            fetch_banxico_mxn(session),
            fetch_exchangerate_api(session),
            return_exceptions=True,
        )

    official = {
        "KZT": (results[0], CURRENCIES["KZT"]["cb_name"]),
        "UAH": (results[1], CURRENCIES["UAH"]["cb_name"]),
        "PLN": (results[2], CURRENCIES["PLN"]["cb_name"]),
        "MDL": (results[3], CURRENCIES["MDL"]["cb_name"]),
        "MXN": (results[4], CURRENCIES["MXN"]["cb_name"]),
    }
    global_rates = results[5] if isinstance(results[5], dict) else {}
    result: dict[str, dict[str, Any]] = {}

    for code in CURRENCIES:
        rate = None
        source = None

        official_rate, official_source = official.get(code, (None, None))
        if isinstance(official_rate, (int, float)) and official_rate > 0:
            rate, source = float(official_rate), official_source
        elif code in global_rates and global_rates[code] > 0:
            rate, source = global_rates[code], "ExchangeRate-API"
        elif code in FALLBACK_RATES:
            rate, source = FALLBACK_RATES[code], "Резервное значение"

        if rate is not None:
            result[code] = {"rate": rate, "source": source}

    return result


async def get_rates_data() -> dict[str, dict[str, Any]]:
    now = time.monotonic()
    if _rates_cache["data"] is not None and now - _rates_cache["ts"] < CACHE_TTL:
        return _rates_cache["data"]

    async with _rates_lock:
        now = time.monotonic()
        if _rates_cache["data"] is not None and now - _rates_cache["ts"] < CACHE_TTL:
            return _rates_cache["data"]

        fresh = await _load_rates()
        if fresh:
            _rates_cache.update(data=fresh, ts=time.monotonic())
            return fresh

        # If every upstream failed, prefer stale data over no response.
        if _rates_cache["data"] is not None:
            logger.error("Все источники курсов недоступны; используем устаревший кэш.")
            return _rates_cache["data"]

        return {}


def extract_conversions(text: str) -> list[tuple[str, str]]:
    found = []
    for match in AMOUNT_CURRENCY_RE.finditer(text):
        raw_amount = match.group("amount")
        currency = match.group("currency")
        found.append((code_for_currency(currency), raw_amount))
        if len(found) >= MAX_MESSAGE_RESULTS:
            break
    return found


def rates_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📊 Показать все курсы", callback_data="show_rates")]]
    )


async def cmd_start(message: Message):
    lines = [
        "👋 <b>Привет! Я конвертирую суммы в разных валютах в доллары США.</b>",
        "",
        "Просто напиши сумму рядом с валютой — можно с пробелом, можно слитно, любым удобным способом:",
        "<code>500 ₸</code>, <code>500тг</code>, <code>100 злотых</code>, <code>1500грн</code>, <code>200 pesos</code>",
        "",
        "🌐 <b>Понимаю три языка</b> — русский, українську та English, — плюс символы (₸ ₴ ₦) и ISO-коды (KZT, PLN…).",
        "В одном сообщении можно упомянуть сразу несколько валют — отвечу по каждой.",
        "",
        "💱 <b>Валюты и откуда курс:</b>",
    ]
    for code, info in CURRENCIES.items():
        source = info["cb_name"] or "ExchangeRate-API"
        lines.append(f"{info['flag']} <b>{code}</b> ({info['label']}) — {source}")
    lines += [
        "",
        "Курс обновляется раз в полдня. Если официальный ЦБ временно недоступен — беру резервный источник, бот сам разберётся.",
        "",
        "📋 <b>Команды:</b>",
        "• /rates — курс к доллару по всем валютам сразу (или жми на кнопку ниже)",
    ]
    await message.answer("\n".join(lines), reply_markup=rates_keyboard())


def build_rates_text(rates_data: dict[str, dict[str, Any]]) -> str:
    lines = ["📊 <b>Курс валют к 1 USD:</b>", ""]
    for code, info in CURRENCIES.items():
        data = rates_data.get(code)
        if not data:
            lines.append(f"{info['flag']} <b>{code}</b> — данных нет")
            continue
        lines.append(
            f"{info['flag']} <b>1 USD = {format_number(data['rate'])} {code}</b>\n"
            f"   └ <i>{html.escape(data['source'])}</i>"
        )
    lines += ["", "ℹ️ Порядок источников: официальный ЦБ → ExchangeRate-API → резервное значение."]
    return "\n".join(lines)


async def cmd_rates(message: Message):
    rates_data = await get_rates_data()
    if not rates_data:
        await message.answer("⚠️ Сейчас не удалось получить курсы валют. Попробуйте позже.")
        return
    await message.answer(build_rates_text(rates_data))


async def cb_show_rates(callback: CallbackQuery):
    rates_data = await get_rates_data()
    if not rates_data:
        await callback.answer("⚠️ Курсы сейчас недоступны, попробуй позже", show_alert=True)
        return
    await callback.message.answer(build_rates_text(rates_data))
    await callback.answer()


async def handle_message(message: Message):
    conversions = extract_conversions(message.text or "")
    if not conversions:
        return

    rates_data = await get_rates_data()
    if not rates_data:
        await message.answer("⚠️ Не удалось получить курсы валют. Попробуйте ещё раз позже.")
        return

    lines = []
    used_sources = set()

    for code, raw in conversions:
        data = rates_data.get(code)
        if not data:
            continue
        try:
            amount = parse_amount(raw)
        except ValueError:
            logger.info("Не удалось распознать сумму %r", raw)
            continue

        usd = amount / data["rate"]
        info = CURRENCIES[code]
        lines.append(
            f"{info['flag']} <b>{format_number(amount)} {info['label']}</b> = "
            f"<b>{format_usd(usd)}</b>"
        )
        used_sources.add(f"{code}: {data['source']}")

    if lines:
        result = "\n".join(lines)
        if used_sources:
            result += "\n\n🏦 <i>" + html.escape(" | ".join(sorted(used_sources))) + "</i>"
        await message.answer(result)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Создайте .env и укажите BOT_TOKEN.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Register handlers on the local dispatcher.
    dp.message.register(cmd_start, Command("start", "help"))
    dp.message.register(cmd_rates, Command("rates", "courses", "kurs"))
    dp.callback_query.register(cb_show_rates, F.data == "show_rates")
    dp.message.register(handle_message, F.text)

    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())