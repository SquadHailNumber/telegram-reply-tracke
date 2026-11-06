import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "people.db")
TZ = ZoneInfo("Europe/Moscow")

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()


class AddPeople(StatesGroup):
    lines = State()


class EditField(StatesGroup):
    name = State()
    start = State()
    end = State()


def now_time():
    return datetime.now(TZ).strftime("%H:%M")


def valid_time(value):
    value = value.strip().replace(".", ":")
    for fmt in ("%H:%M", "%H"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def minutes_between(start, end):
    s = datetime.strptime(start, "%H:%M")
    e = datetime.strptime(end, "%H:%M")
    delta = int((e - s).total_seconds() // 60)
    if delta < 0:
        delta += 24 * 60
    return delta


def plural(value, one, few, many):
    n = value % 100
    if 11 <= n <= 14:
        return many
    n = value % 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def format_duration(minutes):
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if mins:
        parts.append(f"{mins} {plural(mins, 'минута', 'минуты', 'минут')}")
    return " ".join(parts) if parts else "0 минут"


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить людей списком", callback_data="add")],
        [InlineKeyboardButton(text="📋 Все люди", callback_data="list:all")],
        [InlineKeyboardButton(text="📊 Итог смены", callback_data="summary")],
        [InlineKeyboardButton(text="⏳ Кто не ответил", callback_data="list:pending")],
        [InlineKeyboardButton(text="🗑 Удалить несколько", callback_data="delete:select")],
    ])


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                answered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cur = await db.execute("PRAGMA table_info(people)")
        columns = {row[1] for row in await cur.fetchall()}
        if "end_time" not in columns:
            await db.execute("ALTER TABLE people ADD COLUMN end_time TEXT")
        await db.commit()


async def get_person(person_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM people WHERE id=? AND user_id=?", (person_id, user_id)
        )
        return await cur.fetchone()


async def fetch_people(user_id, pending=False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM people WHERE user_id=?"
        if pending:
            sql += " AND answered=0"
        sql += " ORDER BY time, id"
        cur = await db.execute(sql, (user_id,))
        return await cur.fetchall()


def person_line(row):
    status = "✅" if row["answered"] else "⬜"
    if row["end_time"]:
        duration = format_duration(minutes_between(row["time"], row["end_time"]))
        work = f"до {row['end_time']} / {duration}"
    else:
        work = "ещё работает"
    return f"{status} <b>{row['name']}</b> к {row['time']} — {work}"


def person_keyboard(row):
    pid = row["id"]
    toggle = "Снять ✅" if row["answered"] else "Отметить: ответил ✅"
    finish = "🔄 Изменить окончание" if row["end_time"] else "🏁 Закончил работу сейчас"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"toggle:{pid}")],
        [InlineKeyboardButton(text=finish, callback_data=f"finish:{pid}")],
        [InlineKeyboardButton(text="✏️ Изменить фамилию", callback_data=f"editname:{pid}")],
        [InlineKeyboardButton(text="🕒 Изменить время прихода", callback_data=f"editstart:{pid}")],
        [InlineKeyboardButton(text="🕒 Изменить время окончания", callback_data=f"editend:{pid}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"deleteone:{pid}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="list:all")],
    ])


async def render_list(user_id, pending=False):
    rows = await fetch_people(user_id, pending)
    if not rows:
        return ("Нет людей, ожидающих ответа." if pending else "Пока записей нет."), None
    title = "⏳ <b>Кто не ответил</b>" if pending else "📋 <b>Все люди</b>"
    text = title + "\n\n" + "\n".join(person_line(r) for r in rows)
    buttons = [[InlineKeyboardButton(
        text=f"{'✅' if r['answered'] else '⬜'} {r['name']} · {r['time']}",
        callback_data=f"person:{r['id']}"
    )] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_list(target, user_id, pending=False):
    text, markup = await render_list(user_id, pending)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


def parse_people_lines(raw):
    parsed, errors = [], []
    for number, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^(.+?)\s+(\d{1,2}(?::|\.?)\d{0,2})$", line)
        if not match:
            errors.append(f"Строка {number}: «{line}» — нужен формат Фамилия 07:00")
            continue
        name = match.group(1).strip()
        start = valid_time(match.group(2))
        if not name or not start:
            errors.append(f"Строка {number}: «{line}» — проверь фамилию и время")
            continue
        parsed.append((name, start))
    return parsed, errors


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Это бот учёта смен и ответов.\n\n"
        "Добавляй людей списком, отмечай ответы и окончание работы.",
        reply_markup=main_menu()
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Добавление: каждая строка — Фамилия ЧЧ:ММ.\n"
        "Например:\nКириллов 07:00\nГригорьев 07:00\n\n"
        "/list — все записи\n/pending — кто не ответил"
    )


