import asyncio
import base64
import io
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from aiogram import Bot, Dispatcher, F, Router, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from PIL import Image
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("reminder_bot")

TOKEN = "8406940174:AAGDxk-G6NQg_BRCYiErIbUeFqraUIr1eVQ"
API_KEY = (os.getenv("OPENAI_API_KEY") or "sk-fc-brUkPKHb4iXRDA7N4mrkVtD1I-DpCQt6-lAJkGGjflA").strip()
BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://freecodex.fun/v1").rstrip("/")

DB_PATH = Path("/app/data/reminders.db") if Path("/app").is_dir() else Path("reminders.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(90.0, connect=15.0),
    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json", "User-Agent": "OpenAI/Python 1.54.0"},
)

PREDEFINED_ROLES = {
    "pasha": {"name": "Паша", "label": "Паша (Сын)", "emoji": "🙋‍♂️", "roles": "паша, сын, брат, парень"},
    "denis": {"name": "Денис", "label": "Денис (Папа)", "emoji": "👨", "roles": "денис, папа, отец, батя"},
    "yulya": {"name": "Юля", "label": "Юля (Мама)", "emoji": "👩", "roles": "юля, мама, мать, мамуля"},
    "vera": {"name": "Вера", "label": "Вера (Дочь / Сестра)", "emoji": "👧", "roles": "вера, дочь, сестра, мелкая, малая"},
    "verochka": {"name": "Верочка", "label": "Верочка (Девушка)", "emoji": "❤️", "roles": "верочка, девушка, веста, любимая, зайка"},
}

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

def get_now_minsk() -> datetime: 
    return datetime.utcnow() + timedelta(hours=3)

def format_human_dt(dt_str: str) -> str:
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        now = get_now_minsk()
        days_diff = (dt.date() - now.date()).days
        if days_diff == 0: prefix = "Сегодня"
        elif days_diff == 1: prefix = "Завтра"
        elif days_diff == 2: prefix = "Послезавтра"
        else: prefix = f"{WEEKDAYS_RU[dt.weekday()]}, {dt.strftime('%d.%m.%Y')}"
        return f"{prefix} в {dt.strftime('%H:%M')} ({dt.strftime('%d.%m.%Y')})"
    except: 
        return dt_str

def format_delta_hint(dt_str: str) -> str:
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        diff = dt - get_now_minsk()
        tot_m = max(1, int(diff.total_seconds() // 60))
        if tot_m < 60: return f"через {tot_m} мин."
        h, m = tot_m // 60, tot_m % 60
        if h < 24: return f"через {h} ч. {m} мин." if m else f"через {h} ч."
        return f"через {h // 24} дн. {h % 24} ч." if h % 24 else f"через {h // 24} дн."
    except: 
        return ""

class ParsedReminderItem(BaseModel):
    target_role_key: str = Field(default="self")
    scheduled_at: str = Field(description="YYYY-MM-DD HH:MM:SS")
    task_text: str = Field()

class ParsedReminderBatch(BaseModel):
    reminders: List[ParsedReminderItem] = Field(default_factory=list)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (tg_id INTEGER PRIMARY KEY, role_key TEXT DEFAULT 'pasha', display_name TEXT, roles TEXT, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER, target_id INTEGER, target_role_key TEXT DEFAULT 'self', original_text TEXT, parsed_text TEXT, scheduled_at TEXT, status TEXT, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS shopping_list (id INTEGER PRIMARY KEY AUTOINCREMENT, item_text TEXT, added_by TEXT, is_bought INTEGER DEFAULT 0, created_at TEXT)")
        for q in ["ALTER TABLE users ADD COLUMN role_key TEXT DEFAULT 'pasha'", "ALTER TABLE reminders ADD COLUMN target_role_key TEXT DEFAULT 'self'"]:
            try: c.execute(q)
            except: pass
        c.execute("UPDATE users SET role_key='pasha', display_name='Паша', roles=? WHERE LOWER(display_name) IN ('паша','pasha','павел')", (PREDEFINED_ROLES["pasha"]["roles"],))
        conn.commit()

init_db()

def get_user_list():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]

def get_user(tg_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        return dict(r) if r else None

def get_user_by_role(role_key: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM users WHERE role_key = ? ORDER BY created_at DESC LIMIT 1", (role_key,)).fetchone()
        return dict(r) if r else None

def get_main_reply_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Мои напоминания"), KeyboardButton(text="🛒 Список покупок")], 
            [KeyboardButton(text="👨‍👩‍👧‍👦 Семья и роли"), KeyboardButton(text="💡 Примеры и помощь")]
        ], 
        resize_keyboard=True,
        input_field_placeholder="Напиши напоминание..."
    )

def get_roles_inline_kb():
    ubr = {u.get("role_key"): u for u in get_user_list()}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{i['emoji']} {i['label']}{' ✅' if k in ubr else ''}", callback_data=f"reg:{k}")] 
        for k, i in PREDEFINED_ROLES.items()
    ])

