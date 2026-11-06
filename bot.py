import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "people.db")
LOCAL_TZ = ZoneInfo("Europe/Moscow")

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавьте токен в переменные окружения.")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()


class AddPerson(StatesGroup):
    name = State()
    time = State()


class EditPerson(StatesGroup):
    name = State()
    time = State()


def parse_time(value: str):
    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def parse_name_and_time(value: str):
    match = re.fullmatch(r"\s*(.+?)\s+(\d{1,2}:\d{2})\s*", value or "")
    if not match:
        return None
    name = match.group(1).strip()
    time_value = parse_time(match.group(2))
    if not name or not time_value:
        return None
    return name, time_value


def parse_batch_lines(value: str):
    """Разбирает список строк вида «Фамилия 07:00»."""
    parsed = []
    invalid = []
    for line_number, line in enumerate((value or "").splitlines(), start=1):
        if not line.strip():
            continue
        item = parse_name_and_time(line)
        if item:
            parsed.append(item)
        else:
            invalid.append((line_number, line.strip()))
    return parsed, invalid


def worked_duration(start_time: str, finished_at: str):
    end_dt = datetime.fromisoformat(finished_at)
    start_clock = datetime.strptime(start_time, "%H:%M").time()
    start_dt = datetime.combine(end_dt.date(), start_clock)
    # Если время начала позже времени окончания, смена началась накануне.
    if start_dt > end_dt:
        start_dt -= timedelta(days=1)
    return max(end_dt - start_dt, timedelta(0))


