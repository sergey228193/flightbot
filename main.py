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
from datetime import date, datetime, timedelta
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_БОТА")
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_TRAVELPAYOUTS")

# Валюта отображения цен
CURRENCY = "rub"

# Крайняя дата, до которой ищем рейсы (включительно)
DEADLINE = date(2026, 8, 31)

# Ежедневная автоматическая рассылка расписания (время по Москве)
DAILY_UPDATE_HOUR = 9
DAILY_UPDATE_MINUTE = 45
TIMEZONE = "Europe/Moscow"

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
# (например, после каждого передеплоя на Railway) эта привязка теряется,
# и ежедневная рассылка отправляться не будет, пока человек не напишет
# /start и не выберет своё имя ещё раз. Для надёжного хранения между
# перезапусками нужно заменить на файл/БД — могу добавить по запросу.
CHAT_TO_PERSON: dict[int, str] = {}


# ---------------------------------------------------------------------------
# ЗАПРОСЫ К TRAVELPAYOUTS (Aviasales Data API)
# ---------------------------------------------------------------------------

TP_CALENDAR_URL = "https://api.travelpayouts.com/v2/prices/latest"


def _parse_departure(item: dict) -> Optional[datetime]:
    """
    Пытается получить дату+время вылета. API отдаёт либо полный ISO-таймстамп
    в departure_at, либо просто дату в depart_date (без времени) — тогда
    время неизвестно.
    """
    dep_at = item.get("departure_at")
    if dep_at:
        try:
            # departure_at обычно вида "2026-08-05T06:35:00+03:00"
            return datetime.fromisoformat(dep_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    dep_date_str = item.get("depart_date")
    if dep_date_str:
        try:
            return datetime.strptime(dep_date_str, "%Y-%m-%d")
        except ValueError:
            return None
    return None


def _is_night_hour(hour: int) -> bool:
    return hour >= 22 or hour < 6


async def fetch_flights_for_route(session: aiohttp.ClientSession, route: Route) -> list[dict]:
    """
    Возвращает список найденных вариантов перелёта по маршруту, с датой
    вылета не позднее DEADLINE. Данные — из кэша Aviasales (реальные цены
    и билеты, которые люди недавно искали/покупали) — это не «расписание
    вообще всех рейсов на каждый день», а срез актуальных доступных
    вариантов с реальным временем вылета/прилёта, если оно известно.
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
        dep_dt = _parse_departure(item)
        if dep_dt is None:
            continue
        if dep_dt.date() <= DEADLINE:
            filtered.append(item)

    filtered.sort(key=lambda x: (_parse_departure(x) or datetime.max))
    return filtered


def format_flight_item(item: dict) -> str:
    dep_dt = _parse_departure(item)
    price = item.get("value", "?")
    airline = item.get("airline", "—")
    duration_min = item.get("duration") or item.get("duration_to")

    transfers = item.get("number_of_changes")
    if transfers is None:
        transfers_txt = ""
    elif transfers == 0:
        transfers_txt = "прямой рейс"
    else:
        transfers_txt = f"{transfers} пересадк{'а' if transfers == 1 else 'и'}"

    if dep_dt is None:
        return f"📅 ? — {price} {CURRENCY.upper()} · ✈️ {airline}"

    has_time = item.get("departure_at") is not None
    date_part = dep_dt.strftime("%d.%m.%Y")

    night_markers = []

    if has_time:
        dep_time_txt = dep_dt.strftime("%H:%M")
        if _is_night_hour(dep_dt.hour):
            night_markers.append("вылет ночью")
        line = f"📅 {date_part} 🛫 {dep_time_txt}"

        if duration_min:
            try:
                duration_min = int(duration_min)
                arrival_dt = dep_dt + timedelta(minutes=duration_min)
                arr_time_txt = arrival_dt.strftime("%H:%M")
                # если прилёт на следующие сутки — отметим это
                day_shift = (arrival_dt.date() - dep_dt.date()).days
                day_note = f" (+{day_shift}д.)" if day_shift else ""
                line += f" → 🛬 {arr_time_txt}{day_note}"
                if _is_night_hour(arrival_dt.hour):
                    night_markers.append("прилёт ночью")

                hours, minutes = divmod(duration_min, 60)
                duration_txt = f"{hours}ч {minutes:02d}м" if hours else f"{minutes}м"
                line += f" · ⏱ {duration_txt}"
            except (TypeError, ValueError):
                pass
        else:
            line += " · ⏱ длительность неизвестна"
    else:
        # Только дата, без точного времени (агрегированные данные)
        line = f"📅 {date_part} · время вылета уточняется на сайте"

    line += f" · {price} {CURRENCY.upper()} · ✈️ {airline}"
    if transfers_txt:
        line += f" · {transfers_txt}"
    if night_markers:
        line += " · 🌙 " + ", ".join(night_markers)

    return line


async def build_schedule_text(person: Person) -> str:
    lines = [
        f"<b>Расписание для {person.display_name}</b>",
        f"(даты вылета до {DEADLINE.strftime('%d.%m.%Y')} включительно)",
        "🛫 вылет · 🛬 прилёт · ⏱ длительность полёта · 🌙 ночной рейс\n",
    ]

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


async def daily_broadcast(bot: Bot) -> None:
    """Отправляет каждому зарегистрированному пользователю свежее расписание."""
    log.info("Запуск ежедневной рассылки расписания (%s пользователей)", len(CHAT_TO_PERSON))
    for chat_id, person_key in list(CHAT_TO_PERSON.items()):
        person = PEOPLE.get(person_key)
        if not person:
            continue
        try:
            text = await build_schedule_text(person)
            await bot.send_message(chat_id, "🔔 Ежедневное обновление\n\n" + text, reply_markup=refresh_keyboard())
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось отправить рассылку в чат %s: %s", chat_id, exc)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        daily_broadcast,
        trigger=CronTrigger(hour=DAILY_UPDATE_HOUR, minute=DAILY_UPDATE_MINUTE),
        args=[bot],
    )
    scheduler.start()
    log.info(
        "Планировщик запущен: ежедневная рассылка в %02d:%02d (%s)",
        DAILY_UPDATE_HOUR,
        DAILY_UPDATE_MINUTE,
        TIMEZONE,
    )

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