def get_sent_kb(rem_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"rem_done:{rem_id}"), InlineKeyboardButton(text="⏳ +15 мин", callback_data=f"rem_delay:{rem_id}:15")], 
        [InlineKeyboardButton(text="⏳ +1 час", callback_data=f"rem_delay:{rem_id}:60"), InlineKeyboardButton(text="🌅 На завтра", callback_data=f"rem_delay:{rem_id}:1440")]
    ])

async def parse_with_ai(user: dict, contents: Union[str, list], fallback: str = "") -> Optional[ParsedReminderBatch]:
    now = get_now_minsk()
    roles_desc = "\n".join(f"- '{k}': {v['label']} ({v['roles']})" for k, v in PREDEFINED_ROLES.items())
    sys_prompt = f"Ты семейный ИИ-планировщик. Время (Минск): {now.strftime('%Y-%m-%d %H:%M:%S')}. Сегодня: {now.strftime('%Y-%m-%d')}, Завтра: {(now+timedelta(days=1)).strftime('%Y-%m-%d')}. Отправитель: {user.get('display_name')} (role_key='{user.get('role_key', 'pasha')}').\nДоступные роли:\n{roles_desc}\n- 'self': себе\n- 'all': всем\nПРАВИЛА:\n1. Вычисли scheduled_at (YYYY-MM-DD HH:MM:SS).\n2. 'завтра' без времени = 09:00:00.\n3. Исправь опечатки (стомотолог->Стоматолог). Ответь ТОЛЬКО JSON: {{\"reminders\": [{{\"target_role_key\": \"self\", \"scheduled_at\": \"YYYY-MM-DD HH:MM:SS\", \"task_text\": \"Текст\"}}]}}"
    
    for model in ["gemini-3.8-flash", "gemini-3-flash", "gemini-3.1-pro"]:
        try:
            resp = await http_client.post(f"{BASE_URL}/chat/completions", json={"model": model, "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": contents}], "temperature": 0.1})
            if resp.status_code >= 400:
                await asyncio.sleep(1)
                continue
            txt = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(txt, list): txt = "\n".join(p.get("text", "") for p in txt if isinstance(p, dict))
            cln = re.sub(r"^```(?:json)?\s*", "", txt.strip(), flags=re.IGNORECASE)
            cln = re.sub(r"\s*```$", "", cln)
            s, e = cln.find("{"), cln.rfind("}")
            if s != -1 and e != -1:
                b = ParsedReminderBatch.model_validate_json(cln[s:e+1])
                if b.reminders: return b
        except: 
            await asyncio.sleep(1)
    
    if fallback:
        try:
            target = "self"
            for k, p in [("all", r"всем"), ("denis", r"пап|отец|бат"), ("yulya", r"мам|юл"), ("verochka", r"девушк|зайк|вест|вероч"), ("vera", r"вер|сестр|доч|мелк"), ("pasha", r"паш|сын|брат")]:
                if re.search(p, fallback.lower()): target = k; break
            
            dt = now + timedelta(hours=1)
            if "завтра" in fallback.lower(): dt = (now + timedelta(days=1)).replace(hour=9, minute=0)
            if "послезавтра" in fallback.lower(): dt = (now + timedelta(days=2)).replace(hour=9, minute=0)
            
            m = re.search(r"через (\d+) (мин|час)", fallback.lower())
            if m: dt = now + (timedelta(minutes=int(m.group(1))) if "мин" in m.group(2) else timedelta(hours=int(m.group(1))))
            
            return ParsedReminderBatch(reminders=[ParsedReminderItem(target_role_key=target, scheduled_at=dt.strftime("%Y-%m-%d %H:%M:%S"), task_text=fallback.capitalize())])
        except: pass
    return None

