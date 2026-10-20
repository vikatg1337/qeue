
import asyncio
import os
import random
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("Не найден токен! Проверь файл .env")

session = AiohttpSession()
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

DB_FILE = "queues.db"
PLACES = 32
ADMIN_ID = 1053596260
GROUP_CHAT_ID = -5496142597

JOKES = [
    "МЕМЕМЕМЕМЕМЕМЕМЕМЕМЕМЕМЕ",
    "жучило какой-то",
    "...",
    "Скачать вирусы без промоушен",
    "🐸 Кто рано записался, тот рано начал переживать",
    "67",
]


def init_db():
    with sqlite3.connect(DB_FILE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS queues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
        """)
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

def get_queues():
    with sqlite3.connect(DB_FILE) as db:
        return db.execute(
            "SELECT id, name FROM queues ORDER BY id DESC"
        ).fetchall()


def get_queue(queue_id):
    with sqlite3.connect(DB_FILE) as db:
        return db.execute(
            "SELECT id, name FROM queues WHERE id = ?",
            (queue_id,)
        ).fetchone()


def get_bookings(queue_id):
    with sqlite3.connect(DB_FILE) as db:
        return db.execute(
            "SELECT place, user_id, user_name FROM bookings "
            "WHERE queue_id = ? ORDER BY place",
            (queue_id,)
        ).fetchall()


async def update_queue_announcement(queue_id):
    queue = None

    for q_id, name in get_queues():
        if q_id == queue_id:
            queue = name
            break

    if queue is None:
        return

    bookings = get_bookings(queue_id)
    free_places = PLACES - len(bookings)

    with sqlite3.connect(DB_FILE) as db:
        announcements = db.execute(
            """
            SELECT chat_id, message_id
            FROM announcements
            WHERE queue_id = ?
            """,
            (queue_id,)
        ).fetchall()

    for chat_id, message_id in announcements:
        try:
            me = await bot.get_me()
            link = f"https://t.me/{me.username}?start=queue_{queue_id}"

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📝 Открыть очередь и записаться",
                    url=link
                )]
            ])

            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"📢 ОБЪЯВЛЕНА НОВАЯ ОЧЕРЕДЬ!\n\n"
                    f"📚 {queue}\n\n"
                    f"🟢 Свободно мест: {free_places}/{PLACES}\n\n"
                    f"🎓 Выбирайте свободное место в боте.\n"
                    f"💀 Не тяните до последней минуты!"
                ),
                reply_markup=keyboard
            )
        except Exception as error:
            print(f"Не удалось обновить объявление: {error}")


def get_user_booking(queue_id, user_id):
    with sqlite3.connect(DB_FILE) as db:
        return db.execute(
            "SELECT place FROM bookings "
            "WHERE queue_id = ? AND user_id = ?",
            (queue_id, user_id)
        ).fetchone()


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Создать очередь",
            callback_data="create"
        )],
        [InlineKeyboardButton(
            text="📋 Все очереди",
            callback_data="queues"
        )],
    ])



def queues_keyboard(user_id=None):
    queues = get_queues()
    rows = []

    for queue_id, name in queues:
        bookings = get_bookings(queue_id)
        occupied = len(bookings)

        rows.append([
            InlineKeyboardButton(
                text=f"📋 {name} ({occupied}/{PLACES})",
                callback_data=f"view:{queue_id}"
            )
        ])

        if user_id == ADMIN_ID:
            rows.append([
                InlineKeyboardButton(
                    text=f"🗑 Удалить: {name}",
                    callback_data=f"delete:{queue_id}"
                )
            ])

            rows.append([
                InlineKeyboardButton(
                    text=f"📢 Опубликовать: {name}",
                    callback_data=f"publish:{queue_id}"
                )
            ])

    rows.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="home"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)
    queues = get_queues()
    rows = []

    for queue_id, name in queues:
        bookings = get_bookings(queue_id)
        occupied = len(bookings)
        rows.append([InlineKeyboardButton(
            text=f"📋 {name} ({occupied}/{PLACES})",
            callback_data=f"view:{queue_id}"
        )])

    rows.append([InlineKeyboardButton(
        text="🏠 Главное меню",
        callback_data="home"
    )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_keyboard(queue_id):
    bookings = get_bookings(queue_id)
    occupied = {row[0] for row in bookings}
    rows = []
    row = []

    for place in range(1, PLACES + 1):
        label = f"🔴 {place}" if place in occupied else f"🟢 {place}"
        row.append(InlineKeyboardButton(
            text=label,
            callback_data=f"place:{queue_id}:{place}"
        ))
        if len(row) == 4:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            text="❌ Отменить мою запись",
            callback_data=f"cancel:{queue_id}"
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="🔄 Сменить место",
            callback_data=f"change:{queue_id}"
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="🔃 Обновить",
            callback_data=f"view:{queue_id}"
        ),
        InlineKeyboardButton(
            text="📋 Все очереди",
            callback_data="queues"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_text(queue_id):
    queue = get_queue(queue_id)
    if not queue:
        return "🛸 Очередь похищена инопланетянами. Не найдена."

    bookings = get_bookings(queue_id)
    occupied = {row[0]: row[2] for row in bookings}
    free = PLACES - len(bookings)

    lines = [
        f"📋 {queue[1]}",
        random.choice(JOKES),
        "",
        f"🔴 Занято: {len(bookings)}/{PLACES}",
        f"🟢 Свободно: {free}",
        "",
    ]

    for place in range(1, PLACES + 1):
        if place in occupied:
            lines.append(f"🔴 № {place} — {occupied[place]}")
        else:
            lines.append(f"🟢 № {place} — свободно, пока жизнь не настигла")

    lines.extend([
        "",
        "👇 Выбирай место. Судьба уже нажала «Продолжить»."
    ])
    return "\n".join(lines)


waiting_for_name = set()
waiting_for_change = set()

@dp.message(F.text == "/myid")
async def show_my_id(message: Message):
    await message.answer(
        f"🪪 Твой Telegram ID: `{message.from_user.id}`\n"
        "Запиши его — это пропуск в админку 😎"
    )

@dp.message(CommandStart())
async def start(message: Message):
    parts = (message.text or "").split(maxsplit=1)

    if len(parts) > 1 and parts[1].startswith("queue_"):
        try:
            queue_id = int(parts[1].replace("queue_", "", 1))
        except ValueError:
            queue_id = 0

        if queue_id:
            queue = get_queue(queue_id)

            if queue:
                await message.answer(
                    "📩 Ты прибыл по секретной ссылке старосты!\n"
                    "Добро пожаловать в академический хаос ☠️"
                )
                await message.answer(
                    queue_text(queue_id),
                    reply_markup=queue_keyboard(queue_id)
                )
                return

            await message.answer(
                "👽 Эта очередь не найдена. Возможно, её уже удалили."
            )
            return

    await message.answer(
        "🤖 ВИКИН БОТ 3000 (название не я придумала)\n\n"
        "короче Вика навайбкодила бота такая она крутая ну!\n"
        "Короче очередь занимаем электронная не толпимся ноги поднимаем\n\n"
        "Выбирай действие:",
        reply_markup=main_menu()
    )



@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.answer("Возвращаемся в штаб 🫡")
    await callback.message.answer(
        "🏠 Главное меню. Дедлайны подождут три секунды.",
        reply_markup=main_menu()
    )




@dp.callback_query(F.data == "create")
async def create_queue_start(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ Создавать очереди может только староста!",
            show_alert=True
        )
        return

    await callback.answer()
    waiting_for_name.add(callback.from_user.id)
    await callback.message.answer(
        "Режим старосты активирован!\n"
        "Как назовём новую очередь?\n"
        "Например: «Скачать вирусы без промоушшен»"
    )


@dp.message(F.text == "/groupid")
async def show_group_id(message: Message):
    await message.answer(
        f"🛰️ ID этого чата: {message.chat.id}"
    )

@dp.message(F.text)
async def receive_text(message: Message):
    user_id = message.from_user.id
    name = message.text.strip()

    if user_id in waiting_for_change:
        waiting_for_change.discard(user_id)
        try:
            place = int(name)
        except ValueError:
            await message.answer("🤨 Введи номер цифрами от 1 до 32.")
            return

        queue_id = waiting_for_change_queue.pop(user_id, None)
        if queue_id is None:
            await message.answer("Смена места отменена. Попробуй заново.")
            return

        if not 1 <= place <= PLACES:
            await message.answer("Номер должен быть от 1 до 32.")
            return

        current = get_user_booking(queue_id, user_id)
        if not current:
            await message.answer("Сначала нужно занять место в этой очереди.")
            return

        if current[0] == place:
            await message.answer("😂 Ты уже сидишь на этом месте!")
            return

        try:
            with sqlite3.connect(DB_FILE) as db:
                db.execute(
                    "UPDATE bookings SET place = ? "
                    "WHERE queue_id = ? AND user_id = ?",
                    (place, queue_id, user_id)
                )
        except sqlite3.IntegrityError:
            await message.answer(
                "💀 Место занято! Судьба оказалась быстрее."
            )
            return

        await update_queue_announcement(queue_id)

        await message.answer(
            "✨ Пересадка успешна! Новое место — № " + str(place),
            reply_markup=main_menu()
        )
        await message.answer(
            queue_text(queue_id),
            reply_markup=queue_keyboard(queue_id)
        )
        return

    if user_id not in waiting_for_name:
        return

    if not name or len(name) > 100:
        await message.answer("Название должно быть от 1 до 100 символов.")
        return

    with sqlite3.connect(DB_FILE) as db:
        cursor = db.execute(
            "INSERT INTO queues (name) VALUES (?)",
            (name,)
        )
        queue_id = cursor.lastrowid

    waiting_for_name.discard(user_id)
    await message.answer(
        "🎉 Очередь создана! Администрация довольна.",
        reply_markup=main_menu()
    )
    await message.answer(
        queue_text(queue_id),
        reply_markup=queue_keyboard(queue_id)
    )


waiting_for_change_queue = {}


@dp.callback_query(F.data.startswith("delete:"))
async def delete_queue_confirm(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ Только староста может удалять очереди! Сорян",
            show_alert=True
        )
        return

    queue_id = int(callback.data.split(":")[1])
    queue = get_queue(queue_id)

    if not queue:
        await callback.answer("Очередь уже не существует.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💥 Да, удалить",
                callback_data=f"delete_yes:{queue_id}"
            ),
            InlineKeyboardButton(
                text="🛡 Отмена",
                callback_data=f"view:{queue_id}"
            )
        ]
    ])

    await callback.answer()
    await callback.message.answer(
        f"⚠️ Точно удалить очередь «{queue[1]}»?\n\n"
        "Все записи в ней тоже будут удалены. "
        "Вернуть их будет нельзя.",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("delete_yes:"))
async def delete_queue_execute(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Только староста!", show_alert=True)
        return

    queue_id = int(callback.data.split(":")[1])

    with sqlite3.connect(DB_FILE) as db:
        db.execute(
            "DELETE FROM bookings WHERE queue_id = ?",
            (queue_id,)
        )
        cursor = db.execute(
            "DELETE FROM queues WHERE id = ?",
            (queue_id,)
        )

    await callback.answer()
    if cursor.rowcount:
        await callback.message.answer(
            "💥 Очередь удалена. Одной академической проблемой меньше.",
            reply_markup=main_menu()
        )
    else:
        await callback.message.answer("Очередь уже удалена.")


@dp.callback_query(F.data.startswith("publish:"))
async def publish_queue(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ Публиковать очереди может только староста!",
            show_alert=True
        )
        return

    queue_id = int(callback.data.split(":")[1])
    queue = get_queue(queue_id)

    if not queue:
        await callback.answer(
            "Очередь не найдена!",
            show_alert=True
        )
        return

    try:
        me = await bot.get_me()
        bot_username = me.username

        if not bot_username:
            await callback.answer(
                "Не удалось получить имя бота.",
                show_alert=True
            )
            return

        link = f"https://t.me/{bot_username}?start=queue_{queue_id}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📝 Открыть очередь и записаться",
                url=link
            )]
        ])

        
        sent_message = await bot.send_message(
            GROUP_CHAT_ID,
            f"📢 ОБЪЯВЛЕНА НОВАЯ ОЧЕРЕДЬ!\n\n"
            f"📚 {queue[1]}\n\n"
            f"🟢 Свободно мест: {PLACES - len(get_bookings(queue_id))}/{PLACES}\n\n"
            f"🎓 Выбирайте свободное место в боте.\n"
            f"💀 Не тяните до последней минуты!",
            reply_markup=keyboard
        )

        with sqlite3.connect(DB_FILE) as db:
            db.execute(
                """
                INSERT OR IGNORE INTO announcements
                (queue_id, chat_id, message_id)
                VALUES (?, ?, ?)
                """,
                (queue_id, GROUP_CHAT_ID, sent_message.message_id)
            )

        await callback.answer("Объявление опубликовано! 📢")
        await callback.message.answer(
            f"✅ Очередь «{queue[1]}» опубликована в группе!\n"
            "Викин IT-отдел доволен 😎"
        )

    except Exception as error:
        print(f"Ошибка публикации: {error}")
        await callback.answer(
            "Не получилось опубликовать. Проверь, что бот есть в группе.",
            show_alert=True
        )

@dp.callback_query(F.data == "queues")
async def show_queues(callback: CallbackQuery):
    await callback.answer()
    queues = get_queues()

    if not queues:
        await callback.message.answer(
            "📭 Очередей пока нет. Можно создать первую!",
            reply_markup=main_menu()
        )
        return

    await callback.message.answer(
        "📚 Все очереди — выбирай, где будем страдать:",
        reply_markup=queues_keyboard(callback.from_user.id)
    )


@dp.callback_query(F.data.startswith("view:"))
async def view_queue(callback: CallbackQuery):
    await callback.answer()
    queue_id = int(callback.data.split(":")[1])
    await callback.message.answer(
        queue_text(queue_id),
        reply_markup=queue_keyboard(queue_id)
    )


@dp.callback_query(F.data.startswith("place:"))
async def choose_place(callback: CallbackQuery):
    _, queue_id_text, place_text = callback.data.split(":")
    queue_id = int(queue_id_text)
    place = int(place_text)
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name or "Участник"

    if not get_queue(queue_id):
        await callback.answer("Очередь не найдена 👽", show_alert=True)
        return

    try:
        with sqlite3.connect(DB_FILE) as db:
            db.execute(
                "INSERT INTO bookings "
                "(queue_id, place, user_id, user_name) "
                "VALUES (?, ?, ?, ?)",
                (queue_id, place, user_id, user_name)
            )
    
    except sqlite3.IntegrityError:
        await callback.answer(
            "⛔ Место занято или ты уже записан! "
            "Очередь — не королевская битва.",
            show_alert=True
        )
        return

    await update_queue_announcement(queue_id)

    await callback.answer(f"🎉 Место № {place} твоё!")

    await callback.message.answer(
        queue_text(queue_id),
        reply_markup=queue_keyboard(queue_id)
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(callback: CallbackQuery):
    await callback.answer()
    queue_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    with sqlite3.connect(DB_FILE) as db:
        cursor = db.execute(
            "DELETE FROM bookings WHERE queue_id = ? AND user_id = ?",
            (queue_id, user_id)
        )

    if cursor.rowcount == 0:
        await callback.message.answer(
            "🤷 У тебя здесь нет записи. Отменять нечего."
        )
        return

    await update_queue_announcement(queue_id)

    await callback.message.answer(
        "🫡 Запись отменена. Место снова свободно!",
        reply_markup=main_menu()
    )
    await callback.message.answer(
        queue_text(queue_id),
        reply_markup=queue_keyboard(queue_id)
    )


@dp.callback_query(F.data.startswith("change:"))
async def change_booking(callback: CallbackQuery):
    await callback.answer()
    queue_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    if not get_user_booking(queue_id, user_id):
        await callback.message.answer(
            "👀 Сначала запишись в очередь, потом меняй место."
        )
        return

    waiting_for_change.add(user_id)
    waiting_for_change_queue[user_id] = queue_id
    await callback.message.answer(
        "🔄 На какой номер пересаживаемся? Напиши число от 1 до 32.\n"
        "Если место занято, запись не изменится."
    )


async def main():
    init_db()
    print("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
