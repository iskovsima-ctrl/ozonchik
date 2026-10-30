import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========================================
# НАСТРОЙКИ И КЛЮЧИ
# ==========================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
OZON_CLIENT_ID = os.environ["OZON_CLIENT_ID"]
OZON_API_KEY = os.environ["OZON_API_KEY"]

# Telegram ID администратора
ADMIN_ID = 6354368621

# Дополнительные пользователи
ALLOWED_USERS = set()

OZON_URL = "https://api-seller.ozon.ru/v4/posting/fbs/list"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

SHIPPED_STATUSES = {
    "delivering",
    "delivered",
    "driver_pickup",
    "returned",
    "sent_by_seller",
    "not_accepted",
}

# ==========================================
# ЛОГИ
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# ==========================================
# КНОПКИ
# ==========================================

keyboard = ReplyKeyboardMarkup(
    [
        ["📦 Отгрузки за дату"],
        ["📈 Сводка по дням"],
    ],
    resize_keyboard=True,
)

# ==========================================
# АВТОРИЗАЦИЯ
# ==========================================

def is_authorized(user_id):
    return user_id == ADMIN_ID or user_id in ALLOWED_USERS


async def check_auth(update):
    user = update.effective_user

    if not user:
        return False

    if is_authorized(user.id):
        return True

    try:
        await update.get_bot().send_message(
            chat_id=ADMIN_ID,
            text=(
                "⚠️ Попытка доступа\n"
                f"ID: {user.id}\n"
                f"Имя: {user.first_name}\n"
                f"Username: @{user.username or '—'}\n\n"
                f"Чтобы дать доступ: /adduser {user.id}"
            ),
        )
    except Exception:
        logger.exception("Не удалось уведомить администратора")

    if update.message:
        await update.message.reply_text(
            "❌ У вас нет доступа к этому боту."
        )

    return False


# ==========================================
# ДАТЫ
# ==========================================

def parse_api_datetime(dt_str):
    if not dt_str or dt_str in ("null", "None", ""):
        return None

    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"

        dt = datetime.fromisoformat(dt_str)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MOSCOW_TZ)

        return dt.astimezone(MOSCOW_TZ)

    except (ValueError, TypeError):
        return None


def is_cancelled_before_ship(posting):
    status = posting.get("status", "")

    if status not in ("cancelled", "cancelled_by_client"):
        return False

    cancellation = posting.get("cancellation") or {}

    return cancellation.get("cancelled_after_ship") is not True


def get_ship_date(posting):
    status = posting.get("status", "")

    fields = [
        "delivering_date",
        "pickup_code_verified_at",
        "substatus_updated_at",
        "shipment_date",
    ]

    for field in fields:
        value = parse_api_datetime(posting.get(field))

        if value:
            if field in ("substatus_updated_at", "shipment_date"):
                if status not in SHIPPED_STATUSES:
                    continue

            return value

    return None


# ==========================================
# OZON API
# ==========================================

