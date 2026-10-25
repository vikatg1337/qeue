import asyncio
import os
import random
import secrets
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram import BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден токен! Проверь переменную окружения BOT_TOKEN или файл .env")

# Прокси задаётся только при необходимости через BOT_PROXY в окружении/.env.
# На FadeHost оставь BOT_PROXY пустым, если сервер подключается к Telegram напрямую.
PROXY = os.getenv("BOT_PROXY") or None
session = AiohttpSession(proxy=PROXY)
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

DB_FILE = os.getenv("DB_FILE", "queues.db")
ADMIN_IDS = {
    1053596260,
    942790183,
    1214767790
}
LEGACY_GROUP_CHAT_ID = -5496142597  # группа старого бота; её данные сохраняются

JOKES = [
    "МЕМЕМЕМЕМЕМЕМЕМЕМЕМЕМЕМЕ",
    "жучило какой-то",
    "...",
    "Скачать вирусы без промоушен",
    "🐸 Кто рано записался, тот рано начал переживать",
    "спасибо моим бета-тестерам что терпят непонятные соо в чате",
    "продам гараж",
    "если у вас есть какие-то обращения или предложения пишите @vikatang",
    "периодически обновляем бота для вашего удобства, поэтому он может не грузить по выходным",
    "этот бот кстати теперь не только у первокурсников",
    "интересный факт. автор любит пропедевтику.",
    "67",
]

# Простые состояния диалога. Они хранятся в памяти до перезапуска бота.
waiting_for_queue_name = set()
waiting_for_queue_capacity = set()
pending_queue_name = {}
waiting_for_change = set()
waiting_for_change_queue = {}
waiting_for_proxy_name = {}       # user_id -> (queue_id, place)
waiting_for_seminar_title = set()
waiting_for_seminar_topics = {}   # user_id -> seminar title


