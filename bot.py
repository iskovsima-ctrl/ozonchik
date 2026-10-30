import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["8876687619:AAF5roPChRSMwPDLaZFmmL0aP0Gowb56s6g"]
OZON_CLIENT_ID = os.environ["3964041"]
OZON_API_KEY = os.environ["94b93451-a150-4dbb-a6ac-a0403ca2ae95"]

# --- АВТОРИЗАЦИЯ ---
ADMIN_ID =  int(os.environ.get("ADMIN_ID", "6354368621"))  # ID разработчика/админа
# ALLOWED_USERS — список ID через запятую, например: "123456789,987654321"
_allowed_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS = set()
if _allowed_raw:
    for uid in _allowed_raw.split(","):
        uid = uid.strip()
        if uid:
            try:
                ALLOWED_USERS.add(int(uid))
            except ValueError:
                pass

OZON_URL = "https://api-seller.ozon.ru/v3/posting/fbs/list"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

SHIPPED_STATUSES = {
    "delivering",
    "delivered",
    "driver_pickup",
    "returned",
    "sent_by_seller",
    "not_accepted",
}

keyboard = ReplyKeyboardMarkup(
    [
        ["📦 Отгрузки за дату"],
        ["📈 Сводка по дням"],
    ],
    resize_keyboard=True
)

# ---------- авторизация ----------

def _is_authorized(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in ALLOWED_USERS


async def _check_auth(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        return False
    if _is_authorized(user_id):
        return True
    # Уведомляем админа о попытке доступа
    if ADMIN_ID:
        username = update.effective_user.username or "—"
        first_name = update.effective_user.first_name or "—"
        try:
            await update.get_bot().send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Попытка доступа\n"
                     f"ID: {user_id}\n"
                     f"Имя: {first_name}\n"
                     f"Юзернейм: @{username}\n\n"
                     f"Чтобы дать доступ: /adduser {user_id}"
            )
        except Exception:
            pass
    await update.message.reply_text("❌ У вас нет доступа к этому боту.")
    return False


# ---------- ядро ----------

def _parse_api_datetime(dt_str: str):
    if not dt_str or dt_str in ("null", "None", ""):
        return None
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MOSCOW_TZ)
        return dt.astimezone(MOSCOW_TZ)
    except Exception:
        return None


def _is_cancelled_before_ship(posting: dict) -> bool:
    status = posting.get("status", "")
    if status not in ("cancelled", "cancelled_by_client"):
        return False
    cancellation = posting.get("cancellation") or {}
    if cancellation.get("cancelled_after_ship") is True:
        return False
    return True


def _get_ship_date(posting: dict) -> tuple:
    status = posting.get("status", "")
    dd = _parse_api_datetime(posting.get("delivering_date"))
    if dd:
        return dd, "delivering_date"
    pv = _parse_api_datetime(posting.get("pickup_code_verified_at"))
    if pv:
        return pv, "pickup_code_verified_at"
    su = _parse_api_datetime(posting.get("substatus_updated_at"))
    if su and status in SHIPPED_STATUSES:
        return su, "substatus_updated_at"
    if status in SHIPPED_STATUSES:
        sd = _parse_api_datetime(posting.get("shipment_date"))
        if sd:
            return sd, "shipment_date_fallback"
    return None, "none"


