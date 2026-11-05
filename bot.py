import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "people.db")

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
        await db.commit()


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить человека", callback_data="add")],
        [InlineKeyboardButton(text="📋 Все люди", callback_data="list:all")],
        [InlineKeyboardButton(text="⏳ Кто не ответил", callback_data="list:pending")],
        [InlineKeyboardButton(text="🗑 Удалить несколько", callback_data="delete:select")],
    ])


async def get_person(person_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM people WHERE id=? AND user_id=?", (person_id, user_id)
        )
        return await cur.fetchone()


async def render_list(user_id: int, mode: str):
    condition = ""
    if mode == "pending":
        condition = " AND answered=0"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM people WHERE user_id=?{condition} ORDER BY time, id",
            (user_id,)
        )
        rows = await cur.fetchall()

    if not rows:
        return "Пока записей нет." if mode == "all" else "Нет людей, ожидающих ответа.", None

    answered = sum(row["answered"] for row in rows)
    text = f"📋 <b>{'Все люди' if mode == 'all' else 'Ожидают ответа'}</b>\n"
    if mode == "all":
        text += f"Ответили: {answered} из {len(rows)}\n\n"
    else:
        text += f"Не ответили: {len(rows)}\n\n"

    buttons = []
    for row in rows:
        status = "✅" if row["answered"] else "⬜"
        text += f"{status} <b>{row['name']}</b> — {row['time']}\n"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {row['name']} · {row['time']}",
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


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Это бот для учёта ответов.\n\n"
        "Добавляй людей, указывай время и отмечай, кто ответил.",
        reply_markup=main_menu()
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Команды:\n/start — главное меню\n/list — все записи\n/pending — кто не ответил"
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
    await call.message.edit_text("Напиши имя или фамилию человека:")
    await call.answer()


@dp.message(AddPerson.name)
async def add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не должно быть пустым. Напиши ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(AddPerson.time)
    await message.answer("Теперь укажи время (например, 08:30):")


@dp.message(AddPerson.time)
async def add_time(message: Message, state: FSMContext):
    time_value = (message.text or "").strip()
    try:
        datetime.strptime(time_value, "%H:%M")
    except ValueError:
        await message.answer("Введи время в формате ЧЧ:ММ, например 08:30:")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO people(user_id,name,time,answered,created_at) VALUES(?,?,?,?,?)",
            (message.from_user.id, data["name"], time_value, 0, datetime.now().isoformat())
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"✅ Добавлен: <b>{data['name']}</b>, время {time_value}.",
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
    status = "Ответил ✅" if person["answered"] else "Не ответил ⬜"
    toggle_text = "Снять отметку" if person["answered"] else "Отметить: ответил"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"toggle:{person_id}")],
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data=f"editname:{person_id}")],
        [InlineKeyboardButton(text="🕒 Изменить время", callback_data=f"edittime:{person_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"deleteone:{person_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="list:all")],
    ])
    await call.message.edit_text(
        f"<b>{person['name']}</b>\nВремя: {person['time']}\nСтатус: {status}",
        reply_markup=markup, parse_mode="HTML"
    )
    await call.answer()


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
    await person_cb_refresh(call, person_id)


async def person_cb_refresh(call: CallbackQuery, person_id: int):
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.message.edit_text("Запись не найдена.", reply_markup=main_menu())
        return
    status = "Ответил ✅" if person["answered"] else "Не ответил ⬜"
    toggle_text = "Снять отметку" if person["answered"] else "Отметить: ответил"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"toggle:{person_id}")],
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data=f"editname:{person_id}")],
        [InlineKeyboardButton(text="🕒 Изменить время", callback_data=f"edittime:{person_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"deleteone:{person_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="list:all")],
    ])
    await call.message.edit_text(
        f"<b>{person['name']}</b>\nВремя: {person['time']}\nСтатус: {status}",
        reply_markup=markup, parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("editname:"))
async def edit_name_cb(call: CallbackQuery, state: FSMContext):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(person_id=person_id)
    await state.set_state(EditPerson.name)
    await call.message.edit_text(f"Напиши новое имя для записи «{person['name']}»:")
    await call.answer()


@dp.message(EditPerson.name)
async def edit_name_message(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не должно быть пустым. Попробуй ещё раз:")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE people SET name=? WHERE id=? AND user_id=?",
            (name, data["person_id"], message.from_user.id)
        )
        await db.commit()
    await state.clear()
    await message.answer("✅ Имя изменено.", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("edittime:"))
async def edit_time_cb(call: CallbackQuery, state: FSMContext):
    person_id = int(call.data.split(":")[1])
    person = await get_person(person_id, call.from_user.id)
    if not person:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    await state.update_data(person_id=person_id)
    await state.set_state(EditPerson.time)
    await call.message.edit_text("Напиши новое время в формате ЧЧ:ММ:")
    await call.answer()


@dp.message(EditPerson.time)
async def edit_time_message(message: Message, state: FSMContext):
    time_value = (message.text or "").strip()
    try:
        datetime.strptime(time_value, "%H:%M")
    except ValueError:
        await message.answer("Неверный формат. Введи, например, 09:15:")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE people SET time=? WHERE id=? AND user_id=?",
            (time_value, data["person_id"], message.from_user.id)
        )
        await db.commit()
    await state.clear()
    await message.answer("✅ Время изменено.", reply_markup=main_menu())


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
        f"Точно удалить «{person['name']}»? Удаление окончательное.",
        reply_markup=markup
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
    buttons = [
        [InlineKeyboardButton(
            text=f"⬜ {r['name']} · {r['time']}",
            callback_data=f"pickdelete:{r['id']}"
        )] for r in rows
    ]
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

    buttons = [
        [InlineKeyboardButton(
            text=f"{'☑️' if r['id'] in ids else '⬜'} {r['name']} · {r['time']}",
            callback_data=f"pickdelete:{r['id']}"
        )] for r in rows
    ]
    buttons.append([InlineKeyboardButton(text=f"🗑 Удалить выбранных ({len(ids)})", callback_data="delete:confirm")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="menu")])
    await call.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
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
