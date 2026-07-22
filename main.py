"""
Telegram-бот "Расписание рейсов" для конкретных людей.

Что делает бот:
- У каждого человека — своё направление (и свои города вылета).
- Бот запрашивает актуальные варианты перелёта (даты, цену, авиакомпанию,
  количество пересадок) через бесплатный API Travelpayouts/Aviasales.
- Показывает только даты вплоть до 31 августа 2026 включительно.

ВАЖНО (реальность рынка, а не ограничение бота):
Прямых рейсов Россия -> Лондон / Корфу (Греция) / Берлин не существует
с 2022 года (санкции ЕС/Великобритании и зеркальные ответные меры РФ).
Бот будет показывать реальные варианты С ПЕРЕСАДКОЙ для этих направлений.
Прямые рейсы физически есть только до Бангкока.

Настройка:
1. Получить токен бота у @BotFather в Telegram -> переменная BOT_TOKEN.
2. Зарегистрироваться на https://www.travelpayouts.com/ (бесплатно, для доступа
   к Data API) -> получить токен -> переменная TRAVELPAYOUTS_TOKEN.
   Документация API: https://support.travelpayouts.com/hc/ru/articles/203956163
3. pip install -r requirements.txt
4. python main.py
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8679015857:AAHcQYd1sDpbQh14C-RmGiQXcpcx3CIr3mk")
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "656b062c46e3d253140b45d9d8a8ddd3")

# Валюта отображения цен
CURRENCY = "rub"

# Крайняя дата, до которой ищем рейсы (включительно)
DEADLINE = date(2026, 8, 31)

# IATA-коды городов
MOSCOW = "MOW"
SPB = "LED"
KRASNODAR = "KRR"

LONDON = "LON"
CORFU = "CFU"
BANGKOK = "BKK"
BERLIN = "BER"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("flightbot")


@dataclass(frozen=True)
class Route:
    origin: str
    origin_name: str
    destination: str
    destination_name: str


@dataclass(frozen=True)
class Person:
    key: str
    display_name: str
    routes: tuple  # tuple[Route, ...]


# ---------------------------------------------------------------------------
# КТО ЛЕТИТ КУДА
# ---------------------------------------------------------------------------

PEOPLE = {
    "sergey": Person(
        key="sergey",
        display_name="Сергей",
        routes=(
            Route(MOSCOW, "Москва", LONDON, "Лондон"),
            Route(SPB, "Санкт-Петербург", LONDON, "Лондон"),
        ),
    ),
    "darya": Person(
        key="darya",
        display_name="Дарья",
        routes=(
            Route(MOSCOW, "Москва", CORFU, "Корфу"),
            Route(SPB, "Санкт-Петербург", CORFU, "Корфу"),
        ),
    ),
    "valeria": Person(
        key="valeria",
        display_name="Валерия",
        routes=(
            Route(MOSCOW, "Москва", BANGKOK, "Бангкок"),
            Route(SPB, "Санкт-Петербург", BANGKOK, "Бангкок"),
        ),
    ),
    "maxim": Person(
        key="maxim",
        display_name="Максим",
        routes=(
            Route(MOSCOW, "Москва", BERLIN, "Берлин"),
            Route(SPB, "Санкт-Петербург", BERLIN, "Берлин"),
            Route(KRASNODAR, "Краснодар", BERLIN, "Берлин"),
        ),
    ),
}

# Привязка chat_id -> человек. Хранится в памяти; при перезапуске бота
# люди снова выберут себя через /start (это простое хранилище, для
# постоянного хранения можно заменить на файл/БД).
CHAT_TO_PERSON: dict[int, str] = {}


# ---------------------------------------------------------------------------
# ЗАПРОСЫ К TRAVELPAYOUTS (Aviasales Data API)
# ---------------------------------------------------------------------------

TP_CALENDAR_URL = "https://api.travelpayouts.com/v2/prices/latest"


async def fetch_flights_for_route(session: aiohttp.ClientSession, route: Route) -> list[dict]:
    """
    Возвращает список найденных вариантов перелёта по маршруту, с датой
    вылета не позднее DEADLINE. Данные — из кэша Aviasales (реальные цены
    и даты, которые люди покупали/искали в последнее время), это не
    полное расписание "всех рейсов на завтра", а срез актуальных
    доступных вариантов.
    """
    params = {
        "origin": route.origin,
        "destination": route.destination,
        "currency": CURRENCY,
        "period_type": "year",
        "one_way": "true",
        "sorting": "price",
        "limit": 30,
        "token": TRAVELPAYOUTS_TOKEN,
    }
    try:
        async with session.get(TP_CALENDAR_URL, params=params, timeout=15) as resp:
            if resp.status != 200:
                log.warning("Travelpayouts вернул статус %s для %s->%s", resp.status, route.origin, route.destination)
                return []
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("Ошибка запроса к Travelpayouts: %s", exc)
        return []

    results = data.get("data", []) if isinstance(data, dict) else []
    filtered = []
    for item in results:
        dep_date_str = item.get("depart_date")
        if not dep_date_str:
            continue
        try:
            dep_date = datetime.strptime(dep_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dep_date <= DEADLINE:
            filtered.append(item)

    filtered.sort(key=lambda x: x.get("depart_date", ""))
    return filtered


def format_flight_item(item: dict) -> str:
    dep = item.get("depart_date", "?")
    price = item.get("value", "?")
    airline = item.get("airline", "—")
    transfers = item.get("number_of_changes")
    if transfers is None:
        transfers_txt = ""
    elif transfers == 0:
        transfers_txt = "прямой рейс"
    else:
        transfers_txt = f"{transfers} пересадк{'а' if transfers == 1 else 'и'}"
    return f"📅 {dep} — {price} {CURRENCY.upper()} · ✈️ {airline}" + (f" · {transfers_txt}" if transfers_txt else "")


async def build_schedule_text(person: Person) -> str:
    lines = [f"<b>Расписание для {person.display_name}</b>", f"(даты вылета до {DEADLINE.strftime('%d.%m.%Y')} включительно)\n"]

    async with aiohttp.ClientSession() as session:
        for route in person.routes:
            lines.append(f"\n<b>{route.origin_name} ({route.origin}) → {route.destination_name} ({route.destination})</b>")
            flights = await fetch_flights_for_route(session, route)
            if not flights:
                lines.append("Нет данных / вариантов на эту дату в кэше Aviasales.")
                continue
            for item in flights[:10]:
                lines.append(format_flight_item(item))

    if any(r.destination in (LONDON, CORFU, BERLIN) for r in person.routes):
        lines.append(
            "\n⚠️ Прямых рейсов Россия—ЕС/Великобритания нет с 2022 года "
            "(санкции и зеркальные ограничения). Все варианты выше — с пересадкой "
            "(обычно через Стамбул, Дубай, Белград, Ереван и т.п.)."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TELEGRAM-БОТ
# ---------------------------------------------------------------------------

router = Router()


def people_keyboard() -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton(text=p.display_name)] for p in PEOPLE.values()]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить расписание", callback_data="refresh")]]
    )


def person_by_name(name: str) -> Optional[Person]:
    for p in PEOPLE.values():
        if p.display_name.lower() == name.lower().strip():
            return p
    return None


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я показываю актуальные варианты перелётов "
        f"(до {DEADLINE.strftime('%d.%m.%Y')} включительно).\n\n"
        "Выбери, кто ты, чтобы увидеть своё направление:",
        reply_markup=people_keyboard(),
    )


@router.message(Command("schedule"))
async def cmd_schedule(message: Message) -> None:
    person_key = CHAT_TO_PERSON.get(message.chat.id)
    if not person_key:
        await message.answer("Сначала выбери, кто ты — нажми /start и выбери имя.")
        return
    person = PEOPLE[person_key]
    await message.answer("Ищу актуальные варианты, секунду…")
    text = await build_schedule_text(person)
    await message.answer(text, reply_markup=refresh_keyboard())


@router.message(F.text.func(lambda t: person_by_name(t) is not None))
async def handle_name_choice(message: Message) -> None:
    person = person_by_name(message.text)
    CHAT_TO_PERSON[message.chat.id] = person.key
    await message.answer(f"Отлично, {person.display_name}! Ищу актуальные варианты, секунду…")
    text = await build_schedule_text(person)
    await message.answer(text, reply_markup=refresh_keyboard())


@router.callback_query(F.data == "refresh")
async def handle_refresh(callback: CallbackQuery) -> None:
    person_key = CHAT_TO_PERSON.get(callback.message.chat.id)
    if not person_key:
        await callback.answer("Сначала выбери, кто ты, через /start", show_alert=True)
        return
    person = PEOPLE[person_key]
    await callback.answer("Обновляю…")
    text = await build_schedule_text(person)
    await callback.message.answer(text, reply_markup=refresh_keyboard())


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