@dp.message(Command("list"))
async def list_cmd(message: Message):
    await show_list(message, message.from_user.id)


@dp.message(Command("pending"))
async def pending_cmd(message: Message):
    await show_list(message, message.from_user.id, True)


@dp.callback_query(F.data == "menu")
async def menu_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Главное меню:", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "add")
async def add_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddPeople.lines)
    await call.message.edit_text(
        "Отправь список: один человек на каждой строке.\n\n"
        "Пример:\nКириллов 07:00\nГригорьев 07:00\nАллагуров 08:00\n\n"
        "Можно отправить сразу несколько строк."
    )
    await call.answer()


@dp.message(AddPeople.lines)
async def add_people_message(message: Message, state: FSMContext):
    parsed, errors = parse_people_lines(message.text or "")
    if not parsed:
        await message.answer("Не нашёл корректных строк. Формат: Фамилия 07:00\nПопробуй ещё раз.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for name, start in parsed:
            await db.execute(
                "INSERT INTO people(user_id,name,time,answered,created_at,end_time) VALUES(?,?,?,?,?,NULL)",
                (message.from_user.id, name, start, 0, datetime.now(TZ).isoformat())
            )
        await db.commit()
    await state.clear()
    result = f"✅ Добавлено людей: {len(parsed)}\n\n" + "\n".join(
        f"{name} — к {start}" for name, start in parsed
    )
    if errors:
        result += "\n\n⚠️ Не добавлены:\n" + "\n".join(errors)
    await message.answer(result, reply_markup=main_menu())


@dp.callback_query(F.data.startswith("list:"))
async def list_cb(call: CallbackQuery):
    mode = call.data.split(":", 1)[1]
    await show_list(call, call.from_user.id, mode == "pending")


@dp.callback_query(F.data.startswith("person:"))
async def person_cb(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    row = await get_person(pid, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    status = "Ответил ✅" if row["answered"] else "Не ответил ⬜"
    end = row["end_time"] or "не указано"
    duration = format_duration(minutes_between(row["time"], row["end_time"])) if row["end_time"] else "ещё работает"
    await call.message.edit_text(
        f"<b>{row['name']}</b>\nПриход: {row['time']}\nОкончание: {end}\n"
        f"Отработано: {duration}\nСтатус ответа: {status}",
        reply_markup=person_keyboard(row), parse_mode="HTML"
    )
    await call.answer()


async def refresh_person(call, pid):
    row = await get_person(pid, call.from_user.id)
    if row:
        status = "Ответил ✅" if row["answered"] else "Не ответил ⬜"
        end = row["end_time"] or "не указано"
        duration = format_duration(minutes_between(row["time"], row["end_time"])) if row["end_time"] else "ещё работает"
        await call.message.edit_text(
            f"<b>{row['name']}</b>\nПриход: {row['time']}\nОкончание: {end}\n"
            f"Отработано: {duration}\nСтатус ответа: {status}",
            reply_markup=person_keyboard(row), parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_cb(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    row = await get_person(pid, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE people SET answered=? WHERE id=? AND user_id=?",
                         (0 if row["answered"] else 1, pid, call.from_user.id))
        await db.commit()
    await call.answer("Отметка обновлена.")
    await refresh_person(call, pid)


@dp.callback_query(F.data.startswith("finish:"))
async def finish_cb(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    row = await get_person(pid, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    end = now_time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE people SET end_time=? WHERE id=? AND user_id=?",
                         (end, pid, call.from_user.id))
        await db.commit()
    await call.answer(f"Окончание записано: {end}")
    await refresh_person(call, pid)


@dp.callback_query(F.data.startswith("editname:"))
async def edit_name_cb(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    row = await get_person(pid, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(person_id=pid)
    await state.set_state(EditField.name)
    await call.message.edit_text(f"Напиши новое имя вместо «{row['name']}»:")
    await call.answer()


@dp.callback_query(F.data.startswith("editstart:"))
async def edit_start_cb(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    row = await get_person(pid, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(person_id=pid)
    await state.set_state(EditField.start)
    await call.message.edit_text(f"Укажи новое время прихода. Сейчас: {row['time']}\nФормат: 07:00")
    await call.answer()


@dp.callback_query(F.data.startswith("editend:"))
async def edit_end_cb(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    row = await get_person(pid, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(person_id=pid)
    await state.set_state(EditField.end)
    await call.message.edit_text(
        f"Укажи новое время окончания. Сейчас: {row['end_time'] or 'не указано'}\n"
        "Формат: 15:00"
    )
    await call.answer()


@dp.message(EditField.name)
async def edit_name_message(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не должно быть пустым. Напиши ещё раз:")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE people SET name=? WHERE id=? AND user_id=?",
                         (name, data["person_id"], message.from_user.id))
        await db.commit()
    await state.clear()
    await message.answer("✅ Имя изменено.", reply_markup=main_menu())


async def save_edited_time(message, state, field):
    value = valid_time(message.text or "")
    if not value:
        await message.answer("Неверный формат. Введи время, например 15:00:")
        return
    data = await state.get_data()
    column = "time" if field == "start" else "end_time"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE people SET {column}=? WHERE id=? AND user_id=?",
                         (value, data["person_id"], message.from_user.id))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Время {'прихода' if field == 'start' else 'окончания'} изменено на {value}.",
                         reply_markup=main_menu())


@dp.message(EditField.start)
async def edit_start_message(message: Message, state: FSMContext):
    await save_edited_time(message, state, "start")


@dp.message(EditField.end)
async def edit_end_message(message: Message, state: FSMContext):
    await save_edited_time(message, state, "end")


@dp.callback_query(F.data == "summary")
async def summary_cb(call: CallbackQuery):
    rows = await fetch_people(call.from_user.id)
    if not rows:
        await call.answer("Список пуст.", show_alert=True)
        return
    lines = []
    total_minutes = 0
    finished = 0
    for row in rows:
        if row["end_time"]:
            mins = minutes_between(row["time"], row["end_time"])
            total_minutes += mins
            finished += 1
            lines.append(f"<b>{row['name']}</b> к {row['time']} до {row['end_time']} / {format_duration(mins)}")
        else:
            lines.append(f"<b>{row['name']}</b> к {row['time']} — ещё работает")
    text = "📊 <b>Итог смены</b>\n\n" + "\n".join(lines)
    text += f"\n\nЗавершили: {finished} из {len(rows)}"
    text += f"\nСуммарно отработано: {format_duration(total_minutes)}"
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить итог", callback_data="summary")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]
    ]), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("deleteone:"))
async def delete_one_cb(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    row = await get_person(pid, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, удалить", callback_data=f"confirmdelete:{pid}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"person:{pid}")]
    ])
    await call.message.edit_text(f"Точно удалить «{row['name']}»? Удаление окончательное.", reply_markup=markup)
    await call.answer()


@dp.callback_query(F.data.startswith("confirmdelete:"))
async def confirm_delete_cb(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM people WHERE id=? AND user_id=?", (pid, call.from_user.id))
        await db.commit()
    await call.message.edit_text("🗑 Запись удалена.", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "delete:select")
async def delete_select_cb(call: CallbackQuery, state: FSMContext):
    rows = await fetch_people(call.from_user.id)
    if not rows:
        await call.answer("Список пуст.", show_alert=True)
        return
    await state.update_data(delete_ids=[])
    buttons = [[InlineKeyboardButton(text=f"⬜ {r['name']} · {r['time']}",
                                     callback_data=f"pickdelete:{r['id']}")] for r in rows]
    buttons += [
        [InlineKeyboardButton(text="🗑 Удалить выбранных", callback_data="delete:confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="menu")]
    ]
    await call.message.edit_text("Выбери людей для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@dp.callback_query(F.data.startswith("pickdelete:"))
async def pick_delete_cb(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    data = await state.get_data()
    ids = set(data.get("delete_ids", []))
    ids.symmetric_difference_update({pid})
    await state.update_data(delete_ids=list(ids))
    rows = await fetch_people(call.from_user.id)
    buttons = [[InlineKeyboardButton(
        text=f"{'☑️' if r['id'] in ids else '⬜'} {r['name']} · {r['time']}",
        callback_data=f"pickdelete:{r['id']}"
    )] for r in rows]
    buttons += [
        [InlineKeyboardButton(text=f"🗑 Удалить выбранных ({len(ids)})", callback_data="delete:confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="menu")]
    ]
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@dp.callback_query(F.data == "delete:confirm")
async def confirm_multi_delete_cb(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ids = data.get("delete_ids", [])
    if not ids:
        await call.answer("Сначала выбери хотя бы одного человека.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in ids)
        await db.execute(f"DELETE FROM people WHERE user_id=? AND id IN ({placeholders})",
                         [call.from_user.id, *ids])
        await db.commit()
    await state.clear()
    await call.message.edit_text(f"🗑 Удалено записей: {len(ids)}.", reply_markup=main_menu())
    await call.answer()


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
