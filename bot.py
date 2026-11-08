import os
import re
import asyncio
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных Bothost")

# Используем уже существующую БД, если она найдена. Ничего не удаляем.
candidates = [
    os.getenv("DB_PATH", "").strip(),
    "/app/data/payroll.db",
    "/app/payroll.db",
    "/app/data/people.db",
    "payroll.db",
]
DB = next((p for p in candidates if p and Path(p).is_file()), None)
if DB is None:
    DB = os.getenv("DB_PATH", "/app/data/payroll.db")
Path(DB).parent.mkdir(parents=True, exist_ok=True)

dp = Dispatcher()
db = None

HELP = (
    "Команды:\n"
    "/list — список работников\n"
    "/pending — кто ещё не ответил\n"
    "/invite — создать код для второго/третьего пользователя\n"
    "/edit ID | ФИО | начало | конец — изменить запись\n"
    "/delete ID — удалить запись\n"
    "/help — помощь\n\n"
    "Добавляй работников обычным сообщением — по одному или списком:\n"
    "Кириллов Денис 7:00\n"
    "Григорьев Иван 7:00\n"
    "Аллагуров Сергей 8:00\n\n"
    "Время окончания можно не указывать. Нажми «Закончил работу», когда смена завершится."
)

async def init_db():
    global db
    db = await aiosqlite.connect(DB)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_users(
            user_id INTEGER PRIMARY KEY,
            joined_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS invite_codes(
            code TEXT PRIMARY KEY,
            created_by INTEGER NOT NULL,
            used_by INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS work_entries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            replied INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    await db.commit()

async def allowed(user_id: int) -> bool:
    cur = await db.execute("SELECT 1 FROM bot_users WHERE user_id=?", (user_id,))
    return await cur.fetchone() is not None

async def user_count() -> int:
    cur = await db.execute("SELECT COUNT(*) FROM bot_users")
    return (await cur.fetchone())[0]

async def require_access(message: Message) -> bool:
    if await allowed(message.from_user.id):
        return True
    await message.answer("У тебя пока нет доступа. Попроси одного из участников прислать код приглашения и отправь его боту.")
    return False

def parse_time(value):
    value = value.strip().replace(".", ":")
    m = re.fullmatch(r"([0-2]?\d):([0-5]\d)", value)
    if not m:
        return None
    h, minute = int(m.group(1)), int(m.group(2))
    if h > 23:
        return None
    return f"{h:02d}:{minute:02d}"

def parse_line(line):
    line = line.strip()
    if not line:
        return None
    m = re.match(r"^(.+?)\s+([0-2]?\d:[0-5]\d)$", line)
    if not m:
        return None
    name = " ".join(m.group(1).split())
    start = parse_time(m.group(2))
    if not name or not start:
        return None
    return name, start

def duration(start, end):
    fmt = "%H:%M"
    a = datetime.strptime(start, fmt)
    b = datetime.strptime(end, fmt)
    if b < a:
        b += timedelta(days=1)
    mins = int((b-a).total_seconds() // 60)
    hours, rem = divmod(mins, 60)
    return f"{hours} ч {rem:02d} мин" if rem else f"{hours} ч"

def entry_keyboard(entry_id, replied, ended):
    kb = InlineKeyboardBuilder()
    if not replied:
        kb.button(text="✅ Ответил", callback_data=f"reply:{entry_id}")
    if not ended:
        kb.button(text="🏁 Закончил работу", callback_data=f"end:{entry_id}")
    kb.adjust(1)
    return kb.as_markup() if kb.buttons else None

async def render_entry(row):
    entry_id, name, start, end, replied = row
    status = "✅ Ответил" if replied else "⏳ Не ответил"
    if end:
        worked = duration(start, end)
        status += f"\n🏁 {start}–{end} / {worked}"
    else:
        status += f"\n🕒 Начало: {start}"
    return f"#{entry_id} — {name}\n{status}"

@dp.message(Command("start"))
async def start_cmd(message: Message):
    uid = message.from_user.id
    if await allowed(uid):
        await message.answer("Бот готов к работе.\n\n" + HELP)
        return
    count = await user_count()
    if count == 0:
        await db.execute("INSERT INTO bot_users(user_id,joined_at) VALUES(?,?)",
                         (uid, datetime.now().isoformat(timespec="seconds")))
        await db.commit()
        await message.answer("Ты первый участник и администратор доступа. Теперь можно пригласить ещё двух человек командой /invite.\n\n" + HELP)
        return
    await message.answer("Отправь код приглашения, который создал один из участников командой /invite.")

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if await require_access(message):
        await message.answer(HELP)

@dp.message(Command("invite"))
async def invite_cmd(message: Message):
    if not await require_access(message):
        return
    if await user_count() >= 3:
        await message.answer("Все 3 места уже заняты.")
        return
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    await db.execute("INSERT INTO invite_codes(code,created_by,created_at) VALUES(?,?,?)",
                     (code, message.from_user.id, datetime.now().isoformat(timespec="seconds")))
    await db.commit()
    await message.answer(f"Код приглашения: {code}\nПришли его второму или третьему участнику. Код одноразовый.")

@dp.message(F.text)
async def text_handler(message: Message):
    text = (message.text or "").strip()
    if not text:
        return
    if not await allowed(message.from_user.id):
        code = text.upper()
        cur = await db.execute("SELECT code FROM invite_codes WHERE code=? AND used_by IS NULL", (code,))
        found = await cur.fetchone()
        if found and await user_count() < 3:
            await db.execute("INSERT OR IGNORE INTO bot_users(user_id,joined_at) VALUES(?,?)",
                             (message.from_user.id, datetime.now().isoformat(timespec="seconds")))
            await db.execute("UPDATE invite_codes SET used_by=? WHERE code=?",
                             (message.from_user.id, code))
            await db.commit()
            await message.answer("Доступ открыт! Теперь вы видите общий список и можете им управлять.\n\n" + HELP)
        else:
            await message.answer("Код не найден, уже использован или все 3 места заняты.")
        return

    lines = text.splitlines()
    parsed, bad = [], []
    for line in lines:
        item = parse_line(line)
        if item:
            parsed.append(item)
        elif line.strip():
            bad.append(line.strip())

    if not parsed:
        if text.startswith("/"):
            return
        await message.answer("Не понял формат. Используй: Кириллов Денис 7:00\nМожно отправить сразу несколько строк.")
        return

    now = datetime.now().isoformat(timespec="seconds")
    for name, start in parsed:
        await db.execute(
            "INSERT INTO work_entries(full_name,start_time,created_by,created_at) VALUES(?,?,?,?)",
            (name, start, message.from_user.id, now)
        )
    await db.commit()
    answer = f"Добавлено работников: {len(parsed)}."
    if bad:
        answer += "\nНе удалось распознать строки:\n" + "\n".join(bad)
    await message.answer(answer)
    cur = await db.execute("SELECT id,full_name,start_time,end_time,replied FROM work_entries ORDER BY id DESC LIMIT ?",
                           (len(parsed),))
    rows = await cur.fetchall()
    for row in reversed(rows):
        await message.answer(await render_entry(row), reply_markup=entry_keyboard(row[0], row[4], row[3] is not None))

@dp.message(Command("list"))
async def list_cmd(message: Message):
    if not await require_access(message):
        return
    cur = await db.execute("SELECT id,full_name,start_time,end_time,replied FROM work_entries ORDER BY id")
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Список пока пуст.")
        return
    for row in rows:
        await message.answer(await render_entry(row), reply_markup=entry_keyboard(row[0], row[4], row[3] is not None))

@dp.message(Command("pending"))
async def pending_cmd(message: Message):
    if not await require_access(message):
        return
    cur = await db.execute("SELECT id,full_name,start_time,end_time,replied FROM work_entries WHERE replied=0 ORDER BY id")
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Все ответили ✅")
        return
    for row in rows:
        await message.answer(await render_entry(row), reply_markup=entry_keyboard(row[0], row[4], row[3] is not None))

@dp.callback_query(F.data.startswith("reply:"))
async def reply_cb(call: CallbackQuery):
    if not await allowed(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    entry_id = int(call.data.split(":")[1])
    await db.execute("UPDATE work_entries SET replied=1 WHERE id=?", (entry_id,))
    await db.commit()
    await call.answer("Отмечено")
    cur = await db.execute("SELECT id,full_name,start_time,end_time,replied FROM work_entries WHERE id=?", (entry_id,))
    row = await cur.fetchone()
    if row:
        await call.message.edit_text(await render_entry(row), reply_markup=entry_keyboard(row[0], row[4], row[3] is not None))

@dp.callback_query(F.data.startswith("end:"))
async def end_cb(call: CallbackQuery):
    if not await allowed(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    entry_id = int(call.data.split(":")[1])
    end = datetime.now().strftime("%H:%M")
    await db.execute("UPDATE work_entries SET end_time=? WHERE id=? AND end_time IS NULL", (end, entry_id))
    await db.commit()
    cur = await db.execute("SELECT id,full_name,start_time,end_time,replied FROM work_entries WHERE id=?", (entry_id,))
    row = await cur.fetchone()
    await call.answer("Время окончания сохранено")
    if row:
        await call.message.edit_text(await render_entry(row), reply_markup=entry_keyboard(row[0], row[4], row[3] is not None))

@dp.message(Command("edit"))
async def edit_cmd(message: Message):
    if not await require_access(message):
        return
    raw = message.text.partition(" ")[2].strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 4 or not parts[0].isdigit():
        await message.answer("Формат:\n/edit ID | ФИО | начало | конец\nПример: /edit 12 | Кириллов Денис | 7:00 | 15:00\nЕсли смена не закончена, вместо конца укажи -")
        return
    entry_id, name, start, end = int(parts[0]), parts[1], parse_time(parts[2]), (None if parts[3] == "-" else parse_time(parts[3]))
    if not name or not start or (parts[3] != "-" and not end):
        await message.answer("Проверь ФИО и время. Формат времени — 7:00.")
        return
    await db.execute("UPDATE work_entries SET full_name=?,start_time=?,end_time=? WHERE id=?",
                     (name, start, end, entry_id))
    await db.commit()
    await message.answer("Запись обновлена. Посмотреть список: /list")

@dp.message(Command("delete"))
async def delete_cmd(message: Message):
    if not await require_access(message):
        return
    raw = message.text.partition(" ")[2].strip()
    ids = [int(x) for x in re.findall(r"\d+", raw)]
    if not ids:
        await message.answer("Укажи ID: /delete 12\nНесколько: /delete 12 13 14")
        return
    await db.executemany("DELETE FROM work_entries WHERE id=?", [(x,) for x in ids])
    await db.commit()
    await message.answer("Удаление выполнено.")

async def main():
    await init_db()
    print(f"Database: {DB}")
    bot = Bot(token=TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