def resolve_target(user: dict, target_key: str):
    sid, srole = user["tg_id"], user.get("role_key", "pasha")
    if target_key in ("self", "", srole): return sid, srole, f"{PREDEFINED_ROLES.get(srole, PREDEFINED_ROLES['pasha'])['emoji']} Лично", True
    if target_key == "all": return -1, "all", "👨‍👩‍👧‍👦 Всей семье", True
    if target_key in PREDEFINED_ROLES:
        tu = get_user_by_role(target_key)
        lbl = f"{PREDEFINED_ROLES[target_key]['emoji']} {PREDEFINED_ROLES[target_key]['label']}"
        return (tu["tg_id"], target_key, lbl, True) if tu else (0, target_key, lbl, False)
    return sid, srole, "Лично", True

def get_welcome(user: dict):
    if user:
        r = PREDEFINED_ROLES.get(user.get('role_key','pasha'), PREDEFINED_ROLES['pasha'])
        return f"⏰ <b>Семейный ИИ-Напоминатор</b>\nПрофиль: {r['emoji']} <b>{r['label']}</b>\n\nПиши или диктуй задачи как есть:\n• <code>Стоматолог в 10 утра послезавтра</code>\n• <code>Напомни папе сегодня в 19:00 забрать ключи</code>"
    return "👋 <b>Привет! Это наш семейный Напоминатор.</b>\n👇 Нажми на кнопку со своим именем:"

def get_shop_view():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        items = conn.execute("SELECT * FROM shopping_list ORDER BY is_bought ASC, id ASC").fetchall()
    if not items: return "🛒 <b>Список покупок пуст.</b>\nНапиши: <code>Купи молоко и хлеб</code>", InlineKeyboardMarkup(inline_keyboard=[])
    kb = [[InlineKeyboardButton(text=f"{'✅' if i['is_bought'] else '⬜️'} {i['item_text']}", callback_data=f"shop_tg:{i['id']}")] for i in items] + [[InlineKeyboardButton(text="🧹 Очистить купленное", callback_data="shop_clr")]]
    return "🛒 <b>Список покупок:</b>\n" + "\n".join(f"{'✅' if i['is_bought'] else '⬜️'} {'<s>' if i['is_bought'] else '<b>'}{html.quote(i['item_text'])}{'</s>' if i['is_bought'] else '</b>'} <i>({i['added_by']})</i>" for i in items), InlineKeyboardMarkup(inline_keyboard=kb)


# ==============================================================================
# 1. ОБРАБОТЧИКИ МЕНЮ (СТРОГО В НАЧАЛЕ ФАЙЛА)
# ==============================================================================
@router.message(Command("start"))
async def cmd_start(msg: Message):
    u = get_user(msg.from_user.id)
    await msg.answer(get_welcome(u), reply_markup=get_main_reply_kb() if u else get_roles_inline_kb())

@router.message(Command("roles"))
@router.message(F.text == "👨‍👩‍👧‍👦 Семья и роли")
async def cmd_roles(msg: Message):
    await msg.answer("👨‍👩‍👧‍👦 <b>Состав семьи в боте:</b>\n" + "\n".join(f"{i['emoji']} <b>{i['label']}</b> — {'🟢 Подключен(а)' if k in {x.get('role_key') for x in get_user_list()} else '⚪️ Еще не заходил(а)'}" for k, i in PREDEFINED_ROLES.items()) + "\n\n👇 <i>Выбрать свою роль:</i>", reply_markup=get_roles_inline_kb())

@router.message(F.text == "🛒 Список покупок")
async def cmd_shop(msg: Message):
    t, k = get_shop_view()
    await msg.answer(t, reply_markup=k)