def _headers():
    return {
        "Client-Id": str(OZON_CLIENT_ID),
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def _fetch_postings_in_range(since_dt: datetime, to_dt: datetime) -> list:
    since = since_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    to = to_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    all_postings = []
    offset = 0
    limit = 1000
    while True:
        payload = {
            "dir": "ASC",
            "filter": {"since": since, "to": to},
            "limit": limit,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        resp = requests.post(OZON_URL, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        all_postings.extend(result.get("postings", []))
        if not result.get("has_next", False):
            break
        offset += limit
    return all_postings


def _filter_postings_by_ship_date(postings: list, target_date_msk: datetime) -> list:
    target_next = target_date_msk + timedelta(days=1)
    matched = []
    for posting in postings:
        if _is_cancelled_before_ship(posting):
            continue
        ship_dt, _ = _get_ship_date(posting)
        if ship_dt is None:
            continue
        if target_date_msk.date() <= ship_dt.date() < target_next.date():
            matched.append(posting)
    return matched


# ---------- публичные функции ----------

def get_postings_for_day(date_str: str) -> int:
    naive = datetime.strptime(date_str, "%d.%m.%Y")
    target = naive.replace(tzinfo=MOSCOW_TZ)
    postings = _fetch_postings_in_range(target - timedelta(days=30), target + timedelta(days=1))
    return len(_filter_postings_by_ship_date(postings, target))


def get_all_report(date_from_str: str, date_to_str: str) -> list:
    start = datetime.strptime(date_from_str, "%d.%m.%Y").replace(tzinfo=MOSCOW_TZ)
    end = datetime.strptime(date_to_str, "%d.%m.%Y").replace(tzinfo=MOSCOW_TZ)
    if end < start:
        start, end = end, start
    since = start - timedelta(days=30)
    to = end + timedelta(days=1)
    all_postings = _fetch_postings_in_range(since, to)
    daily = {}
    cur = start
    while cur <= end:
        daily[cur.date()] = 0
        cur += timedelta(days=1)
    for posting in all_postings:
        if _is_cancelled_before_ship(posting):
            continue
        ship_dt, _ = _get_ship_date(posting)
        if ship_dt is None:
            continue
        d = ship_dt.date()
        if start.date() <= d <= end.date():
            daily[d] = daily.get(d, 0) + 1
    results = []
    cur = start
    while cur <= end:
        results.append((cur.strftime("%d.%m.%Y"), daily.get(cur.date(), 0)))
        cur += timedelta(days=1)
    return results


# ---------- хендлеры ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_auth(update):
        return
    await update.message.reply_text(
        "Добрый день! Это статистика отгрузок Ozon. Команды /date и /all писать не обязательно, можно сразу дату. \n\n"
        "Команды:\n"
        "• /date 01.01.2026 — отгрузки за день\n"
        "• /all 01.01.2026-12.12.2026 — сводка по дням",
        reply_markup=keyboard,
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_auth(update):
        return
    text = (update.message.text or "").strip()

    if text == "📦 Отгрузки за дату":
        await update.message.reply_text("Введи дату, например: 01.01.2026")
        return
    if text == "📈 Сводка по дням":
        await update.message.reply_text("Введи диапазон, например: 01.01.2026-12.12.2026")
        return

    # Просто дата — отгрузки за день
    try:
        datetime.strptime(text, "%d.%m.%Y")
        await update.message.reply_text("⏳ Запрашиваю данные Ozon...")
        count = await asyncio.to_thread(get_postings_for_day, text)
        await update.message.reply_text(f"📦 Отгрузки за {text}\n\nВсего заказов: {count}")
        return
    except ValueError:
        pass

    # Диапазон через кнопку
    if "-" in text and text.count(".") >= 4:
        parts = text.split("-")
        if len(parts) == 2:
            try:
                datetime.strptime(parts[0].strip(), "%d.%m.%Y")
                datetime.strptime(parts[1].strip(), "%d.%m.%Y")
                await _handle_all(update, parts[0].strip(), parts[1].strip())
                return
            except ValueError:
                pass

    await update.message.reply_text(
        "Не понял. Примеры:\n"
        "• /date 01.01.2026\n"
        "• /all 01.01.2026-12.12.2026"
    )


async def date_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Укажи дату: /date 01.01.2026")
        return
    date_str = context.args[0].strip()
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text("Неверный формат. Пример: /date 01.01.2026")
        return

    await update.message.reply_text("⏳ Запрашиваю данные Ozon...")
    try:
        count = await asyncio.to_thread(get_postings_for_day, date_str)
        await update.message.reply_text(f"📦 Отгрузки за {date_str}\n\nВсего заказов: {count}")
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "unknown")
        await update.message.reply_text(f"❌ Ошибка Ozon API ({status})")
    except Exception:
        await update.message.reply_text("❌ Ошибка. Посмотри логи.")


async def _handle_all(update: Update, date_from: str, date_to: str):
    await update.message.reply_text(f"⏳ Считаю отгрузки с {date_from} по {date_to}...")
    try:
        results = await asyncio.to_thread(get_all_report, date_from, date_to)
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "unknown")
        await update.message.reply_text(f"❌ Ошибка Ozon API ({status})")
        return
    except Exception:
        await update.message.reply_text("❌ Ошибка. Посмотри логи.")
        return

    lines = []
    total = 0
    for d, c in results:
        lines.append(f"{d} — {c}")
        total += c
    lines.append(f"\n📊 Итого: {total}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (обрезано)"
    await update.message.reply_text(text)


async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Укажи диапазон: /all 01.01.2026-12.12.2026")
        return
    raw = context.args[0].strip()
    parts = raw.split("-")
    if len(parts) != 2:
        await update.message.reply_text("Неверный формат. Пример: /all 01.01.2026-12.12.2026")
        return
    try:
        datetime.strptime(parts[0].strip(), "%d.%m.%Y")
        datetime.strptime(parts[1].strip(), "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text("Неверный формат дат.")
        return
    await _handle_all(update, parts[0].strip(), parts[1].strip())


# ---------- управление доступом (только админ) ----------

async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Только администратор может добавлять пользователей.")
        return
    if not context.args:
        await update.message.reply_text("Укажи ID пользователя: /adduser 123456789")
        return
    try:
        new_id = int(context.args[0].strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    ALLOWED_USERS.add(new_id)
    await update.message.reply_text(f"✅ Пользователь {new_id} добавлен в белый список.\n\n"
                                    f"Текущий список: {sorted(ALLOWED_USERS)}")


async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Только администратор может удалять пользователей.")
        return
    if not context.args:
        await update.message.reply_text("Укажи ID пользователя: /removeuser 123456789")
        return
    try:
        rem_id = int(context.args[0].strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    if rem_id in ALLOWED_USERS:
        ALLOWED_USERS.discard(rem_id)
        await update.message.reply_text(f"✅ Пользователь {rem_id} удалён.\n\n"
                                        f"Текущий список: {sorted(ALLOWED_USERS)}")
    else:
        await update.message.reply_text(f"Пользователь {rem_id} не найден в списке.")


async def listusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Только администратор может смотреть список.")
        return
    users = sorted(ALLOWED_USERS)
    if not users:
        await update.message.reply_text("Список пуст. Только админ имеет доступ.")
        return
    await update.message.reply_text(f"👥 Разрешённые пользователи:\n" + "\n".join(f"• {uid}" for uid in users))


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любой пользователь может узнать свой ID."""
    user_id = update.effective_user.id if update.effective_user else "неизвестно"
    await update.message.reply_text(f"🆔 Твой Telegram ID: {user_id}")


# ---------- main ----------

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("date", date_command))
    app.add_handler(CommandHandler("all", all_command))
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("listusers", listusers_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.run_polling()


if __name__ == "__main__":
    main()