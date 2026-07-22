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
from aiogram.exceptions import TelegramBadRequest
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
    # Справочная информация (примерная, не из API) — показывается в скобках
    # рядом с названием маршрута, чтобы сразу было понятно, чего ждать.
    transfer_hint: Optional[str] = None  # где обычно пересадка
    approx_duration: str = ""  # сколько примерно лететь суммарно
    approx_arrival: str = ""  # когда примерно приземление


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
            Route(
                MOSCOW, "Москва", LONDON, "Лондон",
                transfer_hint="обычно через Стамбул, Белград или Ереван",
                approx_duration="~10–13 ч в пути суммарно",
                approx_arrival="прилёт вечером или ночью того же/следующего дня",
            ),
            Route(
                SPB, "Санкт-Петербург", LONDON, "Лондон",
                transfer_hint="обычно через Стамбул, Белград или Ереван",
                approx_duration="~10–13 ч в пути суммарно",
                approx_arrival="прилёт вечером или ночью того же/следующего дня",
            ),
        ),
    ),
    "darya": Person(
        key="darya",
        display_name="Дарья",
        routes=(
            Route(
                MOSCOW, "Москва", CORFU, "Корфу",
                transfer_hint="обычно через Стамбул или Афины",
                approx_duration="~7–10 ч в пути суммарно",
                approx_arrival="прилёт днём или вечером того же дня",
            ),
            Route(
                SPB, "Санкт-Петербург", CORFU, "Корфу",
                transfer_hint="обычно через Стамбул или Афины",
                approx_duration="~7–10 ч в пути суммарно",
                approx_arrival="прилёт днём или вечером того же дня",
            ),
        ),
    ),
    "valeria": Person(
        key="valeria",
        display_name="Валерия",
        routes=(
            Route(
                MOSCOW, "Москва", BANGKOK, "Бангкок",
                transfer_hint=None,  # прямой рейс, пересадки нет
                approx_duration="~9 ч в пути (прямой рейс)",
                approx_arrival="прилёт ранним утром по местному времени, на следующие сутки",
            ),
            Route(
                SPB, "Санкт-Петербург", BANGKOK, "Бангкок",
                transfer_hint="обычно через Москву (прямых рейсов из СПб нет)",
                approx_duration="~11–13 ч в пути суммарно",
                approx_arrival="прилёт утром по местному времени, на следующие сутки",
            ),
        ),
    ),
    "maxim": Person(
        key="maxim",
        display_name="Максим",
        routes=(
            Route(
                MOSCOW, "Москва", BERLIN, "Берлин",
                transfer_hint="обычно через Стамбул или Белград",
                approx_duration="~9–12 ч в пути суммарно",
                approx_arrival="прилёт вечером того же дня или ночью",
            ),
            Route(
                SPB, "Санкт-Петербург", BERLIN, "Берлин",
                transfer_hint="обычно через Стамбул или Белград",
                approx_duration="~9–12 ч в пути суммарно",
                approx_arrival="прилёт вечером того же дня или ночью",
            ),
            Route(
                KRASNODAR, "Краснодар", BERLIN, "Берлин",
                transfer_hint="обычно через Стамбул или Москву",
                approx_duration="~10–13 ч в пути суммарно",
                approx_arrival="прилёт вечером того же дня или ночью",
            ),
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
# ЗАПРОСЫ К TRAVELPAYOUTS (Aviasales GraphQL Flight Data API)
# ---------------------------------------------------------------------------
# ВАЖНО: REST-эндпоинт v2/prices/latest отдаёт только ДАТУ вылета, без
# времени — поэтому раньше бот всегда писал "время уточняется". Точное
# время вылета (departure_at) и продолжительность полёта (trip_duration)
# отдаёт только GraphQL-эндпоинт, поэтому используем именно его.

GRAPHQL_URL = "https://api.travelpayouts.com/graphql/v1/query"

# Поля, которые пробуем запросить сначала (расширенный набор).
FIELDS_FULL = "departure_at value trip_duration number_of_changes airline"
# Если запрос с currency почему-то падает, пробуем те же поля без currency —
# так авиакомпания и длительность полёта не теряются зря.
# Если API не знает какое-то из полей выше, откатываемся на минимальный набор,
# который точно подтверждён документацией Travelpayouts (последний резерв).
FIELDS_BASIC = "departure_at value trip_duration"


def _month_starts(start: date, end: date) -> list[str]:
    """Список первых чисел месяцев в диапазоне [start, end] в формате YYYY-MM-01."""
    months = []
    cursor = start.replace(day=1)
    end_marker = end.replace(day=1)
    while cursor <= end_marker:
        months.append(cursor.strftime("%Y-%m-01"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return months


def _build_query(origin: str, destination: str, depart_month: str, fields: str, with_currency: bool) -> str:
    currency_line = f'currency: "{CURRENCY}"' if with_currency else ""
    return f"""
{{
  prices_one_way(
    params: {{
      origin: "{origin}"
      destination: "{destination}"
      depart_months: "{depart_month}"
      {currency_line}
    }}
    paging: {{ limit: 30, offset: 0 }}
    sorting: VALUE_ASC
  ) {{
    {fields}
  }}
}}
"""


async def _graphql_request(session: aiohttp.ClientSession, query: str) -> Optional[dict]:
    headers = {"Content-Type": "application/json", "X-Access-Token": TRAVELPAYOUTS_TOKEN}
    try:
        async with session.post(GRAPHQL_URL, json={"query": query}, headers=headers, timeout=20) as resp:
            data = await resp.json()
            if resp.status != 200:
                log.warning("GraphQL вернул статус %s: %s", resp.status, data)
                return None
            return data
    except Exception as exc:  # noqa: BLE001
        log.error("Ошибка GraphQL-запроса к Travelpayouts: %s", exc)
        return None


def _parse_departure(item: dict) -> Optional[datetime]:
    dep_at = item.get("departure_at")
    if not dep_at:
        return None
    try:
        # departure_at приходит вида "2026-08-05T06:35:00+03:00"
        return datetime.fromisoformat(dep_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_night_hour(hour: int) -> bool:
    return hour >= 22 or hour < 6


async def fetch_flights_for_route(session: aiohttp.ClientSession, route: Route) -> list[dict]:
    """
    Возвращает найденные варианты перелёта по маршруту с реальным временем
    вылета, отсортированные по дате, с датой вылета не позднее DEADLINE.

    Данные — из кэша Aviasales (реально найденные билеты за недавнее время),
    а не «расписание вообще всех рейсов» — если по каким-то датам никто
    ничего не искал/покупал, по ним не будет данных вовсе.
    """
    today = datetime.utcnow().date()
    months = _month_starts(today, DEADLINE)

    all_items: list[dict] = []
    for month in months:
        # Попытка 1: расширенные поля + явная валюта
        query = _build_query(route.origin, route.destination, month, FIELDS_FULL, with_currency=True)
        data = await _graphql_request(session, query)

        if data is None or data.get("errors"):
            # Попытка 2: те же расширенные поля (авиакомпания, длительность,
            # пересадки), но без currency — вдруг проблема была именно в нём.
            query = _build_query(route.origin, route.destination, month, FIELDS_FULL, with_currency=False)
            data = await _graphql_request(session, query)

        if data is None or data.get("errors"):
            # Попытка 3 (последний резерв): минимальный набор полей —
            # ровно как в официальном примере документации Travelpayouts.
            # Тут уже не будет airline/number_of_changes.
            query = _build_query(route.origin, route.destination, month, FIELDS_BASIC, with_currency=False)
            data = await _graphql_request(session, query)

        if not data:
            continue
        if data.get("errors"):
            log.warning("GraphQL errors для %s->%s (%s): %s", route.origin, route.destination, month, data["errors"])
            continue

        items = (data.get("data") or {}).get("prices_one_way") or []
        all_items.extend(items)

    filtered = []
    for item in all_items:
        dep_dt = _parse_departure(item)
        if dep_dt is None:
            continue
        if dep_dt.date() <= DEADLINE:
            filtered.append(item)

    filtered.sort(key=lambda x: (_parse_departure(x) or datetime.max))
    return filtered


def format_flight_item(item: dict, route: Route) -> str:
    """
    Красивая карточка рейса:

    📅 05.08.2026
    🛫 06:35 -- 🛬 14:20 (+1д.)
    ⏱ 8ч 45м
    💵 45000 RUB
    ✈️ Turkish Airlines · 1 пересадка

    Если API не отдал точное время прилёта/длительность (так бывает —
    Aviasales отдаёт их не для каждого найденного билета), вместо
    "уточняется" подставляем ПРИМЕРНЫЕ данные маршрута (route.approx_*),
    которые заданы вручную в PEOPLE — чтобы цифры были всегда, а не прочерк.
    """
    dep_dt = _parse_departure(item)
    price = item.get("value", "?")
    airline = item.get("airline") or "уточняется"
    duration_min = item.get("trip_duration")
    transfers = item.get("number_of_changes")

    if transfers is None:
        transfers_txt = ""
    elif transfers == 0:
        transfers_txt = "прямой рейс"
    else:
        transfers_txt = f"{transfers} пересадк{'а' if transfers == 1 else 'и'}"

    approx_arrival_txt = route.approx_arrival or "время прилёта уточняется"
    approx_duration_txt = route.approx_duration or "длительность уточняется"

    if dep_dt is None:
        block = [
            "📅 дата уточняется",
            f"💵 {price} {CURRENCY.upper()}",
            f"✈️ {airline}",
        ]
        return "\n".join(block)

    has_time = item.get("departure_at") is not None
    date_part = dep_dt.strftime("%d.%m.%Y")

    lines = [f"📅 <b>{date_part}</b>"]
    night_markers = []

    if has_time:
        dep_time_txt = dep_dt.strftime("%H:%M")
        if _is_night_hour(dep_dt.hour):
            night_markers.append("вылет ночью")

        if duration_min:
            try:
                duration_min = int(duration_min)
                arrival_dt = dep_dt + timedelta(minutes=duration_min)
                arr_time_txt = arrival_dt.strftime("%H:%M")

                # если прилёт на следующие сутки — отметим это
                day_shift = (arrival_dt.date() - dep_dt.date()).days
                day_note = f" (+{day_shift}д.)" if day_shift else ""

                lines.append(f"🛫 {dep_time_txt} -- 🛬 {arr_time_txt}{day_note}")

                if _is_night_hour(arrival_dt.hour):
                    night_markers.append("прилёт ночью")

                hours, minutes = divmod(duration_min, 60)
                duration_txt = f"{hours}ч {minutes:02d}м" if hours else f"{minutes}м"
                lines.append(f"⏱ {duration_txt}")
            except (TypeError, ValueError):
                # Точное время прилёта не посчиталось — подставляем примерное
                lines.append(f"🛫 {dep_time_txt} -- 🛬 {approx_arrival_txt} (примерно)")
                lines.append(f"⏱ {approx_duration_txt} (примерно)")
        else:
            # API не отдал trip_duration для этого билета — подставляем примерное
            lines.append(f"🛫 {dep_time_txt} -- 🛬 {approx_arrival_txt} (примерно)")
            lines.append(f"⏱ {approx_duration_txt} (примерно)")
    else:
        # API не отдал даже время вылета (только дату) — подставляем примерное
        lines.append(f"🛫 время вылета уточняется -- 🛬 {approx_arrival_txt} (примерно)")
        lines.append(f"⏱ {approx_duration_txt} (примерно)")

    lines.append(f"💵 {price} {CURRENCY.upper()}")

    extra = [f"✈️ {airline}"]
    if transfers_txt:
        extra.append(transfers_txt)
    lines.append(" · ".join(extra))

    if night_markers:
        lines.append("🌙 " + ", ".join(night_markers))

    if route.transfer_hint:
        lines.append(f"🛩 {route.transfer_hint}")
    else:
        lines.append("🛩 прямой рейс")

    return "\n".join(lines)


async def build_schedule_text(person: Person) -> str:
    lines = [
        f"<b>Расписание для {person.display_name}</b>",
        f"(даты вылета до {DEADLINE.strftime('%d.%m.%Y')} включительно)\n",
    ]

    async with aiohttp.ClientSession() as session:
        for route in person.routes:
            lines.append(f"\n<b>{route.origin_name} ({route.origin}) → {route.destination_name} ({route.destination})</b>")

            hint_parts = []
            if route.transfer_hint:
                hint_parts.append(route.transfer_hint)
            if route.approx_duration:
                hint_parts.append(route.approx_duration)
            if route.approx_arrival:
                hint_parts.append(route.approx_arrival)
            if hint_parts:
                lines.append(f"<i>({'; '.join(hint_parts)})</i>")

            flights = await fetch_flights_for_route(session, route)

            if not flights:
                lines.append("Нет данных / вариантов на эту дату в кэше Aviasales.")
                continue

            for i, item in enumerate(flights[:10]):
                if i > 0:
                    lines.append("➖➖➖➖➖➖➖➖")
                lines.append(format_flight_item(item, route))

    lines.append(
        "\nℹ️ Тут присутствуют не все рейсы, а те, которые зарегистрированы в программе, "
        "но они точные — если нужна определённая дата или время, гуглите сами😋"
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

    chat_id = callback.message.chat.id
    old_message_id = callback.message.message_id

    # Сначала отправляем новое сообщение, потом удаляем старое —
    # так пользователь не остаётся без сообщения, если удаление вдруг не удастся.
    new_message = await callback.bot.send_message(chat_id, text, reply_markup=refresh_keyboard())

    try:
        await callback.bot.delete_message(chat_id, old_message_id)
    except TelegramBadRequest as exc:
        log.warning("Не удалось удалить старое сообщение %s в чате %s: %s", old_message_id, chat_id, exc)

    _ = new_message  # оставлено для наглядности, можно расширить логику при желании


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