def duration_text(duration: timedelta):
    total_minutes = int(duration.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if minutes:
        return f"{hours} ч {minutes} мин"
    return f"{hours} ч"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                answered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
        """)
        # Миграция существующей базы: добавляем время окончания, не удаляя старые записи.
        cur = await db.execute("PRAGMA table_info(people)")
        columns = {row[1] for row in await cur.fetchall()}
        if "finished_at" not in columns:
            await db.execute("ALTER TABLE people ADD COLUMN finished_at TEXT")
        await db.commit()


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить человека", callback_data="add")],
        [InlineKeyboardButton(text="📋 Все люди", callback_data="list:all")],
        [InlineKeyboardButton(text="⏳ Кто ещё работает", callback_data="list:pending")],
        [InlineKeyboardButton(text="🗑 Удалить несколько", callback_data="delete:select")],
    ])


async def get_person(person_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM people WHERE id=? AND user_id=?", (person_id, user_id)
        )
        return await cur.fetchone()


async def insert_person(user_id: int, name: str, time_value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO people(user_id,name,time,answered,created_at,finished_at) VALUES(?,?,?,?,?,NULL)",
            (user_id, name, time_value, 0, datetime.now(LOCAL_TZ).isoformat())
        )
        await db.commit()


async def render_list(user_id: int, mode: str):
    condition = ""
    if mode == "pending":
        condition = " AND finished_at IS NULL"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM people WHERE user_id=?{condition} ORDER BY time, id",
            (user_id,)
        )
        rows = await cur.fetchall()

    if not rows:
        return ("Пока записей нет." if mode == "all" else "Нет людей, которые ещё работают."), None

    finished_count = sum(row["finished_at"] is not None for row in rows)
    title = "Все люди" if mode == "all" else "Ещё работают"
    text = f"📋 <b>{title}</b>\n"
    if mode == "all":
        text += f"Закончили: {finished_count} из {len(rows)}\n\n"
    else:
        text += f"В работе: {len(rows)}\n\n"

    buttons = []
    for row in rows:
        name = html.escape(row["name"])
        if row["finished_at"]:
            duration = duration_text(worked_duration(row["time"], row["finished_at"]))
            status = "🏁"
            detail = f"{row['time']} · {duration}"
        else:
            status = "⏳"
            detail = f"с {row['time']}"
        answer_mark = "✅" if row["answered"] else "⬜"
        text += f"{status} <b>{name}</b> — {detail} · Ответил: {answer_mark}\n"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {answer_mark} {row['name']} · {detail}",
            callback_data=f"person:{row['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_list(target, user_id: int, mode: str):
    text, markup = await render_list(user_id, mode)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


def person_keyboard(person):
    person_id = person["id"]
    finish_button = (
        InlineKeyboardButton(text="↩️ Снять отметку окончания", callback_data=f"unfinish:{person_id}")
        if person["finished_at"] else
        InlineKeyboardButton(text="🏁 Закончил работу", callback_data=f"finish:{person_id}")
    )
    toggle_text = "✅ Ответил — снять отметку" if person["answered"] else "⬜ Отметить: ответил"
    return InlineKeyboardMarkup(inline_keyboard=[
        [finish_button],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"toggle:{person_id}")],
        [InlineKeyboardButton(text="✏️ Изменить фамилию", callback_data=f"editname:{person_id}")],
        [InlineKeyboardButton(text="🕒 Изменить время начала", callback_data=f"edittime:{person_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"deleteone:{person_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="list:all")],
    ])


def person_text(person):
    answer_status = "✅ Да" if person["answered"] else "⬜ Нет"
    text = f"<b>{html.escape(person['name'])}</b>\nНачало работы: {person['time']}\nОтветил: {answer_status}\n"
    if person["finished_at"]:
        end_dt = datetime.fromisoformat(person["finished_at"])
        duration = duration_text(worked_duration(person["time"], person["finished_at"]))
        text += f"Окончание: {end_dt.strftime('%H:%M')}\nОтработано: <b>{duration}</b>\n"
        text += "Статус: 🏁 Закончил работу"
    else:
        text += "Окончание: ещё не отмечено\nСтатус: ⏳ Работает"
    return text


async def refresh_person(call: CallbackQuery, person_id: int):
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.message.edit_text("Запись не найдена.", reply_markup=main_menu())
        return
    await call.message.edit_text(
        person_text(person), reply_markup=person_keyboard(person), parse_mode="HTML"
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Это бот для учёта рабочего времени.\n\n"
        "Добавляй людей с временем начала работы, а когда человек закончит — нажимай «Закончил работу».\n"
        "Бот сам посчитает продолжительность смены.",
        reply_markup=main_menu()
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Команды:\n/start — главное меню\n/list — все записи\n/pending — кто ещё работает\n\n"
        "Добавление одного человека: Иванов 07:00\n"
        "Добавление нескольких: каждый человек с новой строки, например:\n"
        "Григорьев 07:00\nАллагуров 07:00\nКириллов 08:00"
    )


@dp.message(Command("list"))
async def list_cmd(message: Message):
    await show_list(message, message.from_user.id, "all")


@dp.message(Command("pending"))
async def pending_cmd(message: Message):
    await show_list(message, message.from_user.id, "pending")


@dp.callback_query(F.data == "menu")
async def menu_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Главное меню:", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "add")
async def add_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddPerson.name)
    await call.message.edit_text(
        "Напиши фамилию и время начала одной строкой, например:\n<b>Иванов 07:00</b>\n\n"
        "Можно также написать только фамилию — тогда бот отдельно спросит время.",
        parse_mode="HTML"
    )
    await call.answer()


@dp.message(AddPerson.name)
async def add_name(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Напиши фамилию и время, например: Иванов 07:00")
        return

    # Несколько строк — добавляем все корректные записи за один раз.
    if "\n" in value:
        parsed_rows, invalid_rows = parse_batch_lines(value)
        if not parsed_rows:
            await message.answer(
                "Не нашёл корректных строк. Используй формат:\n"
                "Григорьев 07:00\nАллагуров 07:00\nКириллов 08:00"
            )
            return
        for name, time_value in parsed_rows:
            await insert_person(message.from_user.id, name, time_value)
        await state.clear()
        result = ["✅ <b>Добавлены записи:</b>"]
        result.extend(f"• {html.escape(name)} — {time_value}" for name, time_value in parsed_rows)
        if invalid_rows:
            result.append("\n⚠️ <b>Не добавлены (проверь формат):</b>")
            result.extend(f"• Строка {number}: {html.escape(line)}" for number, line in invalid_rows)
        result.append(f"\nВсего добавлено: {len(parsed_rows)}")
        await message.answer("\n".join(result), reply_markup=main_menu(), parse_mode="HTML")
        return

    parsed = parse_name_and_time(value)
    if parsed:
        name, time_value = parsed
        await insert_person(message.from_user.id, name, time_value)
        await state.clear()
        await message.answer(
            f"✅ Добавлен: <b>{html.escape(name)}</b>\nНачало работы: {time_value}",
            reply_markup=main_menu(), parse_mode="HTML"
        )
        return

    await state.update_data(name=value)
    await state.set_state(AddPerson.time)
    await message.answer("Теперь укажи время начала работы (например, 07:00):")


@dp.message(AddPerson.time)
async def add_time(message: Message, state: FSMContext):
    time_value = parse_time((message.text or "").strip())
    if not time_value:
        await message.answer("Введи время в формате ЧЧ:ММ, например 07:00:")
        return
    data = await state.get_data()
    await insert_person(message.from_user.id, data["name"], time_value)
    await state.clear()
    await message.answer(
        f"✅ Добавлен: <b>{html.escape(data['name'])}</b>, начало работы {time_value}.",
        reply_markup=main_menu(), parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("list:"))
async def list_cb(call: CallbackQuery):
    mode = call.data.split(":", 1)[1]
    await show_list(call, call.from_user.id, mode)


@dp.callback_query(F.data.startswith("person:"))
async def person_cb(call: CallbackQuery):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await call.message.edit_text(
        person_text(person), reply_markup=person_keyboard(person), parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("finish:"))
async def finish_cb(call: CallbackQuery):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    if person["finished_at"]:
        await call.answer("Окончание уже отмечено.", show_alert=True)
        return
    finished_at = datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="minutes")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE people SET finished_at=? WHERE id=? AND user_id=? AND finished_at IS NULL",
            (finished_at, person_id, call.from_user.id)
        )
        await db.commit()
    await call.answer("Время окончания сохранено.")
    await refresh_person(call, person_id)


@dp.callback_query(F.data.startswith("unfinish:"))
async def unfinish_cb(call: CallbackQuery):
    person_id = int(call.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE people SET finished_at=NULL WHERE id=? AND user_id=?",
            (person_id, call.from_user.id)
        )
        await db.commit()
    await call.answer("Отметка окончания снята.")
    await refresh_person(call, person_id)


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_cb(call: CallbackQuery):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    new_value = 0 if person["answered"] else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE people SET answered=? WHERE id=? AND user_id=?",
            (new_value, person_id, call.from_user.id)
        )
        await db.commit()
    await call.answer("Статус обновлён.")
    await refresh_person(call, person_id)


@dp.callback_query(F.data.startswith("editname:"))
async def edit_name_cb(call: CallbackQuery, state: FSMContext):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(person_id=person_id)
    await state.set_state(EditPerson.name)
    await call.message.edit_text(f"Напиши новую фамилию для записи «{html.escape(person['name'])}»:", parse_mode="HTML")
    await call.answer()


@dp.message(EditPerson.name)
async def edit_name_message(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Фамилия не должна быть пустой. Попробуй ещё раз:")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE people SET name=? WHERE id=? AND user_id=?",
            (name, data["person_id"], message.from_user.id)
        )
        await db.commit()
    await state.clear()
    await message.answer("✅ Фамилия изменена.", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("edittime:"))
async def edit_time_cb(call: CallbackQuery, state: FSMContext):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(person_id=person_id)
    await state.set_state(EditPerson.time)
    await call.message.edit_text("Напиши новое время начала работы в формате ЧЧ:ММ:")
    await call.answer()


@dp.message(EditPerson.time)
async def edit_time_message(message: Message, state: FSMContext):
    time_value = parse_time((message.text or "").strip())
    if not time_value:
        await message.answer("Неверный формат. Введи, например, 07:00:")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE people SET time=? WHERE id=? AND user_id=?",
            (time_value, data["person_id"], message.from_user.id)
        )
        await db.commit()
    await state.clear()
    await message.answer("✅ Время начала изменено.", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("deleteone:"))
async def delete_one_cb(call: CallbackQuery):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, удалить", callback_data=f"confirmdelete:{person_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"person:{person_id}")],
    ])
    await call.message.edit_text(
        f"Точно удалить «{html.escape(person['name'])}»? Удаление окончательное.",
        reply_markup=markup, parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("confirmdelete:"))
async def confirm_delete_cb(call: CallbackQuery):
    person_id = int(call.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM people WHERE id=? AND user_id=?",
            (person_id, call.from_user.id)
        )
        await db.commit()
    await call.answer("Запись удалена.")
    await call.message.edit_text("🗑 Запись удалена.", reply_markup=main_menu())


@dp.callback_query(F.data == "delete:select")
async def delete_select_cb(call: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id,name,time FROM people WHERE user_id=? ORDER BY time,id",
            (call.from_user.id,)
        )
        rows = await cur.fetchall()
    if not rows:
        await call.answer("Список пуст.", show_alert=True)
        return
    await state.update_data(delete_ids=[])
    buttons = [[InlineKeyboardButton(
        text=f"⬜ {r['name']} · {r['time']}", callback_data=f"pickdelete:{r['id']}"
    )] for r in rows]
    buttons.append([InlineKeyboardButton(text="🗑 Удалить выбранных", callback_data="delete:confirm")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="menu")])
    await call.message.edit_text(
        "Выбери людей для удаления. Нажимай на имя, чтобы отметить или снять отметку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("pickdelete:"))
async def pick_delete_cb(call: CallbackQuery, state: FSMContext):
    person_id = int(call.data.split(":")[1])
    data = await state.get_data()
    ids = set(data.get("delete_ids", []))
    if person_id in ids:
        ids.remove(person_id)
    else:
        ids.add(person_id)
    await state.update_data(delete_ids=list(ids))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id,name,time FROM people WHERE user_id=? ORDER BY time,id",
            (call.from_user.id,)
        )
        rows = await cur.fetchall()
    buttons = [[InlineKeyboardButton(
        text=f"{'☑️' if r['id'] in ids else '⬜'} {r['name']} · {r['time']}",
        callback_data=f"pickdelete:{r['id']}"
    )] for r in rows]
    buttons.append([InlineKeyboardButton(text=f"🗑 Удалить выбранных ({len(ids)})", callback_data="delete:confirm")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="menu")])
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
        await db.execute(
            f"DELETE FROM people WHERE user_id=? AND id IN ({placeholders})",
            [call.from_user.id, *ids]
        )
        await db.commit()
    await state.clear()
    await call.message.edit_text(
        f"🗑 Удалено записей: {len(ids)}. Восстановить их нельзя.",
        reply_markup=main_menu()
    )
    await call.answer()


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
