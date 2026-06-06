import asyncio
import logging
import sys

import pandas as pd
import re
from pathlib import Path
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, Document, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== НАСТРОЙКИ ====================
TOKEN = "8667492386:AAFHLl9HCkbO36I2Rys7t0QMjLG3ibWyO1M"  # ← Замени!
ADMIN_PASSWORD = "admin123"
# ===================================================

dp = Dispatcher(storage=MemoryStorage())
DB_PATH = "schedule.db"


class AdminStates(StatesGroup):
    waiting_for_password = State()
    waiting_for_new_password = State()
    waiting_for_feedback = State()


# ==================== БАЗА ДАННЫХ ====================
async def init_db():
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DROP TABLE IF EXISTS schedule")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY,
                group_name TEXT,
                day TEXT,
                date TEXT,
                time_start TEXT,
                time_end TEXT,
                subject TEXT,
                cabinet TEXT
            )
        ''')
        await db.execute("DROP TABLE IF EXISTS users")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                role TEXT DEFAULT 'user'
            )
        ''')
        await db.execute("DROP TABLE IF EXISTS admin_settings")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS admin_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.execute("DROP TABLE IF EXISTS feedback")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                username TEXT,
                text TEXT,
                date TEXT,
                is_read INTEGER DEFAULT 0
            )
        ''')
        await db.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('password', ?)", (ADMIN_PASSWORD,))
        await db.commit()


async def get_admin_password():
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM admin_settings WHERE key = 'password'") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else ADMIN_PASSWORD


async def set_admin_password(new_password: str):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE admin_settings SET value = ? WHERE key = 'password'", (new_password,))
        await db.commit()


async def save_user(user_id: int, username: str = None, role: str = "user"):
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('''
                INSERT OR REPLACE INTO users (user_id, username, role)
                VALUES (?, ?, ?)
            ''', (user_id, username, role))
            await db.commit()
    except:
        pass


async def notify_regular_users(bot: Bot, text: str):
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE role = 'user'") as cursor:
                users = await cursor.fetchall()
        for (user_id,) in users:
            try:
                await bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
            except:
                pass
    except:
        pass


async def add_feedback(user_id: int, username: str, text: str):
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('''
                INSERT INTO feedback (user_id, username, text, date)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, text, datetime.now().strftime("%d.%m.%Y %H:%M")))
            await db.commit()
    except Exception as e:
        logging.error(f"Ошибка сохранения отзыва: {e}")


async def get_unread_feedback_count():
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM feedback WHERE is_read = 0") as cursor:
                result = await cursor.fetchone()
                return result[0] if result else 0
    except:
        return 0


async def clear_all_feedback():
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM feedback")
            await db.commit()
        return True
    except:
        return False


# ==================== АДАПТИВНЫЙ ПАРСИНГ ====================
COLUMN_VARIANTS = {
    'group': ['Группа', 'group', 'группа', 'Group'],
    'day': ['День недели', 'День', 'day'],
    'date': ['Дата', 'date', 'Date'],
    'time': ['Время', 'время', 'Time', 'time', 'Пара'],
    'subject': ['Предмет', 'subject', 'Subject', 'Название'],
    'cabinet': ['Кабинет', 'cabinet', 'Аудитория', 'Каб']
}


def find_best_column(df_columns, variants):
    for col in df_columns:
        col_lower = str(col).strip().lower()
        for v in variants:
            if v.lower() in col_lower or col_lower in v.lower():
                return col
    return None