@router.message(F.text == "📋 Мои напоминания")
async def cmd_list(msg: Message):
    u = get_user(msg.from_user.id)
    if not u: return
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM reminders WHERE status='PENDING' AND (target_id=? OR creator_id=? OR target_role_key=? OR target_id=-1) ORDER BY scheduled_at ASC LIMIT 15", (u["tg_id"], u["tg_id"], u["role_key"])).fetchall()
    if not r: return await msg.answer("📭 Активных напоминаний нет.")
    await msg.answer("📋 <b>Активные:</b>\n\n" + "\n\n".join(f"<b>{idx}. {html.quote(x['parsed_text'])}</b>\n🕒 {format_human_dt(x['scheduled_at'])}" for idx, x in enumerate(r, 1)), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🗑 Удалить #{idx}", callback_data=f"rem_cancel:{x['id']}")] for idx, x in enumerate(r, 1)]))

@router.message(F.text == "💡 Примеры и помощь")
async def cmd_help(msg: Message):
    u = get_user(msg.from_user.id)
    await msg.answer(get_welcome(u), reply_markup=get_main_reply_kb() if u else get_roles_inline_kb())


# ==============================================================================
# 2. CALLBACK-КНОПКИ
# ==============================================================================
@router.callback_query(F.data.startswith("reg:"))
async def process_reg(call: CallbackQuery):
    k = call.data.split(":")[1]
    if k in PREDEFINED_ROLES:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO users (tg_id, role_key, display_name, roles, created_at) VALUES (?, ?, ?, ?, ?)", (call.from_user.id, k, PREDEFINED_ROLES[k]["name"], PREDEFINED_ROLES[k]["roles"], get_now_minsk().strftime("%Y-%m-%d %H:%M:%S")))
        await call.message.edit_text(f"✅ <b>Профиль сохранен!</b>\n\n{get_welcome(get_user(call.from_user.id))}")
        await call.message.answer("Нижнее меню активно 👇", reply_markup=get_main_reply_kb())
    await call.answer()

@router.callback_query(F.data.startswith("shop_tg:"))
async def cb_shop_tg(call: CallbackQuery):
    with sqlite3.connect(DB_PATH) as c: c.execute("UPDATE shopping_list SET is_bought = 1 - is_bought WHERE id = ?", (int(call.data.split(":")[1]),))
    t, k = get_shop_view()
    await call.message.edit_text(t, reply_markup=k)
    await call.answer()

@router.callback_query(F.data == "shop_clr")
async def cb_shop_clr(call: CallbackQuery):
    with sqlite3.connect(DB_PATH) as c: c.execute("DELETE FROM shopping_list WHERE is_bought = 1")
    t, k = get_shop_view()
    await call.message.edit_text(t, reply_markup=k)
    await call.answer()

@router.callback_query(F.data.startswith("rem_cancel:"))
async def cb_canc(call: CallbackQuery):
    with sqlite3.connect(DB_PATH) as c: c.execute("DELETE FROM reminders WHERE id = ?", (int(call.data.split(":")[1]),))
    await call.message.edit_text("🗑 <b>Отменено</b>")

@router.callback_query(F.data.startswith("rem_done:"))
async def cb_done(call: CallbackQuery):
    rid = int(call.data.split(":")[1])
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        if r:
            c.execute("UPDATE reminders SET status='COMPLETED' WHERE id = ?", (rid,))
            if r["creator_id"] != call.from_user.id:
                try: await bot.send_message(r["creator_id"], f"👍 <b>{get_user(call.from_user.id)['display_name']}</b> выполнил(а):\n✔️ <s>{html.quote(r['parsed_text'])}</s>")
                except: pass
            await call.message.edit_text(f"✅ <b>Выполнено:</b>\n<s>{html.quote(r['parsed_text'])}</s>")

@router.callback_query(F.data.startswith("rem_delay:"))
async def cb_delay(call: CallbackQuery):
    rid, m = int(call.data.split(":")[1]), int(call.data.split(":")[2])
    nt = (get_now_minsk() + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        if r:
            c.execute("UPDATE reminders SET scheduled_at=?, status='PENDING' WHERE id=?", (nt, rid))
            await call.message.edit_text(f"⏳ <b>Отложено на {format_human_dt(nt)}:</b>\n📌 {html.quote(r['parsed_text'])}")


# ==============================================================================
# 3. ДИНАМИЧЕСКОЕ ИИ-РАСПОЗНАВАНИЕ (СВОБОДНЫЙ ВВОД - В КОНЦЕ!)
# ==============================================================================
async def handle_dynamic(msg: Message, content: Union[str, list], raw: str):
    u = get_user(msg.from_user.id)
    if not u: return await cmd_start(msg)
    
    if len(raw) < 2: return
    
    if isinstance(content, str) and re.match(r"^(в покупки|купи(?:ть)?)\b", content, re.I):
        items = [x.strip().capitalize() for x in re.split(r"[,;\n]+", re.sub(r"^(в покупки|купи(?:ть)?)\s*[:,-]?\s*", "", content, flags=re.I)) if x.strip()]
        if items:
            with sqlite3.connect(DB_PATH) as c:
                for i in items: c.execute("INSERT INTO shopping_list (item_text, added_by, created_at) VALUES (?, ?, ?)", (i, u["display_name"], get_now_minsk().strftime("%Y-%m-%d %H:%M:%S")))
            t, k = get_shop_view()
            return await msg.answer(t, reply_markup=k)

    wm = await msg.answer("⏳ <i>Анализирую...</i>")
    b = await parse_with_ai(u, content, raw)
    try: await wm.delete()
    except: pass

    if not b or not b.reminders: return await msg.answer("🤔 Не понял время/дату. Напиши конкретнее.")
    for r in b.reminders:
        tid, rk, lbl, ok = resolve_target(u, r.target_role_key)
        with sqlite3.connect(DB_PATH) as c:
            cur = c.execute("INSERT INTO reminders (creator_id, target_id, target_role_key, original_text, parsed_text, scheduled_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)", (msg.from_user.id, tid, rk, raw, r.task_text, r.scheduled_at, get_now_minsk().strftime("%Y-%m-%d %H:%M:%S")))
            rid = cur.lastrowid
        
        await msg.answer(f"✅ <b>Записано!</b>\n📌 {html.quote(r.task_text)}\n👤 Кому: {lbl}\n🕒 Когда: {format_human_dt(r.scheduled_at)} <i>({format_delta_hint(r.scheduled_at)})</i>" + ("\n⚠️ <i>Пользователь еще не нажал /start. Напоминание привяжется, когда он войдет!</i>" if not ok and rk != "all" else ""), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Отменить", callback_data=f"rem_cancel:{rid}")]]))
        
        if tid not in (0, msg.from_user.id):
            nt = f"📨 <b>От {PREDEFINED_ROLES.get(u.get('role_key','pasha'), PREDEFINED_ROLES['pasha'])['label']}:</b>\n📌 {html.quote(r.task_text)}\n🕒 {format_human_dt(r.scheduled_at)}"
            if tid == -1:
                for x in get_user_list():
                    if x["tg_id"] != msg.from_user.id:
                        try: await bot.send_message(x["tg_id"], nt)
                        except: pass
            else:
                try: await bot.send_message(tid, nt)
                except: pass

@router.message(F.text)
async def h_txt(m: Message):
    await handle_dynamic(m, m.text, m.text)

@router.message(F.voice)
async def h_voc(m: Message):
    b = io.BytesIO()
    await bot.download_file((await bot.get_file(m.voice.file_id)).file_path, b)
    await handle_dynamic(m, [{"type": "text", "text": "Извлеки напоминания"}, {"type": "image_url", "image_url": {"url": f"data:audio/ogg;base64,{base64.b64encode(b.getvalue()).decode('ascii')}"}}], "[Голосовое]")

@router.message(F.photo)
async def h_pic(m: Message):
    b = io.BytesIO()
    await bot.download_file((await bot.get_file(m.photo[-1].file_id)).file_path, b)
    await handle_dynamic(m, [{"type": "text", "text": f"Извлеки задачу/дату. Коммент: {m.caption or ''}"}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(b.getvalue()).decode('ascii')}"}}], m.caption or "[Фото]")


# ==============================================================================
# 4. ФОНОВАЯ ЗАДАЧА 
# ==============================================================================
async def task_loop():
    while True:
        try:
            ns = get_now_minsk().strftime("%Y-%m-%d %H:%M:%S")
            with sqlite3.connect(DB_PATH) as c:
                c.row_factory = sqlite3.Row
                pend = c.execute("SELECT * FROM reminders WHERE status='PENDING' AND scheduled_at<=?", (ns,)).fetchall()
                for r in pend:
                    c.execute("UPDATE reminders SET status='SENT' WHERE id=?", (r["id"],))
                    tid = r["target_id"]
                    if tid == 0: tid = (get_user_by_role(r["target_role_key"]) or {"tg_id": r["creator_id"]})["tg_id"]
                    cr = get_user(r["creator_id"])
                    txt = f"🔔 <b>НАПОМИНАНИЕ!</b>\n📌 <b>{html.quote(r['parsed_text'])}</b>" + (f"\n👤 <i>От: {cr['display_name']}</i>" if cr and r["creator_id"] != tid and tid != -1 else "")
                    kb = get_sent_kb(r["id"])
                    if tid == -1:
                        for u in get_user_list():
                            try: await bot.send_message(u["tg_id"], txt, reply_markup=kb)
                            except: pass
                    else:
                        try: await bot.send_message(tid, txt, reply_markup=kb)
                        except: pass
        except: pass
        await asyncio.sleep(10)

async def main():
    asyncio.create_task(task_loop())
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())