def connect_db():
    db = sqlite3.connect(DB_FILE, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    """Create missing tables and add new columns without deleting existing data."""
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS groups_registry (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                registered_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS group_admins (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_group_selection (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS queues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                places INTEGER NOT NULL DEFAULT 32
            )
        """)
        queue_columns = {row[1] for row in db.execute("PRAGMA table_info(queues)").fetchall()}
        if "places" not in queue_columns:
            db.execute("ALTER TABLE queues ADD COLUMN places INTEGER NOT NULL DEFAULT 32")
        if "group_chat_id" not in queue_columns:
            db.execute("ALTER TABLE queues ADD COLUMN group_chat_id INTEGER")
        seminar_columns = {row[1] for row in db.execute("PRAGMA table_info(seminars)").fetchall()}
        db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                queue_id INTEGER NOT NULL,
                place INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                PRIMARY KEY (queue_id, place),
                UNIQUE (queue_id, user_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                queue_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (queue_id, chat_id, message_id)
            )
        """)

        # Миграция: для записей за другого человека сохраняем ID того, кто сделал запись.
        columns = {row[1] for row in db.execute("PRAGMA table_info(bookings)").fetchall()}
        if "booked_by" not in columns:
            db.execute("ALTER TABLE bookings ADD COLUMN booked_by INTEGER")

        db.execute("""
            CREATE TABLE IF NOT EXISTS seminars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        seminar_columns = {row[1] for row in db.execute("PRAGMA table_info(seminars)").fetchall()}
        if "group_chat_id" not in seminar_columns:
            db.execute("ALTER TABLE seminars ADD COLUMN group_chat_id INTEGER")
        db.execute("""
            CREATE TABLE IF NOT EXISTS seminar_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seminar_id INTEGER NOT NULL,
                topic_name TEXT NOT NULL,
                user_id INTEGER,
                user_name TEXT,
                booked_at TEXT,
                FOREIGN KEY (seminar_id) REFERENCES seminars(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS seminar_announcements (
                seminar_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (seminar_id, chat_id, message_id)
            )
        """)
        # Отдельная таблица записей позволяет нескольким людям выбрать одну тему.
        db.execute("""
            CREATE TABLE IF NOT EXISTS seminar_topic_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                booked_at TEXT NOT NULL,
                UNIQUE(topic_id, user_id),
                FOREIGN KEY (topic_id) REFERENCES seminar_topics(id)
            )
        """)
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_topic_per_user_per_seminar
            ON seminar_topics(seminar_id, user_id) WHERE user_id IS NOT NULL
        """)
        # Старые очереди и семинары остаются в прежней группе; новые получают group_chat_id.
        db.execute("UPDATE queues SET group_chat_id = ? WHERE group_chat_id IS NULL", (LEGACY_GROUP_CHAT_ID,))
        db.execute("UPDATE seminars SET group_chat_id = ? WHERE group_chat_id IS NULL", (LEGACY_GROUP_CHAT_ID,))
        db.execute(
            "INSERT OR IGNORE INTO groups_registry (chat_id, title, registered_by, created_at) VALUES (?, ?, ?, ?)",
            (LEGACY_GROUP_CHAT_ID, "Старая группа (перенесена автоматически)", next(iter(ADMIN_IDS)), datetime.utcnow().isoformat(timespec="seconds"))
        )
        db.execute("INSERT OR IGNORE INTO group_admins (chat_id, user_id) VALUES (?, ?)", (LEGACY_GROUP_CHAT_ID, next(iter(ADMIN_IDS))))
        # Переносим старые записи в новую таблицу, не удаляя их из старой.
        db.execute("""
            INSERT OR IGNORE INTO seminar_topic_bookings (topic_id, user_id, user_name, booked_at)
            SELECT id, user_id, user_name, COALESCE(booked_at, ?)
            FROM seminar_topics WHERE user_id IS NOT NULL
        """, (datetime.utcnow().isoformat(timespec="seconds"),))


def selected_group_id(user_id):
    with connect_db() as db:
        row = db.execute("SELECT chat_id FROM user_group_selection WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return row["chat_id"]
    return LEGACY_GROUP_CHAT_ID


def set_selected_group(user_id, chat_id):
    with connect_db() as db:
        db.execute(
            "INSERT INTO user_group_selection (user_id, chat_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id",
            (user_id, chat_id),
        )


def is_registered_group(chat_id):
    with connect_db() as db:
        return db.execute("SELECT 1 FROM groups_registry WHERE chat_id = ?", (chat_id,)).fetchone() is not None


def user_is_group_admin(user_id, chat_id):
    with connect_db() as db:
        return db.execute(
            "SELECT 1 FROM group_admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone() is not None


async def verify_group_member(user_id, chat_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"creator", "administrator", "member"}
    except Exception:
        return False


async def verify_group_admin(user_id, chat_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"creator", "administrator"}
    except Exception:
        return False


def item_belongs_to_group(kind, item_id, chat_id):
    table = "queues" if kind == "queue" else "seminars"
    with connect_db() as db:
        return db.execute(
            f"SELECT 1 FROM {table} WHERE id = ? AND group_chat_id = ?", (item_id, chat_id)
        ).fetchone() is not None


async def callback_item_access(callback, kind, item_id):
    chat_id = selected_group_id(callback.from_user.id)
    if not item_belongs_to_group(kind, item_id, chat_id):
        await callback.answer("⛔ Это относится к другой группе. Открой нужную группу через /groups.", show_alert=True)
        return False
    if not await verify_group_member(callback.from_user.id, chat_id):
        await callback.answer("⛔ Бот не может подтвердить, что ты участник выбранной группы. Проверь, что бот добавлен в группу.", show_alert=True)
        return False
    return True


class TenantIsolationMiddleware(BaseMiddleware):
    """Checks ownership and membership for every item callback, including forged buttons."""
    async def __call__(self, handler, event, data):
        payload = getattr(event, "data", None)
        user = getattr(event, "from_user", None)
        if not payload or not user or payload.startswith("group_select:"):
            return await handler(event, data)
        group_id = selected_group_id(user.id)
        kind = None
        item_id = None
        try:
            if payload.startswith(("view:", "place:", "bookself:", "bookother:", "cancel:", "change:", "delete:", "delete_yes:", "publish:")):
                kind = "queue"
                item_id = int(payload.split(":")[1])
            elif payload.startswith(("seminar_view:", "seminar_publish:", "seminar_delete:", "seminar_delete_yes:", "topic_cancel:")):
                kind = "seminar"
                item_id = int(payload.split(":")[1])
            elif payload.startswith("topic_book:"):
                topic_id = int(payload.split(":")[1])
                with connect_db() as db:
                    parent = db.execute("SELECT s.id FROM seminars s JOIN seminar_topics t ON t.seminar_id=s.id WHERE t.id=? AND s.group_chat_id=?", (topic_id, group_id)).fetchone()
                if not parent:
                    await event.answer("⛔ Эта тема относится к другой группе.", show_alert=True)
                    return
                if not await verify_group_member(user.id, group_id):
                    await event.answer("⛔ Не удалось подтвердить участие в выбранной группе.", show_alert=True)
                    return
            if kind and item_id is not None:
                if not item_belongs_to_group(kind, item_id, group_id):
                    await event.answer("⛔ Это относится к другой группе. Открой /groups и выбери свою.", show_alert=True)
                    return
                if not await verify_group_member(user.id, group_id):
                    await event.answer("⛔ Не удалось подтвердить участие в выбранной группе.", show_alert=True)
                    return
        except (ValueError, IndexError):
            await event.answer("Некорректная кнопка. Открой меню заново.", show_alert=True)
            return
        return await handler(event, data)


dp.callback_query.outer_middleware(TenantIsolationMiddleware())


def get_queues(user_id=None):
    chat_id = selected_group_id(user_id) if user_id is not None else LEGACY_GROUP_CHAT_ID
    with connect_db() as db:
        return db.execute("SELECT id, name, places FROM queues WHERE group_chat_id = ? ORDER BY id DESC", (chat_id,)).fetchall()


def get_queue(queue_id):
    with connect_db() as db:
        return db.execute("SELECT id, name, places, group_chat_id FROM queues WHERE id = ?", (queue_id,)).fetchone()


def get_bookings(queue_id):
    with connect_db() as db:
        return db.execute(
            "SELECT place, user_id, user_name, booked_by FROM bookings WHERE queue_id = ? ORDER BY place",
            (queue_id,),
        ).fetchall()


def get_user_booking(queue_id, user_id):
    with connect_db() as db:
        return db.execute(
            "SELECT place FROM bookings WHERE queue_id = ? AND user_id = ? AND user_id > 0",
            (queue_id, user_id),
        ).fetchone()


async def update_queue_announcement(queue_id):
    queue = get_queue(queue_id)
    if not queue:
        return
    bookings = get_bookings(queue_id)
    free_places = queue["places"] - len(bookings)
    with connect_db() as db:
        announcements = db.execute(
            "SELECT chat_id, message_id FROM announcements WHERE queue_id = ?", (queue_id,)
        ).fetchall()

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=queue_{queue_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📝 Открыть очередь и записаться", url=link
    )]])
    for announcement in announcements:
        try:
            await bot.edit_message_text(
                chat_id=announcement["chat_id"],
                message_id=announcement["message_id"],
                text=(
                    "📢 ОБЪЯВЛЕНА НОВАЯ ОЧЕРЕДЬ!\n\n"
                    f"📚 {queue['name']}\n\n"
                    f"🟢 Свободно мест: {free_places}/{queue['places']}\n\n"
                    "🎓 Выбирайте свободное место в боте.\n"
                    "💀 Не тяните до последней минуты!"
                ),
                reply_markup=keyboard,
            )
        except TelegramBadRequest as error:
            error_text = str(error).lower()
            if "message is not modified" in error_text:
                # Текст и кнопки уже актуальны — это нормальная ситуация.
                continue
            if "message to edit not found" in error_text or "message can't be edited" in error_text:
                with connect_db() as db:
                    db.execute(
                        "DELETE FROM announcements WHERE queue_id = ? AND chat_id = ? AND message_id = ?",
                        (queue_id, announcement["chat_id"], announcement["message_id"]),
                    )
                print("Ссылка на недоступное объявление очереди очищена.")
            else:
                print(f"Не удалось обновить объявление очереди: {error}")
        except Exception as error:
            print(f"Не удалось обновить объявление очереди: {error}")


def main_menu(user_id=None):
    rows = [
        [InlineKeyboardButton(text="➕ Создать очередь", callback_data="create")],
        [InlineKeyboardButton(text="📋 Все очереди", callback_data="queues")],
        [InlineKeyboardButton(text="📚 Темы семинаров", callback_data="seminars")],
        [InlineKeyboardButton(text="🏫 Выбрать учебную группу (/groups)", callback_data="groups_help")],
    ]
    if user_id is not None and user_is_group_admin(user_id, selected_group_id(user_id)):
        rows.append([InlineKeyboardButton(text="➕ Создать семинар", callback_data="seminar_create")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queues_keyboard(user_id=None):
    rows = []
    for queue in get_queues(user_id):
        occupied = len(get_bookings(queue["id"]))
        rows.append([InlineKeyboardButton(
            text=f"📋 {queue['name']} ({occupied}/{queue['places']})", callback_data=f"view:{queue['id']}"
        )])
        if user_id is not None and user_is_group_admin(user_id, selected_group_id(user_id)):
            rows.append([InlineKeyboardButton(
                text=f"🗑 Удалить: {queue['name']}", callback_data=f"delete:{queue['id']}"
            )])
            rows.append([InlineKeyboardButton(
                text=f"📢 Опубликовать: {queue['name']}", callback_data=f"publish:{queue['id']}"
            )])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_keyboard(queue_id):
    queue = get_queue(queue_id)
    if not queue:
        return InlineKeyboardMarkup(inline_keyboard=[])
    bookings = {row["place"] for row in get_bookings(queue_id)}
    rows, row = [], []
    for place in range(1, queue["places"] + 1):
        label = f"🔴 {place}" if place in bookings else f"🟢 {place}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"place:{queue_id}:{place}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend([
        [InlineKeyboardButton(text="❌ Отменить мою запись", callback_data=f"cancel:{queue_id}")],
        [InlineKeyboardButton(text="🔄 Сменить место", callback_data=f"change:{queue_id}")],
        [InlineKeyboardButton(text="🔃 Обновить", callback_data=f"view:{queue_id}"),
         InlineKeyboardButton(text="📋 Все очереди", callback_data="queues")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_text(queue_id):
    queue = get_queue(queue_id)
    if not queue:
        return "🛸 Очередь похищена инопланетянами. Не найдена."
    bookings = {row["place"]: row["user_name"] for row in get_bookings(queue_id)}
    capacity = queue["places"]
    free = capacity - len(bookings)
    lines = [
        f"📋 {queue['name']}", random.choice(JOKES), "",
        f"🔴 Занято: {len(bookings)}/{capacity}", f"🟢 Свободно: {free}", "",
    ]
    for place in range(1, capacity + 1):
        if place in bookings:
            lines.append(f"🔴 № {place} — {bookings[place]}")
        else:
            lines.append(f"🟢 № {place} — свободно, пока жизнь не настигла")
    lines.extend(["", "👇 Выбирай свободное место. Если записываешь другого человека, укажи его имя."])
    return "\n".join(lines)


def seminars_keyboard(user_id=None):
    rows = []
    with connect_db() as db:
        seminars = db.execute("SELECT id, title FROM seminars WHERE group_chat_id = ? ORDER BY id DESC", (selected_group_id(user_id) if user_id is not None else LEGACY_GROUP_CHAT_ID,)).fetchall()
    for seminar in seminars:
        with connect_db() as db:
            total = db.execute("SELECT COUNT(*) FROM seminar_topics WHERE seminar_id = ?", (seminar["id"],)).fetchone()[0]
            taken = db.execute("""
                SELECT COUNT(DISTINCT st.id)
                FROM seminar_topics st
                LEFT JOIN seminar_topic_bookings b ON b.topic_id = st.id
                WHERE st.seminar_id = ? AND (b.id IS NOT NULL OR st.user_id IS NOT NULL)
            """, (seminar["id"],)).fetchone()[0]
        rows.append([InlineKeyboardButton(
            text=f"📚 {seminar['title']} ({taken}/{total})", callback_data=f"seminar_view:{seminar['id']}"
        )])
        if user_id is not None and user_is_group_admin(user_id, selected_group_id(user_id)):
            rows.append([
                InlineKeyboardButton(text=f"📢 Опубликовать: {seminar['title'][:28]}", callback_data=f"seminar_publish:{seminar['id']}"),
                InlineKeyboardButton(text=f"🗑 Удалить: {seminar['title'][:28]}", callback_data=f"seminar_delete:{seminar['id']}")
            ])
    if user_id is not None and user_is_group_admin(user_id, selected_group_id(user_id)):
        rows.append([InlineKeyboardButton(text="➕ Создать семинар", callback_data="seminar_create")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)




def seminar_topics_text(seminar_id):
    """Полный список тем и записавшихся; кнопки остаются короткими."""
    with connect_db() as db:
        seminar = db.execute("SELECT title FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
        topics = db.execute(
            """
            SELECT st.id, st.topic_name, st.user_id, st.user_name,
                   GROUP_CONCAT(b.user_name, ', ') AS booked_names,
                   COUNT(b.id) AS booking_count
            FROM seminar_topics st
            LEFT JOIN seminar_topic_bookings b ON b.topic_id = st.id
            WHERE st.seminar_id = ?
            GROUP BY st.id
            ORDER BY st.id
            """,
            (seminar_id,),
        ).fetchall()
    if not seminar:
        return "Этот семинар не найден."
    taken_topics = 0
    lines = [f"📚 {seminar['title']}", "", "📝 Темы и записи:"]
    for i, topic in enumerate(topics, 1):
        names = topic["booked_names"]
        if not names and topic["user_id"] is not None:
            names = topic["user_name"] or "Участник"
        if names:
            taken_topics += 1
            line = f"🔴 {i}. {topic['topic_name']}\n   👤 Записались: {names}"
        else:
            line = f"🟢 {i}. {topic['topic_name']}\n   👤 Пока никто не записался"
        # Оставляем запас до лимита Telegram в 4096 символов.
        if sum(len(part) + 1 for part in lines) + len(line) > 3600:
            lines.append("… Список длиннее лимита сообщения. Остальные темы доступны по кнопкам ниже.")
            break
        lines.append(line)
    lines.insert(3, f"Занято тем: {taken_topics}/{len(topics)}")
    lines.extend(["", "👇 Выбери тему кнопкой ниже. На одну тему могут записаться несколько человек."])
    return "\n".join(lines)


async def update_seminar_announcement(seminar_id):
    with connect_db() as db:
        seminar = db.execute("SELECT id, title FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
        announcements = db.execute(
            "SELECT chat_id, message_id FROM seminar_announcements WHERE seminar_id = ?", (seminar_id,)
        ).fetchall()
    if not seminar or not announcements:
        return
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=seminar_{seminar_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📚 Выбрать тему семинара", url=link)]])
    text = "📢 ЗАПИСЬ НА СЕМИНАР ОТКРЫТА!\n\n" + seminar_topics_text(seminar_id) + "\n\n👇 Нажмите кнопку, чтобы открыть темы."
    # Не допускаем превышения лимита Telegram для текста объявления.
    text = text[:4000]
    for announcement in announcements:
        try:
            await bot.edit_message_text(chat_id=announcement["chat_id"], message_id=announcement["message_id"], text=text, reply_markup=keyboard)
        except TelegramBadRequest as error:
            error_text = str(error).lower()
            if "message is not modified" in error_text:
                continue
            if "message to edit not found" in error_text or "message can't be edited" in error_text:
                with connect_db() as db:
                    db.execute("DELETE FROM seminar_announcements WHERE seminar_id = ? AND chat_id = ? AND message_id = ?", (seminar_id, announcement["chat_id"], announcement["message_id"]))
                print("Ссылка на недоступное объявление семинара очищена.")
            else:
                print(f"Не удалось обновить объявление семинара: {error}")
        except Exception as error:
            print(f"Не удалось обновить объявление семинара: {error}")


def seminar_topics_keyboard(seminar_id):
    with connect_db() as db:
        topics = db.execute(
            """
            SELECT st.id, st.seminar_id, st.topic_name,
                   GROUP_CONCAT(b.user_name, ', ') AS booked_names,
                   COUNT(b.id) AS booking_count,
                   st.user_id, st.user_name
            FROM seminar_topics st
            LEFT JOIN seminar_topic_bookings b ON b.topic_id = st.id
            WHERE st.seminar_id = ?
            GROUP BY st.id
            ORDER BY st.id
            """,
            (seminar_id,),
        ).fetchall()

    rows = []
    for topic in topics:
        names = topic["booked_names"]
        # Для старых записей, до миграции, показываем прежнего участника.
        if not names and topic["user_id"] is not None:
            names = topic["user_name"] or "Участник"
        if names:
            label = f"🔴 {topic['topic_name']} ({topic['booking_count'] or 1})"
        else:
            label = f"🟢 {topic['topic_name']}"
        # Имена участников показываются отдельным сообщением, а не в кнопках.
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"topic_book:{topic['id']}")])

    rows.append([InlineKeyboardButton(text="❌ Снять мою тему", callback_data=f"topic_cancel:{seminar_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К семинарам", callback_data="seminars")])
    return InlineKeyboardMarkup(inline_keyboard=rows)




@dp.message(Command("setup"))
async def setup_group(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Команду /setup нужно отправить внутри вашей учебной группы.")
        return
    if not await verify_group_admin(message.from_user.id, message.chat.id):
        await message.answer("⛔ Зарегистрировать группу может только её администратор/староста.")
        return
    try:
        bot_member = await bot.get_chat_member(message.chat.id, (await bot.get_me()).id)
        if bot_member.status not in {"administrator", "creator"}:
            await message.answer("Сначала выдай боту права администратора в этой группе, затем повтори /setup.")
            return
        admins = await bot.get_chat_administrators(message.chat.id)
    except Exception:
        await message.answer("Не получилось проверить права. Убедись, что бот добавлен в группу и имеет права администратора.")
        return
    with connect_db() as db:
        db.execute(
            "INSERT INTO groups_registry (chat_id, title, registered_by, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, registered_by = excluded.registered_by",
            (message.chat.id, message.chat.title or str(message.chat.id), message.from_user.id, datetime.utcnow().isoformat(timespec="seconds"))
        )
        db.execute("DELETE FROM group_admins WHERE chat_id = ?", (message.chat.id,))
        for admin in admins:
            db.execute("INSERT OR IGNORE INTO group_admins (chat_id, user_id) VALUES (?, ?)", (message.chat.id, admin.user.id))
    set_selected_group(message.from_user.id, message.chat.id)
    await message.answer("✅ Группа подключена! Теперь открой бота в личке и отправь /groups, чтобы выбрать эту группу.")


@dp.message(Command("groups"))
async def list_groups(message: Message):
    with connect_db() as db:
        groups = db.execute("SELECT chat_id, title FROM groups_registry ORDER BY title").fetchall()
    if not groups:
        await message.answer("Пока нет зарегистрированных групп. Староста должна отправить /setup внутри группы.")
        return
    rows = []
    for group in groups:
        if await verify_group_member(message.from_user.id, group["chat_id"]):
            rows.append([InlineKeyboardButton(text=f"🏫 {group['title']}", callback_data=f"group_select:{group['chat_id']}")])
    if not rows:
        await message.answer("Не нашла группы, в которых могу подтвердить твоё участие. Убедитесь, что бот добавлен в группу и имеет права администратора.")
        return
    await message.answer("Выбери свою учебную группу. Очереди и темы других групп будут скрыты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("group_select:"))
async def select_group(callback: CallbackQuery):
    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная группа.", show_alert=True)
        return
    if not is_registered_group(chat_id) or not await verify_group_member(callback.from_user.id, chat_id):
        await callback.answer("⛔ Не удалось подтвердить участие в этой группе.", show_alert=True)
        return
    set_selected_group(callback.from_user.id, chat_id)
    await callback.answer("Группа выбрана!")
    await callback.message.answer("🏫 Группа выбрана. Открывай меню:", reply_markup=main_menu(callback.from_user.id))


@dp.message(F.text == "/myid")
async def show_my_id(message: Message):
    await message.answer(f"🪪 Твой Telegram ID: `{message.from_user.id}`\nЗапиши его и отдай Вике патамушта так нада", parse_mode="Markdown")


@dp.message(CommandStart())
async def start(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("seminar_"):
        try:
            seminar_id = int(parts[1].replace("seminar_", "", 1))
        except ValueError:
            seminar_id = 0
        if seminar_id:
            with connect_db() as db:
                seminar = db.execute("SELECT id, title, group_chat_id FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
            if seminar and await verify_group_member(message.from_user.id, seminar["group_chat_id"]):
                set_selected_group(message.from_user.id, seminar["group_chat_id"])
                await message.answer(seminar_topics_text(seminar_id), reply_markup=seminar_topics_keyboard(seminar_id))
            else:
                await message.answer("👽 Этот семинар не найден. Возможно, его уже удалили.")
            return
    if len(parts) > 1 and parts[1].startswith("queue_"):
        try:
            queue_id = int(parts[1].replace("queue_", "", 1))
        except ValueError:
            queue_id = 0
        if queue_id:
            queue_for_link = get_queue(queue_id)
            if queue_for_link and await verify_group_member(message.from_user.id, queue_for_link["group_chat_id"]):
                set_selected_group(message.from_user.id, queue_for_link["group_chat_id"])
                await message.answer("📩 Ты прибыл по секретной ссылке старосты!\nДобро пожаловать!")
                await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
            else:
                await message.answer("👽 Эта очередь не найдена. Возможно, её уже удалили.")
            return
    await message.answer(
        "🤖 БОТ 3000 (название не я придумала)\n\n"
        "короче Вика сделала бота такая она крутая ну!\n"
        "Короче очередь занимаем электронная не толпимся ноги поднимаем\n\nВыбирай действие:",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.callback_query(F.data == "groups_help")
async def groups_help(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Отправь команду /groups, чтобы выбрать учебную группу. Новую группу староста подключает командой /setup внутри группового чата.")


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("🏠 Главное меню. Дедлайны подождут три секунды.", reply_markup=main_menu(callback.from_user.id))


@dp.callback_query(F.data == "create")
async def create_queue_start(callback: CallbackQuery):
    print("ADMIN CHECK:", callback.from_user.id, ADMIN_IDS)  # ← ВСТАВЬ ЭТУ СТРОКУ
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Создавать очереди может только староста!", show_alert=True)
        return
    waiting_for_queue_name.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer("Привет староста!\nКак назовём новую очередь? Напиши название сообщением. Для отмены — /cancel.")


@dp.callback_query(F.data == "queues")
async def show_queues(callback: CallbackQuery):
    await callback.answer()
    if not get_queues(callback.from_user.id):
        await callback.message.answer("📭 Очередей пока нет. Можно создать первую!", reply_markup=main_menu(callback.from_user.id))
        return
    await callback.message.answer("📚 Все очереди — выбирай, где будем страдать:", reply_markup=queues_keyboard(callback.from_user.id))


@dp.callback_query(F.data.startswith("view:"))
async def view_queue(callback: CallbackQuery):
    await callback.answer()
    queue_id = int(callback.data.split(":")[1])
    await callback.message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))


@dp.callback_query(F.data.startswith("place:"))
async def choose_place(callback: CallbackQuery):
    _, queue_id_text, place_text = callback.data.split(":")
    queue_id, place = int(queue_id_text), int(place_text)
    if not get_queue(queue_id):
        await callback.answer("Очередь не найдена 👽", show_alert=True)
        return
    if not 1 <= place <= get_queue(queue_id)["places"]:
        await callback.answer("Такого места нет.", show_alert=True)
        return
    with connect_db() as db:
        taken = db.execute("SELECT user_name FROM bookings WHERE queue_id = ? AND place = ?", (queue_id, place)).fetchone()
    if taken:
        await callback.answer(f"Место уже занято: {taken['user_name']}", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Занять за себя", callback_data=f"bookself:{queue_id}:{place}")],
        [InlineKeyboardButton(text="✍️ Записать другого", callback_data=f"bookother:{queue_id}:{place}")],
        [InlineKeyboardButton(text="⬅️ Назад к очереди", callback_data=f"view:{queue_id}")],
    ])
    await callback.answer()
    await callback.message.answer(f"Свободное место № {place}. Кого записываем?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("bookself:"))
async def book_self(callback: CallbackQuery):
    _, q_text, p_text = callback.data.split(":")
    queue_id, place = int(q_text), int(p_text)
    queue = get_queue(queue_id)
    if not queue or not 1 <= place <= queue["places"]:
        await callback.answer("Такого места нет или очередь удалена.", show_alert=True)
        return
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name or "Участник"
    try:
        with connect_db() as db:
            db.execute("INSERT INTO bookings (queue_id, place, user_id, user_name, booked_by) VALUES (?, ?, ?, ?, ?)",
                       (queue_id, place, user_id, user_name, user_id))
    except sqlite3.IntegrityError:
        await callback.answer("⛔ Место занято или ты уже записан! Очередь — не королевская битва.", show_alert=True)
        return
    await update_queue_announcement(queue_id)
    await callback.answer(f"🎉 Место № {place} твоё!")
    await callback.message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))


@dp.callback_query(F.data.startswith("bookother:"))
async def book_other_start(callback: CallbackQuery):
    _, q_text, p_text = callback.data.split(":")
    queue_id, place = int(q_text), int(p_text)
    queue = get_queue(queue_id)
    if not queue or not 1 <= place <= queue["places"]:
        await callback.answer("Такого места нет или очередь удалена.", show_alert=True)
        return
    with connect_db() as db:
        taken = db.execute("SELECT 1 FROM bookings WHERE queue_id = ? AND place = ?", (queue_id, place)).fetchone()
    if taken:
        await callback.answer("Это место уже заняли. Обнови очередь.", show_alert=True)
        return
    waiting_for_proxy_name[callback.from_user.id] = (queue_id, place)
    await callback.answer()
    await callback.message.answer(f"✍️ Напиши имя человека для места № {place}. Можно указать имя и фамилию. Напиши /cancel, чтобы отменить.")


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    with connect_db() as db:
        cursor = db.execute("DELETE FROM bookings WHERE queue_id = ? AND user_id = ? AND user_id > 0", (queue_id, user_id))
    await callback.answer()
    if cursor.rowcount == 0:
        await callback.message.answer("🤷 У тебя здесь нет личной записи. Запись, сделанную за другого, этой кнопкой не отменить.")
        return
    await update_queue_announcement(queue_id)
    await callback.message.answer("🫡 Запись отменена. Место снова свободно!", reply_markup=main_menu(user_id))
    await callback.message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))


@dp.callback_query(F.data.startswith("change:"))
async def change_booking(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    if not get_user_booking(queue_id, user_id):
        await callback.answer()
        await callback.message.answer("👀 Сначала запишись в очередь за себя, потом меняй место.")
        return
    waiting_for_change.add(user_id)
    waiting_for_change_queue[user_id] = queue_id
    await callback.answer()
    await callback.message.answer(f"🔄 На какой номер пересаживаемся? Напиши число от 1 до {get_queue(queue_id)['places']}. Если место занято, запись не изменится.")


@dp.callback_query(F.data.startswith("delete:"))
async def delete_queue_confirm(callback: CallbackQuery):
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Только староста может удалять очереди!", show_alert=True)
        return
    queue_id = int(callback.data.split(":")[1])
    queue = get_queue(queue_id)
    if not queue:
        await callback.answer("Очередь уже не существует.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💥 Да, удалить", callback_data=f"delete_yes:{queue_id}"),
        InlineKeyboardButton(text="🛡 Отмена", callback_data=f"view:{queue_id}"),
    ]])
    await callback.answer()
    await callback.message.answer(f"⚠️ Точно удалить очередь «{queue['name']}»? Все записи в ней тоже будут удалены. Вернуть их будет нельзя.", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("delete_yes:"))
async def delete_queue_execute(callback: CallbackQuery):
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Только староста!", show_alert=True)
        return
    queue_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        announcements = db.execute(
            "SELECT chat_id, message_id FROM announcements WHERE queue_id = ?", (queue_id,)
        ).fetchall()
        db.execute("DELETE FROM bookings WHERE queue_id = ?", (queue_id,))
        db.execute("DELETE FROM announcements WHERE queue_id = ?", (queue_id,))
        cursor = db.execute("DELETE FROM queues WHERE id = ?", (queue_id,))
    await callback.answer()
    if cursor.rowcount:
        # Убираем объявления из чата, чтобы участники не открывали удалённую очередь.
        for announcement in announcements:
            try:
                await bot.delete_message(
                    chat_id=announcement["chat_id"],
                    message_id=announcement["message_id"],
                )
            except TelegramBadRequest as error:
                error_text = str(error).lower()
                if "message to delete not found" in error_text or "message can't be deleted for everyone" in error_text:
                    print("Объявление очереди уже удалено или Telegram не разрешил его удалить; очередь удалена из базы.")
                else:
                    print(f"Не удалось удалить объявление очереди: {error}")
            except Exception as error:
                print(f"Не удалось удалить объявление очереди: {error}")
        await callback.message.answer("💥 Очередь удалена. Одной проблемой меньше.", reply_markup=main_menu(callback.from_user.id))
    else:
        await callback.message.answer("Очередь уже удалена.")


@dp.callback_query(F.data.startswith("publish:"))
async def publish_queue(callback: CallbackQuery):
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Публиковать очереди может только староста! сорян( если вы староста обратитесь к Вике", show_alert=True)
        return
    queue_id = int(callback.data.split(":")[1])
    queue = get_queue(queue_id)
    if not queue:
        await callback.answer("Очередь не найдена!", show_alert=True)
        return
    try:
        me = await bot.get_me()
        if not me.username:
            await callback.answer("Не удалось получить имя бота.", show_alert=True)
            return
        link = f"https://t.me/{me.username}?start=queue_{queue_id}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Открыть очередь и записаться", url=link)]])
        sent_message = await bot.send_message(
            queue["group_chat_id"],
            f"📢 ОБЪЯВЛЕНА НОВАЯ ОЧЕРЕДЬ!\n\n📚 {queue['name']}\n\n"
            f"🟢 Свободно мест: {queue['places'] - len(get_bookings(queue_id))}/{queue['places']}\n\n"
            "🎓 Выбирайте свободное место в боте.\n💀 Не тяните до последней минуты!",
            reply_markup=keyboard,
        )
        with connect_db() as db:
            db.execute("INSERT OR IGNORE INTO announcements (queue_id, chat_id, message_id) VALUES (?, ?, ?)",
                       (queue_id, queue["group_chat_id"], sent_message.message_id))
        await callback.answer("Объявление опубликовано! 📢")
        await callback.message.answer(f"✅ Очередь «{queue['name']}» опубликована в группе!\nДизайнерский IT-отдел доволен 😎")
    except Exception as error:
        print(f"Ошибка публикации: {error}")
        await callback.answer("Не получилось опубликовать. Проверь, что бот есть в группе.", show_alert=True)


@dp.callback_query(F.data == "seminars")
async def show_seminars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("📚 Семинары и темы. Выбирай семинар:", reply_markup=seminars_keyboard(callback.from_user.id))


@dp.callback_query(F.data == "seminar_create")
async def seminar_create_start(callback: CallbackQuery):
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Создавать семинары может только староста!", show_alert=True)
        return
    waiting_for_seminar_title.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer("➕ Напиши название семинара или предмета. Например: «История искусства — семинар 2».")


@dp.callback_query(F.data.startswith("seminar_view:"))
async def seminar_view(callback: CallbackQuery):
    seminar_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        seminar = db.execute("SELECT id, title, group_chat_id FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM seminar_topics WHERE seminar_id = ?", (seminar_id,)).fetchone()[0]
    await callback.answer()
    if not seminar:
        await callback.message.answer("Этот семинар не найден.")
        return
    if count == 0:
        await callback.message.answer(f"📚 {seminar['title']}\nПока тем нет.")
        return
    await callback.message.answer(seminar_topics_text(seminar_id), reply_markup=seminar_topics_keyboard(seminar_id))


@dp.callback_query(F.data.startswith("seminar_publish:"))
async def publish_seminar(callback: CallbackQuery):
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Публиковать семинары может только староста!", show_alert=True)
        return
    seminar_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        seminar = db.execute("SELECT id, title, group_chat_id FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
        topics = db.execute(
            "SELECT COUNT(*) FROM seminar_topics WHERE seminar_id = ?", (seminar_id,)
        ).fetchone()[0]
    if not seminar:
        await callback.answer("Семинар не найден.", show_alert=True)
        return
    try:
        me = await bot.get_me()
        if not me.username:
            await callback.answer("Не удалось получить имя бота.", show_alert=True)
            return
        link = f"https://t.me/{me.username}?start=seminar_{seminar_id}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📚 Выбрать тему семинара", url=link)
        ]])
        sent = await bot.send_message(
            seminar["group_chat_id"],
            f"📢 ЗАПИСЬ НА СЕМИНАР ОТКРЫТА!\n\n" +
            seminar_topics_text(seminar_id) + "\n\n👇 Нажмите кнопку, чтобы открыть темы и выбрать свою.",
            reply_markup=keyboard
        )
        with connect_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO seminar_announcements (seminar_id, chat_id, message_id) VALUES (?, ?, ?)",
                (seminar_id, seminar["group_chat_id"], sent.message_id)
            )
        await callback.answer("Семинар опубликован в группе! 📢")
        await callback.message.answer(f"✅ Семинар «{seminar['title']}» опубликован в группе!")
    except Exception as error:
        print(f"Ошибка публикации семинара: {error}")
        await callback.answer("Не получилось опубликовать. Проверь, что бот есть в группе и может писать сообщения.", show_alert=True)