async def load_excel_to_db(file_path: str):
    try:
        df = pd.read_excel(file_path, dtype=str)
        df.columns = [str(col).strip() for col in df.columns]
        logging.info(f"Колонки: {list(df.columns)}")

        group_col = find_best_column(df.columns, COLUMN_VARIANTS['group'])
        day_col = find_best_column(df.columns, COLUMN_VARIANTS['day'])
        date_col = find_best_column(df.columns, COLUMN_VARIANTS['date'])
        time_col = find_best_column(df.columns, COLUMN_VARIANTS['time'])
        subject_col = find_best_column(df.columns, COLUMN_VARIANTS['subject'])
        cabinet_col = find_best_column(df.columns, COLUMN_VARIANTS['cabinet'])

        if not group_col or not subject_col:
            return False, 0, "Не найдены колонки Группа или Предмет"

        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM schedule")
            inserted = 0

            for _, row in df.iterrows():
                group = str(row.get(group_col, '')).strip()
                subject = str(row.get(subject_col, '')).strip()
                if not group or not subject or subject.lower() in ["nan", ""]:
                    continue

                time_str = str(row.get(time_col, '')).strip() if time_col else ""
                cabinet_str = str(row.get(cabinet_col, '')).strip() if cabinet_col else ""

                # Парсинг времени из строки типа "35 9:00 - 9:35"
                time_start = ""
                time_end = ""
                if time_str:
                    match = re.search(r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})', time_str)
                    if match:
                        time_start = match.group(1)
                        time_end = match.group(2)

                await db.execute('''
                    INSERT INTO schedule 
                    (group_name, day, date, time_start, time_end, subject, cabinet)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    group,
                    str(row.get(day_col, '')).strip(),
                    str(row.get(date_col, '')).strip(),
                    time_start,
                    time_end,
                    subject,
                    cabinet_str
                ))
                inserted += 1

            await db.commit()

        return True, inserted, None

    except Exception as e:
        logging.error(f"Ошибка Excel: {e}")
        return False, 0, str(e)


async def has_schedule() -> bool:
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM schedule") as cursor:
                result = await cursor.fetchone()
                return result[0] > 0 if result else False
    except:
        return False


async def get_main_menu(is_admin: bool = False):
    keyboard = []
    if await has_schedule():
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT DISTINCT group_name FROM schedule") as cursor:
                groups = [row[0] for row in await cursor.fetchall()]
        for group in groups:
            keyboard.append([InlineKeyboardButton(text=f"{group}", callback_data=f"group:{group}")])
    else:
        keyboard.append([InlineKeyboardButton(text="Расписание ещё не загружено", callback_data="no_schedule")])

    if is_admin:
        unread = await get_unread_feedback_count()
        feedback_text = f"Отзывы ({unread})" if unread > 0 else "Отзывы"
        keyboard.append([InlineKeyboardButton(text="Загрузить новое расписание", callback_data="upload_info")])
        keyboard.append([InlineKeyboardButton(text="🗑 Очистить расписание", callback_data="clear_db")])
        keyboard.append([InlineKeyboardButton(text=feedback_text, callback_data="admin_feedback")])
        keyboard.append([InlineKeyboardButton(text="🗑 Очистить все отзывы", callback_data="clear_feedback")])
        keyboard.append([InlineKeyboardButton(text="Сменить пароль", callback_data="change_password")])
    else:
        keyboard.append([InlineKeyboardButton(text="Написать отзыв", callback_data="feedback")])

    keyboard.append([InlineKeyboardButton(text="Сменить роль", callback_data="change_role")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def create_schedule_text(rows, group: str) -> str:
    lines = [f"🎓 <b>{group.upper()}</b>\n"]
    lines.append("═" * 40 + "\n")
    
    current_date = None
    
    for row in rows:
        day, date, t_start, t_end, subject, cabinet = row
        
        # Заголовок дня
        if date != current_date and date:
            if current_date is not None:
                lines.append("\n")
            emoji_day = {"Понедельник": "1️⃣", "Вторник": "2️⃣", "Среда": "3️⃣", 
                        "Четверг": "4️⃣", "Пятница": "5️⃣", "Суббота": "6️⃣"}.get(day, "📅")
            lines.append(f"{emoji_day} <b>{day}</b> • {date}")
            lines.append("─" * 40)
            current_date = date
        
        # Время
        time_display = f"{t_start}–{t_end}" if t_start and t_end else "—"
        
        # Кабинет
        cab_display = f"📍 {cabinet}" if cabinet and str(cabinet).strip() not in ["nan", "—", ""] else ""
        
        # Формат: время | предмет | кабинет
        line = f"<code>{time_display:>10}</code> | {subject}"
        if cab_display:
            line += f"\n{'':>13}{cab_display}"
        
        lines.append(line)
    
    lines.append("\n" + "═" * 40)
    
    return "\n".join(lines)


# ==================== ХЕНДЛЕРЫ ====================

@dp.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext):
    await save_user(message.from_user.id, message.from_user.username, "user")
    await state.clear()
    await message.answer(
        "<b>Добро пожаловать!</b>\n\nВыберите режим:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Обычный пользователь", callback_data="role:user")],
            [InlineKeyboardButton(text="👑 Администратор", callback_data="role:admin")]
        ])
    )


@dp.callback_query(lambda c: c.data == "change_role")
async def change_role(callback: CallbackQuery, state: FSMContext):
    await command_start_handler(callback.message, state)


@dp.callback_query(lambda c: c.data.startswith("role:"))
async def choose_role(callback: CallbackQuery, state: FSMContext):
    role = callback.data.split(":")[1]
    await save_user(callback.from_user.id, callback.from_user.username, role)

    if role == "user":
        if not await has_schedule():
            await callback.message.edit_text(
                "<b>Расписание ещё не добавлено</b>\n\nАдминистратор пока не загрузил расписание.",
                parse_mode=ParseMode.HTML,
                reply_markup=await get_main_menu(False)
            )
        else:
            await callback.message.edit_text(
                "<b>Режим: Обычный пользователь</b>\n\nВыберите группу:",
                parse_mode=ParseMode.HTML,
                reply_markup=await get_main_menu(False)
            )
    else:
        await callback.message.edit_text("🔑Введите пароль администратора:")
        await state.set_state(AdminStates.waiting_for_password)


@dp.message(AdminStates.waiting_for_password)
async def check_password(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Введите пароль.")
        return

    if message.text.strip() == await get_admin_password():
        await state.clear()
        await save_user(message.from_user.id, message.from_user.username, "admin")
        await message.answer(
            "<b>Администратор успешно авторизован!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=await get_main_menu(True)
        )
    else:
        await message.answer("❌ Неверный пароль!\n\nПопробуйте ещё раз или /start")


@dp.callback_query(lambda c: c.data.startswith("group:"))
async def show_group_schedule(callback: CallbackQuery):
    group = callback.data.split(":", 1)[1]

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT day, date, time_start, time_end, subject, cabinet 
            FROM schedule WHERE group_name = ? ORDER BY date, time_start
        """, (group,)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await callback.message.edit_text("❌Расписание для этой группы отсутствует.",
                                         reply_markup=await get_main_menu(False))
        return

    text = create_schedule_text(rows, group)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Главное меню", callback_data="main_menu")]
    ])

    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


@dp.callback_query(lambda c: c.data == "main_menu")
async def back_to_main(callback: CallbackQuery):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role FROM users WHERE user_id = ?",
                              (callback.from_user.id,)) as cursor:
            result = await cursor.fetchone()
            is_admin = result and result[0] == "admin"

    await callback.message.edit_text(
        "<b>Главное меню</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=await get_main_menu(is_admin)
    )


@dp.callback_query(lambda c: c.data == "upload_info")
async def upload_info(callback: CallbackQuery):
    await callback.message.edit_text(
        "Отправьте мне файл <b>расписание.xlsx</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="main_menu")]
        ])
    )


@dp.callback_query(lambda c: c.data == "clear_db")
async def clear_db(callback: CallbackQuery):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM schedule")
        await db.commit()
    await callback.message.edit_text("🗑 Расписание очищено.", reply_markup=await get_main_menu(True))


@dp.callback_query(lambda c: c.data == "change_password")
async def change_password(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔑 Введите новый пароль администратора:")
    await state.set_state(AdminStates.waiting_for_new_password)


@dp.message(AdminStates.waiting_for_new_password)
async def set_new_password(message: Message, state: FSMContext):
    if message.text:
        await set_admin_password(message.text.strip())
        await state.clear()
        await message.answer("Пароль успешно изменён!", reply_markup=await get_main_menu(True))


@dp.callback_query(lambda c: c.data == "feedback")
async def start_feedback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Напишите ваш отзыв, предложение или сообщение об ошибке:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Отмена", callback_data="main_menu")]
        ])
    )
    await state.set_state(AdminStates.waiting_for_feedback)


@dp.message(AdminStates.waiting_for_feedback)
async def receive_feedback(message: Message, state: FSMContext):
    await add_feedback(message.from_user.id, message.from_user.username, message.text)
    await state.clear()
    await message.answer("Спасибо! Ваш отзыв отправлен администратору.")


@dp.callback_query(lambda c: c.data == "admin_feedback")
async def show_feedback(callback: CallbackQuery):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, username, text, date FROM feedback 
            WHERE is_read = 0 ORDER BY date DESC
        """) as cursor:
            feedbacks = await cursor.fetchall()

    if not feedbacks:
        await callback.message.edit_text("Нет новых отзывов.", reply_markup=await get_main_menu(True))
        return

    text = "<b>Новые отзывы</b>\n\n"
    for fid, username, ftext, fdate in feedbacks:
        text += f"👤 {username} ({fdate})\n{ftext}\n\n"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE feedback SET is_read = 1 WHERE is_read = 0")

    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu(True))


@dp.callback_query(lambda c: c.data == "clear_feedback")
async def clear_feedback(callback: CallbackQuery):
    if await clear_all_feedback():
        await callback.message.edit_text("Все отзывы полностью удалены.",
                                         reply_markup=await get_main_menu(True))
    else:
        await callback.message.edit_text("❌ Ошибка при очистке отзывов.",
                                         reply_markup=await get_main_menu(True))


@dp.message(F.document)
async def handle_document(message: Message):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            result = await cursor.fetchone()
            role = result[0] if result else "user"

    if role != "admin":
        await message.answer("⛔ Только администратор может загружать расписание.")
        return

    document = message.document
    if not document.file_name.lower().endswith(('.xlsx', '.xls')):
        await message.answer("❌ Только .xlsx и .xls файлы")
        return

    await message.answer("📤 Обрабатываю файл...")

    file = await message.bot.get_file(document.file_id)
    file_path = f"temp_{document.file_name}"

    await message.bot.download_file(file.file_path, file_path)
    success, count, warning = await load_excel_to_db(file_path)
    Path(file_path).unlink(missing_ok=True)

    if success:
        msg = f"✅ Расписание успешно загружено!\nЗаписей: <b>{count}</b>"
        if warning:
            msg += f"\n{warning}"
        await message.answer(msg, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu(True))
        await notify_regular_users(message.bot, "<b>Расписание обновлено!</b>")
    else:
        await message.answer(f"❌ Ошибка обработки файла.", parse_mode=ParseMode.HTML)


async def main():
    await init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logging.info("Бот запущен — исправленный парсинг времени")
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