def get_headers():
    return {
        "Client-Id": str(OZON_CLIENT_ID),
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def fetch_postings(since_dt, to_dt):
    """
    Получает все FBS-отправления за период через Ozon API v4.
    Используется cursor-пагинация.
    """

    since = (
        since_dt.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    to = (
        to_dt.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    all_postings = []
    cursor = None
    limit = 100

    while True:
        payload = {
            "dir": "ASC",
            "filter": {
                "since": since,
                "to": to,
            },
            "limit": limit,
            "with": {
                "analytics_data": False,
                "financial_data": False,
            },
        }

        if cursor:
            payload["cursor"] = cursor

        response = requests.post(
            OZON_URL,
            headers=get_headers(),
            json=payload,
            timeout=60,
        )

        if not response.ok:
            logger.error(
                "Ozon API error %s: %s",
                response.status_code,
                response.text[:1000],
            )

        response.raise_for_status()

        data = response.json()

        # В API v4 postings находятся на верхнем уровне,
        # а не внутри объекта result.
        postings = data.get("postings", [])

        if not isinstance(postings, list):
            raise RuntimeError(
                "Ozon API вернул неожиданный формат поля postings"
            )

        all_postings.extend(postings)

        if not data.get("has_next", False):
            break

        next_cursor = data.get("cursor")

        if not next_cursor:
            raise RuntimeError(
                "Ozon API сообщил has_next=true, "
                "но не вернул cursor"
            )

        if next_cursor == cursor:
            raise RuntimeError(
                "Ozon API вернул повторяющийся cursor"
            )

        cursor = next_cursor

    logger.info(
        "Ozon API: загружено отправлений: %s",
        len(all_postings),
    )

    return all_postings


# ==========================================
# СТАТИСТИКА
# ==========================================

def filter_postings(postings, target_date):
    matched = []

    for posting in postings:
        if is_cancelled_before_ship(posting):
            continue

        ship_date = get_ship_date(posting)

        if not ship_date:
            continue

        if ship_date.date() == target_date.date():
            matched.append(posting)

    return matched


def get_postings_for_day(date_str):
    naive = datetime.strptime(date_str, "%d.%m.%Y")
    target = naive.replace(tzinfo=MOSCOW_TZ)

    postings = fetch_postings(
        target - timedelta(days=30),
        target + timedelta(days=1),
    )

    return len(filter_postings(postings, target))


def get_all_report(date_from_str, date_to_str):
    start = datetime.strptime(
        date_from_str,
        "%d.%m.%Y",
    ).replace(tzinfo=MOSCOW_TZ)

    end = datetime.strptime(
        date_to_str,
        "%d.%m.%Y",
    ).replace(tzinfo=MOSCOW_TZ)

    if end < start:
        start, end = end, start

    # API Ozon ограничивает период одним годом.
    if (end - start).days > 365:
        raise ValueError(
            "Период отчёта не должен превышать один год."
        )

    postings = fetch_postings(
        start - timedelta(days=30),
        end + timedelta(days=1),
    )

    daily = {}
    current = start

    while current.date() <= end.date():
        daily[current.date()] = 0
        current += timedelta(days=1)

    for posting in postings:
        if is_cancelled_before_ship(posting):
            continue

        ship_date = get_ship_date(posting)

        if not ship_date:
            continue

        day = ship_date.date()

        if start.date() <= day <= end.date():
            daily[day] = daily.get(day, 0) + 1

    results = []
    current = start

    while current.date() <= end.date():
        results.append(
            (
                current.strftime("%d.%m.%Y"),
                daily.get(current.date(), 0),
            )
        )

        current += timedelta(days=1)

    return results


# ==========================================
# ОБРАБОТКА ОШИБОК OZON
# ==========================================

def get_api_error(error):
    response = getattr(error, "response", None)

    if response is None:
        return str(error) or "Неизвестная ошибка"

    try:
        return (
            f"HTTP {response.status_code}\n"
            f"{response.text[:1500]}"
        )
    except Exception:
        return str(error)


# ==========================================
# TELEGRAM: СТАРТ
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    await update.message.reply_text(
        "Добрый день! 📦\n\n"
        "Это бот статистики отгрузок Ozon.\n\n"
        "Команды:\n"
        "/date 30.10.2026 — отгрузки за день\n"
        "/all 01.10.2026-30.10.2026 — сводка\n"
        "/myid — узнать свой ID",
        reply_markup=keyboard,
    )


# ==========================================
# TELEGRAM: ОТЧЁТ ЗА ДЕНЬ
# ==========================================

async def date_command(update, context):
    if not await check_auth(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Укажи дату: /date 30.10.2026"
        )
        return

    date_str = context.args[0]

    try:
        datetime.strptime(date_str, "%d.%m.%Y")

        await update.message.reply_text(
            "⏳ Запрашиваю данные Ozon..."
        )

        count = await asyncio.to_thread(
            get_postings_for_day,
            date_str,
        )

        await update.message.reply_text(
            f"📦 Отгрузки за {date_str}\n\n"
            f"Всего заказов: {count}"
        )

    except requests.HTTPError as error:
        logger.exception("Ozon API HTTP error")

        await update.message.reply_text(
            "❌ Ошибка Ozon API\n"
            f"{get_api_error(error)}"
        )

    except Exception as error:
        logger.exception("Date command error")

        await update.message.reply_text(
            f"❌ Ошибка: {str(error)[:1000]}"
        )


# ==========================================
# TELEGRAM: СВОДКА ЗА ПЕРИОД
# ==========================================

async def all_command(update, context):
    if not await check_auth(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Укажи диапазон:\n"
            "/all 01.10.2026-30.10.2026"
        )
        return

    parts = context.args[0].split("-")

    if len(parts) != 2:
        await update.message.reply_text(
            "Неверный формат дат.\n"
            "Пример: /all 01.10.2026-30.10.2026"
        )
        return

    await handle_all(
        update,
        parts[0],
        parts[1],
    )


async def handle_all(update, date_from, date_to):
    try:
        await update.message.reply_text(
            f"⏳ Считаю отгрузки с {date_from} по {date_to}..."
        )

        results = await asyncio.to_thread(
            get_all_report,
            date_from,
            date_to,
        )

        lines = []
        total = 0

        for day, count in results:
            lines.append(f"{day} — {count}")
            total += count

        lines.append(f"\n📊 Итого: {total}")

        report_text = "\n".join(lines)

        if len(report_text) > 4000:
            report_text = report_text[:3900] + "\n... (обрезано)"

        await update.message.reply_text(report_text)

    except requests.HTTPError as error:
        logger.exception("Ozon API HTTP error")

        await update.message.reply_text(
            "❌ Ошибка Ozon API\n"
            f"{get_api_error(error)}"
        )

    except Exception as error:
        logger.exception("Report error")

        await update.message.reply_text(
            f"❌ Ошибка: {str(error)[:1000]}"
        )


# ==========================================
# TELEGRAM: ОБЫЧНЫЕ СООБЩЕНИЯ И КНОПКИ
# ==========================================

async def message_handler(update, context):
    if not await check_auth(update):
        return

    text = (update.message.text or "").strip()

    if text == "📦 Отгрузки за дату":
        await update.message.reply_text(
            "Введи дату, например: 30.10.2026"
        )
        return

    if text == "📈 Сводка по дням":
        await update.message.reply_text(
            "Введи диапазон, например:\n"
            "01.10.2026-30.10.2026"
        )
        return

    try:
        datetime.strptime(text, "%d.%m.%Y")

        await update.message.reply_text(
            "⏳ Запрашиваю данные Ozon..."
        )

        count = await asyncio.to_thread(
            get_postings_for_day,
            text,
        )

        await update.message.reply_text(
            f"📦 Отгрузки за {text}\n\n"
            f"Всего заказов: {count}"
        )

        return

    except ValueError:
        pass

    if "-" in text and text.count(".") >= 4:
        parts = text.split("-")

        if len(parts) == 2:
            try:
                date_from = parts[0].strip()
                date_to = parts[1].strip()

                datetime.strptime(date_from, "%d.%m.%Y")
                datetime.strptime(date_to, "%d.%m.%Y")

                await handle_all(
                    update,
                    date_from,
                    date_to,
                )

                return

            except ValueError:
                pass

    await update.message.reply_text(
        "Не понял дату.\n"
        "Пример: 30.10.2026\n"
        "Или диапазон: 01.10.2026-30.10.2026"
    )


# ==========================================
# УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# ==========================================

async def adduser_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Только администратор может добавлять пользователей."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /adduser 123456789"
        )
        return

    try:
        user_id = int(context.args[0])
        ALLOWED_USERS.add(user_id)

        await update.message.reply_text(
            f"✅ Пользователь {user_id} добавлен."
        )

    except ValueError:
        await update.message.reply_text(
            "ID должен быть числом."
        )


async def removeuser_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Только администратор может удалять пользователей."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /removeuser 123456789"
        )
        return

    try:
        user_id = int(context.args[0])
        ALLOWED_USERS.discard(user_id)

        await update.message.reply_text(
            f"✅ Пользователь {user_id} удалён."
        )

    except ValueError:
        await update.message.reply_text(
            "ID должен быть числом."
        )


async def listusers_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Только администратор может смотреть список."
        )
        return

    if not ALLOWED_USERS:
        await update.message.reply_text(
            "Список пользователей пуст."
        )
        return

    await update.message.reply_text(
        "👥 Разрешённые пользователи:\n"
        + "\n".join(
            f"• {uid}" for uid in sorted(ALLOWED_USERS)
        )
    )


async def myid_command(update, context):
    user = update.effective_user

    if not user:
        return

    await update.message.reply_text(
        f"🆔 Твой Telegram ID: {user.id}"
    )


# ==========================================
# ЗАПУСК БОТА
# ==========================================

def main():
    required_variables = [
        "BOT_TOKEN",
        "OZON_CLIENT_ID",
        "OZON_API_KEY",
    ]

    missing = [
        name for name in required_variables
        if not os.environ.get(name)
    ]

    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("date", date_command))
    app.add_handler(CommandHandler("all", all_command))

    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("listusers", listusers_command))
    app.add_handler(CommandHandler("myid", myid_command))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    logger.info("Telegram bot is starting...")

    app.run_polling()


if __name__ == "__main__":
    main()