@dp.callback_query(F.data.startswith("seminar_delete:"))
async def delete_seminar_confirm(callback: CallbackQuery):
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Удалять семинары может только староста!", show_alert=True)
        return
    seminar_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        seminar = db.execute("SELECT id, title, group_chat_id FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
    if not seminar:
        await callback.answer("Семинар уже не существует.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💥 Да, удалить семинар", callback_data=f"seminar_delete_yes:{seminar_id}"),
        InlineKeyboardButton(text="🛡 Отмена", callback_data="seminars")
    ]])
    await callback.answer()
    await callback.message.answer(
        f"⚠️ Точно удалить семинар «{seminar['title']}»? Все темы и записи на них будут удалены.",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("seminar_delete_yes:"))
async def delete_seminar_execute(callback: CallbackQuery):
    if not user_is_group_admin(callback.from_user.id, selected_group_id(callback.from_user.id)):
        await callback.answer("⛔ Только староста!", show_alert=True)
        return
    seminar_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        seminar = db.execute("SELECT id, title, group_chat_id FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
        announcements = db.execute(
            "SELECT chat_id, message_id FROM seminar_announcements WHERE seminar_id = ?", (seminar_id,)
        ).fetchall()
        if not seminar:
            await callback.answer("Семинар уже удалён.", show_alert=True)
            return
        db.execute("DELETE FROM seminar_topic_bookings WHERE topic_id IN (SELECT id FROM seminar_topics WHERE seminar_id = ?)", (seminar_id,))
        db.execute("DELETE FROM seminar_topics WHERE seminar_id = ?", (seminar_id,))
        db.execute("DELETE FROM seminar_announcements WHERE seminar_id = ?", (seminar_id,))
        db.execute("DELETE FROM seminars WHERE id = ?", (seminar_id,))
    for announcement in announcements:
        try:
            await bot.delete_message(chat_id=announcement["chat_id"], message_id=announcement["message_id"])
        except TelegramBadRequest as error:
            error_text = str(error).lower()
            if "message to delete not found" in error_text or "message can't be deleted for everyone" in error_text:
                print("Объявление семинара уже удалено или Telegram не разрешил его удалить; семинар удалён из базы.")
            else:
                print(f"Не удалось удалить старое объявление семинара: {error}")
        except Exception as error:
            print(f"Не удалось удалить старое объявление семинара: {error}")
    await callback.answer("Семинар удалён.")
    await callback.message.answer(
        f"💥 Семинар «{seminar['title']}» удалён вместе с темами и записями.",
        reply_markup=seminars_keyboard(callback.from_user.id)
    )


@dp.callback_query(F.data.startswith("topic_book:"))
async def topic_book(callback: CallbackQuery):
    topic_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    with connect_db() as db:
        topic = db.execute(
            "SELECT id, seminar_id, topic_name FROM seminar_topics WHERE id = ?", (topic_id,)
        ).fetchone()
        if not topic:
            await callback.answer("Тема не найдена.", show_alert=True)
            return

        # Один человек может выбрать только одну тему в рамках семинара.
        existing = db.execute("""
            SELECT st.topic_name
            FROM seminar_topic_bookings b
            JOIN seminar_topics st ON st.id = b.topic_id
            WHERE st.seminar_id = ? AND b.user_id = ?
            LIMIT 1
        """, (topic["seminar_id"], user_id)).fetchone()
        if not existing:
            # Совместимость со старой записью, если она ещё не перенеслась.
            existing = db.execute(
                "SELECT topic_name FROM seminar_topics WHERE seminar_id = ? AND user_id = ?",
                (topic["seminar_id"], user_id),
            ).fetchone()
        if existing:
            if existing["topic_name"] == topic["topic_name"]:
                await callback.answer("Ты уже записан(а) на эту тему!", show_alert=True)
            else:
                await callback.answer(f"Ты уже выбрал(а) тему: {existing['topic_name']}", show_alert=True)
            return

        try:
            db.execute(
                "INSERT INTO seminar_topic_bookings (topic_id, user_id, user_name, booked_at) VALUES (?, ?, ?, ?)",
                (topic_id, user_id, callback.from_user.full_name or "Участник",
                 datetime.utcnow().isoformat(timespec="seconds")),
            )
        except sqlite3.IntegrityError:
            await callback.answer("Ты уже записан(а) на эту тему!", show_alert=True)
            return

    await update_seminar_announcement(topic["seminar_id"])
    await callback.answer("🎉 Записала тебя на тему!")
    await callback.message.answer(
        seminar_topics_text(topic["seminar_id"]),
        reply_markup=seminar_topics_keyboard(topic["seminar_id"]),
    )


@dp.callback_query(F.data.startswith("topic_cancel:"))
async def topic_cancel(callback: CallbackQuery):
    seminar_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        cursor = db.execute("""
            DELETE FROM seminar_topic_bookings
            WHERE user_id = ? AND topic_id IN (
                SELECT id FROM seminar_topics WHERE seminar_id = ?
            )
        """, (callback.from_user.id, seminar_id))
        # Если это старая запись, до миграции/переноса, тоже снимаем её.
        legacy = db.execute(
            "UPDATE seminar_topics SET user_id = NULL, user_name = NULL, booked_at = NULL WHERE seminar_id = ? AND user_id = ?",
            (seminar_id, callback.from_user.id),
        )
    await callback.answer()
    if cursor.rowcount or legacy.rowcount:
        await update_seminar_announcement(seminar_id)
        await callback.message.answer(seminar_topics_text(seminar_id), reply_markup=seminar_topics_keyboard(seminar_id))
    else:
        await callback.message.answer("У тебя нет выбранной темы в этом семинаре.")


@dp.message(F.text)
async def receive_text(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if text == "/cancel":
        waiting_for_proxy_name.pop(user_id, None)
        waiting_for_change.discard(user_id)
        waiting_for_change_queue.pop(user_id, None)
        waiting_for_queue_name.discard(user_id)
        waiting_for_queue_capacity.discard(user_id)
        pending_queue_name.pop(user_id, None)
        waiting_for_seminar_title.discard(user_id)
        waiting_for_seminar_topics.pop(user_id, None)
        staged_topics.pop(user_id, None)
        await message.answer("Окей, как хош.", reply_markup=main_menu(user_id))
        return

    if user_id in waiting_for_proxy_name:
        queue_id, place = waiting_for_proxy_name.pop(user_id)
        if not text or len(text) > 100:
            await message.answer("Имя должно быть от 1 до 100 символов. Попробуй ещё раз через выбор места.")
            return
        if not get_queue(queue_id):
            await message.answer("Очередь больше не существует.")
            return
        with connect_db() as db:
            taken = db.execute("SELECT 1 FROM bookings WHERE queue_id = ? AND place = ?", (queue_id, place)).fetchone()
            duplicate = db.execute(
                "SELECT 1 FROM bookings WHERE queue_id = ? AND lower(user_name) = lower(?)",
                (queue_id, text),
            ).fetchone()
            if taken:
                await message.answer("Место уже успели занять. Обнови очередь и выбери другое.")
                return
            if duplicate:
                await message.answer("Похоже, этот человек уже есть в очереди. Проверь список, чтобы не записать его дважды.")
                return
            # Negative synthetic ID keeps the legacy NOT NULL + UNIQUE constraints intact.
            # Check the generated ID separately so a place conflict cannot cause an endless retry.
            while True:
                synthetic_id = -secrets.randbelow(2_000_000_000) - 1
                id_taken = db.execute(
                    "SELECT 1 FROM bookings WHERE queue_id = ? AND user_id = ?",
                    (queue_id, synthetic_id),
                ).fetchone()
                if not id_taken:
                    break
            try:
                db.execute(
                    "INSERT INTO bookings (queue_id, place, user_id, user_name, booked_by) VALUES (?, ?, ?, ?, ?)",
                    (queue_id, place, synthetic_id, text, user_id),
                )
            except sqlite3.IntegrityError:
                await message.answer("Место только что заняли. Обнови очередь и попробуй другое.")
                return
        await update_queue_announcement(queue_id)
        await message.answer(f"✅ Записала «{text}» на место № {place}.", reply_markup=main_menu(user_id))
        await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
        return

    if user_id in waiting_for_change:
        try:
            place = int(text)
        except ValueError:
            await message.answer("🤨 Введи номер места цифрами.")
            return
        queue_id = waiting_for_change_queue.get(user_id)
        if queue_id is None:
            waiting_for_change.discard(user_id)
            await message.answer("Смена места отменена. Попробуй заново.")
            return
        queue = get_queue(queue_id) if queue_id is not None else None
        if not queue:
            waiting_for_change.discard(user_id)
            waiting_for_change_queue.pop(user_id, None)
            await message.answer("Очередь больше не существует.")
            return
        if not 1 <= place <= queue["places"]:
            await message.answer(f"Номер должен быть от 1 до {queue['places']}.")
            return
        current = get_user_booking(queue_id, user_id)
        if not current:
            waiting_for_change.discard(user_id)
            waiting_for_change_queue.pop(user_id, None)
            await message.answer("Сначала нужно занять место в этой очереди.")
            return
        if current[0] == place:
            await message.answer("😂 Ты уже сидишь на этом месте!")
            return
        try:
            with connect_db() as db:
                db.execute("UPDATE bookings SET place = ? WHERE queue_id = ? AND user_id = ? AND user_id > 0", (place, queue_id, user_id))
        except sqlite3.IntegrityError:
            await message.answer("💀 Место занято! Судьба оказалась быстрее.")
            return
        waiting_for_change.discard(user_id)
        waiting_for_change_queue.pop(user_id, None)
        await update_queue_announcement(queue_id)
        await message.answer(f"✨ Пересадка успешна! С новосельем! Новое место — № {place}", reply_markup=main_menu(user_id))
        await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
        return

    if user_id in waiting_for_queue_name:
        if not user_is_group_admin(user_id, selected_group_id(user_id)):
            waiting_for_queue_name.discard(user_id)
            return
        if not text or len(text) > 100:
            await message.answer("Название должно быть от 1 до 100 символов.")
            return
        waiting_for_queue_name.discard(user_id)
        pending_queue_name[user_id] = text
        waiting_for_queue_capacity.add(user_id)
        await message.answer("🔢 Сколько участников должно быть в очереди? Напиши число от 1 до 200. Например, 32. Для отмены — /cancel.")
        return

    if user_id in waiting_for_queue_capacity:
        if not user_is_group_admin(user_id, selected_group_id(user_id)):
            waiting_for_queue_capacity.discard(user_id)
            pending_queue_name.pop(user_id, None)
            return
        try:
            capacity = int(text)
        except ValueError:
            await message.answer("Нужно написать целое число от 1 до 200. Попробуй ещё раз.")
            return
        if not 1 <= capacity <= 200:
            await message.answer("Количество участников должно быть от 1 до 200. Попробуй ещё раз.")
            return
        name = pending_queue_name.pop(user_id, None)
        if not name:
            waiting_for_queue_capacity.discard(user_id)
            await message.answer("Название очереди потерялось. Начни создание заново.", reply_markup=main_menu(user_id))
            return
        with connect_db() as db:
            cursor = db.execute("INSERT INTO queues (name, places, group_chat_id) VALUES (?, ?, ?)", (name, capacity, selected_group_id(user_id)))
            queue_id = cursor.lastrowid
        waiting_for_queue_capacity.discard(user_id)
        await message.answer(f"🎉 Очередь создана! Вместимость: {capacity} участник(ов).", reply_markup=main_menu(user_id))
        await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
        return

    if user_id in waiting_for_seminar_title:
        if not user_is_group_admin(user_id, selected_group_id(user_id)):
            waiting_for_seminar_title.discard(user_id)
            return
        if not text or len(text) > 150:
            await message.answer("Название должно быть от 1 до 150 символов.")
            return
        waiting_for_seminar_title.discard(user_id)
        staged_topics[user_id] = []
        waiting_for_seminar_topics[user_id] = text
        await message.answer("Теперь отправь список тем — каждую тему с новой строки. Когда закончишь, напиши ГОТОВО. Для отмены — /cancel.")
        return

    if user_id in waiting_for_seminar_topics:
        if not user_is_group_admin(user_id, selected_group_id(user_id)):
            waiting_for_seminar_topics.pop(user_id, None)
            return
        if text.upper() == "ГОТОВО":
            title = waiting_for_seminar_topics.pop(user_id)
            # Не создаём пустой семинар, если староста сразу нажал «ГОТОВО».
            topics = staged_topics.pop(user_id, [])
            if not topics:
                await message.answer(
                    "Список тем пустой. Отправь хотя бы одну тему или /cancel для отмены.",
                    reply_markup=main_menu(user_id),
                )
                waiting_for_seminar_topics[user_id] = title
                staged_topics[user_id] = []
                return
            with connect_db() as db:
                cursor = db.execute("INSERT INTO seminars (title, created_by, created_at, group_chat_id) VALUES (?, ?, ?, ?)",
                                    (title, user_id, datetime.utcnow().isoformat(timespec="seconds"), selected_group_id(user_id)))
                seminar_id = cursor.lastrowid
                db.executemany("INSERT INTO seminar_topics (seminar_id, topic_name) VALUES (?, ?)", [(seminar_id, topic) for topic in topics])
            await message.answer(f"🎉 Семинар «{title}» создан: тем — {len(topics)}.", reply_markup=main_menu(user_id))
            await message.answer(seminar_topics_text(seminar_id), reply_markup=seminar_topics_keyboard(seminar_id))
            return
        if len(text) > 150:
            await message.answer("Одна тема должна быть не длиннее 150 символов.")
            return
        topics = staged_topics.setdefault(user_id, [])
        if len(topics) >= 50:
            await message.answer("Максимум 50 тем в одном семинаре.")
            return
        if text not in topics:
            topics.append(text)
        await message.answer(f"Добавлено тем: {len(topics)}. Отправь следующую тему или напиши ГОТОВО.")
        return


staged_topics = {}


async def main():
    init_db()
    print(f"Бот запускается... База данных: {os.path.abspath(DB_FILE)}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
