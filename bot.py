"""
EduProject Bot - maktab loyihalariga ro'yxatdan o'tkazish boti.

- Har bir loyiha uchun jami TOTAL_SEATS (60) ta joy.
- Admin loyiha yaratayotganda sinflarni belgilaydi, joylar sinflarga avtomatik taqsimlanadi
  (xohlasa qo'lda o'zgartiradi).
- Sinf uchun joy tugasa, o'quvchi ro'yxatdan o'ta olmaydi.
"""
import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
from datetime import datetime
from html import escape as esc

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------- SOZLAMALAR
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
TOTAL_SEATS = int(os.getenv("TOTAL_SEATS", "60"))  # bitta loyiha uchun jami joy
CLASSES = [5, 6, 7, 8, 9, 10]  # kerak bo'lsa 11 ni ham qo'shing
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)

# ----------------------------------------------------------------- BAZA
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, date TEXT, time TEXT, place TEXT,
    total INTEGER, status TEXT DEFAULT 'active', created_at TEXT
);
CREATE TABLE IF NOT EXISTS project_classes (
    project_id INTEGER, class INTEGER, seats INTEGER,
    PRIMARY KEY (project_id, class)
);
CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER, tg_id INTEGER, username TEXT,
    first_name TEXT, last_name TEXT, class INTEGER, created_at TEXT,
    UNIQUE (project_id, tg_id)
);
"""
)
conn.commit()


def q(sql, args=()):
    return conn.execute(sql, args).fetchall()


def q1(sql, args=()):
    return conn.execute(sql, args).fetchone()


def run(sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def class_stats(pid):
    return q(
        """SELECT pc.class AS cls, pc.seats AS seats,
           (SELECT COUNT(*) FROM registrations r
             WHERE r.project_id = pc.project_id AND r.class = pc.class) AS taken
           FROM project_classes pc WHERE pc.project_id = ? ORDER BY pc.class""",
        (pid,),
    )


def seats_left(pid):
    p = q1("SELECT total FROM projects WHERE id=?", (pid,))
    taken = q1("SELECT COUNT(*) n FROM registrations WHERE project_id=?", (pid,))["n"]
    return p["total"] - taken


def split_seats(classes, total):
    """Joylarni tanlangan sinflarga teng bo'lib chiqadi (qoldiq yuqori sinflarga)."""
    classes = sorted(classes)
    base, rem = divmod(total, len(classes))
    res = {c: base for c in classes}
    for c in classes[::-1][:rem]:
        res[c] += 1
    return res


def parse_limits(text, allowed, total):
    """'5=5, 6=11, 7=11' ko'rinishidagi matnni tekshiradi."""
    pairs = re.findall(r"(\d+)\s*[=:]\s*(\d+)", text)
    res = {int(a): int(b) for a, b in pairs}
    if set(res) != set(allowed):
        need = ", ".join(f"{c}=?" for c in sorted(allowed))
        return None, f"Aynan shu sinflarni yozing: {need}"
    if sum(res.values()) != total:
        return None, f"Yig'indi {total} bo'lishi kerak, sizda {sum(res.values())} bo'ldi. Qaytadan yuboring."
    return res, None


# ----------------------------------------------------------------- MATNLAR
def project_text(p, with_seats=True):
    t = (
        f"📌 <b>{esc(p['name'])}</b>\n"
        f"📅 Sana: {esc(p['date'])}\n"
        f"🕒 Vaqt: {esc(p['time'])}\n"
        f"📍 Manzil: {esc(p['place'])}"
    )
    if with_seats:
        st = class_stats(p["id"])
        taken = sum(r["taken"] for r in st)
        t += f"\n\n👥 Band: {taken}/{p['total']}\n"
        t += "\n".join(f"• {r['cls']}-sinf: {r['taken']}/{r['seats']}" for r in st)
    return t


def dist_text(dist):
    lines = "\n".join(f"• {c}-sinf: {n} ta joy" for c, n in sorted(dist.items()))
    return f"{lines}\n<b>Jami: {sum(dist.values())} ta</b>"


WELCOME = (
    "Assalomu alaykum! 👋\n\n"
    "Maktabimizning loyihalar platformasiga xush kelibsiz!\n"
    "Bu bot orqali loyihalarda qatnashish uchun ro'yxatdan o'tishingiz mumkin.\n\n"
    "<b>Kerakli bo'limni tanlang:</b>"
)


# ----------------------------------------------------------------- KLAVIATURALAR
def main_menu(uid):
    b = InlineKeyboardBuilder()
    b.button(text="📝 Loyihaga ro'yxatdan o'tish", callback_data="menu:reg")
    b.button(text="📅 Loyihalar va tadbirlar", callback_data="menu:projects")
    b.button(text="👤 Mening ro'yxatdan o'tishlarim", callback_data="menu:my")
    b.button(text="ℹ️ Biz haqimizda", callback_data="menu:about")
    if uid in ADMIN_IDS:
        b.button(text="🛡 Admin panel", callback_data="adm:home")
    b.adjust(1)
    return b.as_markup()


def home_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Asosiy menyu", callback_data="menu:home")
    return b.as_markup()


def admin_home_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Yangi loyiha qo'shish", callback_data="adm:new")
    b.button(text="📋 Loyihalar", callback_data="adm:list")
    b.button(text="🏠 Asosiy menyu", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def project_kb(pid, status="active"):
    b = InlineKeyboardBuilder()
    b.button(text="👥 Qatnashuvchilar", callback_data=f"adm:ppl:{pid}")
    b.button(text="📥 CSV (Excel) yuklash", callback_data=f"adm:csv:{pid}")
    if status == "active":
        b.button(text="⚙️ Sinf limitlarini o'zgartirish", callback_data=f"adm:lim:{pid}")
        b.button(text="📢 Qatnashuvchilarga xabar", callback_data=f"adm:msg:{pid}")
        b.button(text="📣 Hammaga e'lon qilish", callback_data=f"ann:{pid}")
        b.button(text="🔒 Loyihani yakunlash", callback_data=f"adm:close:{pid}")
    b.button(text="⬅️ Loyihalar", callback_data="adm:list")
    b.adjust(1)
    return b.as_markup()


def classes_kb(sel):
    b = InlineKeyboardBuilder()
    for c in CLASSES:
        b.button(text=("✅ " if c in sel else "⬜ ") + f"{c}-sinf", callback_data=f"sel:{c}")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="☑️ Hammasini tanlash", callback_data="sel:all"))
    b.row(InlineKeyboardButton(text="➡️ Davom etish", callback_data="sel:done"))
    return b.as_markup()


async def show(c: CallbackQuery, text, kb=None):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await c.message.answer(text, reply_markup=kb)


def valid_name(t):
    return 2 <= len(t) <= 30 and not any(ch.isdigit() for ch in t)


async def send_many(bot: Bot, ids, text):
    ok = fail = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


# =================================================================== O'QUVCHI
router = Router()


class Reg(StatesGroup):
    first = State()
    last = State()
    cls = State()


def remember_user(uid):
    run("INSERT OR IGNORE INTO users(tg_id, created_at) VALUES(?, ?)", (uid, now()))


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    remember_user(m.from_user.id)
    await m.answer(WELCOME, reply_markup=main_menu(m.from_user.id))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data == "menu:home")
async def menu_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    remember_user(c.from_user.id)
    await show(c, WELCOME, main_menu(c.from_user.id))
    await c.answer()


@router.callback_query(F.data == "menu:about")
async def menu_about(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(
        c,
        "ℹ️ <b>Biz haqimizda</b>\n\nBu bot maktabimizdagi loyiha va tadbirlarga "
        "ro'yxatdan o'tish uchun yaratilgan. Savollar bo'lsa, tashkilotchilarga murojaat qiling.",
        home_kb(),
    )
    await c.answer()


@router.callback_query(F.data == "menu:projects")
async def menu_projects(c: CallbackQuery, state: FSMContext):
    await state.clear()
    ps = q("SELECT * FROM projects WHERE status='active' ORDER BY id DESC")
    if not ps:
        await show(c, "Hozircha faol loyiha yo'q.", home_kb())
    else:
        text = "📅 <b>Loyihalar va tadbirlar</b>\n\n" + "\n\n".join(project_text(p) for p in ps)
        await show(c, text, home_kb())
    await c.answer()


@router.callback_query(F.data == "menu:reg")
async def menu_reg(c: CallbackQuery, state: FSMContext):
    await state.clear()
    ps = q("SELECT * FROM projects WHERE status='active' ORDER BY id DESC")
    if not ps:
        await show(c, "Hozircha ro'yxatdan o'tish uchun faol loyiha yo'q.", home_kb())
        await c.answer()
        return
    b = InlineKeyboardBuilder()
    for p in ps:
        b.button(text=f"{p['name']} · {p['date']}", callback_data=f"reg:{p['id']}")
    b.button(text="🏠 Asosiy menyu", callback_data="menu:home")
    b.adjust(1)
    await show(c, "Qaysi loyihaga ro'yxatdan o'tmoqchisiz?", b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("reg:"))
async def reg_start(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    p = q1("SELECT * FROM projects WHERE id=? AND status='active'", (pid,))
    if not p:
        await c.answer("Bu loyiha yopilgan.", show_alert=True)
        return
    if q1("SELECT 1 FROM registrations WHERE project_id=? AND tg_id=?", (pid, c.from_user.id)):
        await c.answer("Siz bu loyihaga allaqachon ro'yxatdan o'tgansiz.", show_alert=True)
        return
    if seats_left(pid) <= 0:
        await c.answer("Afsuski, bu loyiha uchun barcha joylar to'lgan.", show_alert=True)
        return
    await state.clear()
    await state.set_state(Reg.first)
    await state.update_data(pid=pid)
    await show(c, f"{project_text(p, False)}\n\n✍️ <b>Ismingizni kiriting:</b>\n(Bekor qilish: /cancel)")
    await c.answer()


@router.message(Reg.first, F.text, ~F.text.startswith("/"))
async def reg_first(m: Message, state: FSMContext):
    name = m.text.strip()
    if not valid_name(name):
        await m.answer("Iltimos, ismingizni to'g'ri kiriting (faqat harflar).")
        return
    await state.update_data(first=name)
    await state.set_state(Reg.last)
    await m.answer("✍️ <b>Familiyangizni kiriting:</b>")


@router.message(Reg.last, F.text, ~F.text.startswith("/"))
async def reg_last(m: Message, state: FSMContext):
    last = m.text.strip()
    if not valid_name(last):
        await m.answer("Iltimos, familiyangizni to'g'ri kiriting (faqat harflar).")
        return
    await state.update_data(last=last)
    data = await state.get_data()
    b = InlineKeyboardBuilder()
    for r in class_stats(data["pid"]):
        left = r["seats"] - r["taken"]
        if left > 0:
            b.button(text=f"{r['cls']}-sinf · {left} joy", callback_data=f"cls:{r['cls']}")
        else:
            b.button(text=f"❌ {r['cls']}-sinf · to'lgan", callback_data="cls:full")
    b.adjust(2)
    await state.set_state(Reg.cls)
    await m.answer("🏫 <b>Sinfingizni tanlang:</b>", reply_markup=b.as_markup())


@router.callback_query(Reg.cls, F.data.startswith("cls:"))
async def reg_class(c: CallbackQuery, state: FSMContext):
    val = c.data.split(":")[1]
    if val == "full":
        await c.answer("Bu sinf uchun joylar tugagan.", show_alert=True)
        return
    cls = int(val)
    data = await state.get_data()
    pid = data["pid"]
    p = q1("SELECT * FROM projects WHERE id=? AND status='active'", (pid,))
    if not p:
        await state.clear()
        await c.answer("Bu loyiha yopilgan.", show_alert=True)
        return
    row = next((r for r in class_stats(pid) if r["cls"] == cls), None)
    if row is None:
        await c.answer("Bu sinf loyihada qatnashmaydi.", show_alert=True)
        return
    if row["seats"] - row["taken"] <= 0:
        await c.answer(f"{cls}-sinf uchun joylar tugadi.", show_alert=True)
        return
    try:
        run(
            "INSERT INTO registrations(project_id, tg_id, username, first_name, last_name, class, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (pid, c.from_user.id, c.from_user.username or "", data["first"], data["last"], cls, now()),
        )
    except sqlite3.IntegrityError:
        await state.clear()
        await c.answer("Siz allaqachon ro'yxatdan o'tgansiz.", show_alert=True)
        return
    await state.clear()
    await show(
        c,
        "✅ <b>Muvaffaqiyatli ro'yxatdan o'tdingiz!</b>\n\n"
        f"Ism: {esc(data['first'])}\n"
        f"Familiya: {esc(data['last'])}\n"
        f"Sinf: {cls}-sinf\n\n"
        f"<b>Loyiha haqida:</b>\n{project_text(p, False)}\n\n"
        "Sizni kutib qolamiz! 🎉",
        home_kb(),
    )
    await c.answer()


def my_regs_view(uid):
    rows = q(
        """SELECT r.id rid, r.first_name, r.last_name, r.class, p.name, p.date, p.time, p.place, p.status
           FROM registrations r JOIN projects p ON p.id = r.project_id
           WHERE r.tg_id = ? ORDER BY r.id DESC""",
        (uid,),
    )
    if not rows:
        return "Siz hali hech qaysi loyihaga ro'yxatdan o'tmagansiz.", home_kb()
    b = InlineKeyboardBuilder()
    parts = []
    for r in rows:
        state_txt = "faol" if r["status"] == "active" else "yakunlangan"
        parts.append(
            f"📌 <b>{esc(r['name'])}</b> ({state_txt})\n"
            f"👤 {esc(r['last_name'])} {esc(r['first_name'])}, {r['class']}-sinf\n"
            f"📅 {esc(r['date'])}, {esc(r['time'])}\n📍 {esc(r['place'])}"
        )
        if r["status"] == "active":
            b.button(text=f"❌ Bekor qilish: {r['name']}", callback_data=f"cancel:{r['rid']}")
    b.button(text="🏠 Asosiy menyu", callback_data="menu:home")
    b.adjust(1)
    return "👤 <b>Mening ro'yxatdan o'tishlarim</b>\n\n" + "\n\n".join(parts), b.as_markup()


@router.callback_query(F.data == "menu:my")
async def menu_my(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = my_regs_view(c.from_user.id)
    await show(c, text, kb)
    await c.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_reg(c: CallbackQuery):
    rid = int(c.data.split(":")[1])
    cur = run(
        """DELETE FROM registrations WHERE id = ? AND tg_id = ?
           AND project_id IN (SELECT id FROM projects WHERE status='active')""",
        (rid, c.from_user.id),
    )
    await c.answer("Ro'yxatdan chiqdingiz, joy bo'shatildi." if cur.rowcount else "Bekor qilib bo'lmadi.", show_alert=True)
    text, kb = my_regs_view(c.from_user.id)
    await show(c, text, kb)


# =================================================================== ADMIN
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class NewProject(StatesGroup):
    name = State()
    date = State()
    time = State()
    place = State()
    classes = State()
    confirm = State()
    limits = State()


class EditLimits(StatesGroup):
    limits = State()


class Broadcast(StatesGroup):
    text = State()


@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛡 <b>Admin panel</b>", reply_markup=admin_home_kb())


@admin.callback_query(F.data == "adm:home")
async def admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "🛡 <b>Admin panel</b>", admin_home_kb())
    await c.answer()


# ---- Yangi loyiha
@admin.callback_query(F.data == "adm:new")
async def new_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(NewProject.name)
    await show(c, "➕ <b>Yangi loyiha</b>\n\nLoyiha nomini yozing (masalan: Zakovat):\n(Bekor qilish: /cancel)")
    await c.answer()


@admin.message(NewProject.name, F.text, ~F.text.startswith("/"))
async def new_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(NewProject.date)
    await m.answer("📅 Sanani yozing (masalan: 20-dekabr):")


@admin.message(NewProject.date, F.text, ~F.text.startswith("/"))
async def new_date(m: Message, state: FSMContext):
    await state.update_data(date=m.text.strip())
    await state.set_state(NewProject.time)
    await m.answer("🕒 Vaqtni yozing (masalan: 13:00):")


@admin.message(NewProject.time, F.text, ~F.text.startswith("/"))
async def new_time(m: Message, state: FSMContext):
    await state.update_data(time=m.text.strip())
    await state.set_state(NewProject.place)
    await m.answer("📍 Manzilni yozing (masalan: Maktab kutubxonasi):")


@admin.message(NewProject.place, F.text, ~F.text.startswith("/"))
async def new_place(m: Message, state: FSMContext):
    await state.update_data(place=m.text.strip(), sel=[])
    await state.set_state(NewProject.classes)
    await m.answer(
        f"🏫 <b>Qaysi sinflar qatnashadi?</b>\n\nBelgilang. Jami {TOTAL_SEATS} ta joy "
        "tanlangan sinflarga teng taqsimlanadi (keyin o'zgartirishingiz mumkin).",
        reply_markup=classes_kb([]),
    )


@admin.callback_query(NewProject.classes, F.data.startswith("sel:"))
async def new_select(c: CallbackQuery, state: FSMContext):
    val = c.data.split(":")[1]
    sel = (await state.get_data()).get("sel", [])
    if val == "all":
        sel = list(CLASSES) if len(sel) < len(CLASSES) else []
    elif val == "done":
        if not sel:
            await c.answer("Kamida bitta sinfni belgilang.", show_alert=True)
            return
        dist = split_seats(sel, TOTAL_SEATS)
        await state.update_data(dist={str(k): v for k, v in dist.items()})
        await state.set_state(NewProject.confirm)
        await show(c, preview_text(dist), preview_kb())
        await c.answer()
        return
    else:
        n = int(val)
        sel = [x for x in sel if x != n] if n in sel else sel + [n]
    await state.update_data(sel=sel)
    await show(c, "🏫 <b>Qaysi sinflar qatnashadi?</b>", classes_kb(sel))
    await c.answer()


def preview_text(dist):
    return f"📊 <b>Joylar taqsimoti</b>\n\n{dist_text(dist)}\n\nTasdiqlaysizmi?"


def preview_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tasdiqlash va yaratish", callback_data="np:ok")
    b.button(text="✏️ Limitlarni o'zgartirish", callback_data="np:edit")
    b.button(text="⬅️ Sinflarni qayta tanlash", callback_data="np:back")
    b.adjust(1)
    return b.as_markup()


@admin.callback_query(NewProject.confirm, F.data == "np:back")
async def np_back(c: CallbackQuery, state: FSMContext):
    sel = (await state.get_data()).get("sel", [])
    await state.set_state(NewProject.classes)
    await show(c, "🏫 <b>Qaysi sinflar qatnashadi?</b>", classes_kb(sel))
    await c.answer()


@admin.callback_query(NewProject.confirm, F.data == "np:edit")
async def np_edit(c: CallbackQuery, state: FSMContext):
    sel = sorted((await state.get_data()).get("sel", []))
    example = ", ".join(f"{k}={v}" for k, v in split_seats(sel, TOTAL_SEATS).items())
    await state.set_state(NewProject.limits)
    await show(
        c,
        f"✏️ Har bir sinf uchun joy sonini yozing. Yig'indi aynan <b>{TOTAL_SEATS}</b> bo'lishi kerak.\n\n"
        f"Namuna:\n<code>{example}</code>",
    )
    await c.answer()


@admin.message(NewProject.limits, F.text, ~F.text.startswith("/"))
async def np_limits(m: Message, state: FSMContext):
    sel = (await state.get_data()).get("sel", [])
    dist, err = parse_limits(m.text, sel, TOTAL_SEATS)
    if err:
        await m.answer(f"⚠️ {err}")
        return
    await state.update_data(dist={str(k): v for k, v in dist.items()})
    await state.set_state(NewProject.confirm)
    await m.answer(preview_text(dist), reply_markup=preview_kb())


@admin.callback_query(NewProject.confirm, F.data == "np:ok")
async def np_ok(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    cur = conn.execute(
        "INSERT INTO projects(name, date, time, place, total, status, created_at) VALUES(?,?,?,?,?,'active',?)",
        (d["name"], d["date"], d["time"], d["place"], TOTAL_SEATS, now()),
    )
    pid = cur.lastrowid
    for cls, seats in d["dist"].items():
        conn.execute("INSERT INTO project_classes(project_id, class, seats) VALUES(?,?,?)", (pid, int(cls), seats))
    conn.commit()
    await state.clear()
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    b = InlineKeyboardBuilder()
    b.button(text="📣 Hammaga e'lon qilish", callback_data=f"ann:{pid}")
    b.button(text="🛡 Admin panel", callback_data="adm:home")
    b.adjust(1)
    await show(c, f"✅ <b>Loyiha yaratildi!</b>\n\n{project_text(p)}", b.as_markup())
    await c.answer()


# ---- Loyihalar ro'yxati va boshqaruv
@admin.callback_query(F.data == "adm:list")
async def adm_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    ps = q("SELECT * FROM projects ORDER BY id DESC LIMIT 20")
    if not ps:
        await show(c, "Hali loyiha yo'q.", admin_home_kb())
        await c.answer()
        return
    b = InlineKeyboardBuilder()
    for p in ps:
        icon = "🟢" if p["status"] == "active" else "⚪️"
        b.button(text=f"{icon} {p['name']} · {p['date']}", callback_data=f"adm:p:{p['id']}")
    b.button(text="⬅️ Admin panel", callback_data="adm:home")
    b.adjust(1)
    await show(c, "📋 <b>Loyihalar</b>", b.as_markup())
    await c.answer()


@admin.callback_query(F.data.startswith("adm:p:"))
async def adm_project(c: CallbackQuery, state: FSMContext):
    await state.clear()
    p = q1("SELECT * FROM projects WHERE id=?", (int(c.data.split(":")[2]),))
    if not p:
        await c.answer("Topilmadi.", show_alert=True)
        return
    await show(c, project_text(p), project_kb(p["id"], p["status"]))
    await c.answer()


def participants(pid):
    return q(
        "SELECT * FROM registrations WHERE project_id=? ORDER BY class, last_name, first_name",
        (pid,),
    )


@admin.callback_query(F.data.startswith("adm:ppl:"))
async def adm_people(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    rows = participants(pid)
    if not rows:
        text = "Hali hech kim ro'yxatdan o'tmagan."
    else:
        text = "👥 <b>Qatnashuvchilar</b>\n\n" + "\n".join(
            f"{i}. {esc(r['last_name'])} {esc(r['first_name'])} — {r['class']}-sinf"
            for i, r in enumerate(rows, 1)
        )
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Orqaga", callback_data=f"adm:p:{pid}")
    await show(c, text, b.as_markup())
    await c.answer()


@admin.callback_query(F.data.startswith("adm:csv:"))
async def adm_csv(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["№", "Familiya", "Ism", "Sinf", "Telegram ID", "Username", "Vaqt"])
    for i, r in enumerate(participants(pid), 1):
        w.writerow([i, r["last_name"], r["first_name"], r["class"], r["tg_id"], r["username"], r["created_at"]])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # Excel uchun BOM
    await c.message.answer_document(BufferedInputFile(data, filename=f"{p['name']}_qatnashuvchilar.csv"))
    await c.answer()


@admin.callback_query(F.data.startswith("adm:lim:"))
async def adm_limits(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    st = class_stats(pid)
    cur = ", ".join(f"{r['cls']}={r['seats']}" for r in st)
    taken = "\n".join(f"• {r['cls']}-sinf: {r['taken']} kishi yozilgan" for r in st)
    p = q1("SELECT total FROM projects WHERE id=?", (pid,))
    await state.set_state(EditLimits.limits)
    await state.update_data(pid=pid)
    await show(
        c,
        f"⚙️ Yangi limitlarni yozing. Yig'indi aynan <b>{p['total']}</b> bo'lishi kerak.\n\n"
        f"Hozirgi:\n<code>{cur}</code>\n\n{taken}\n\n(Bekor qilish: /cancel)",
    )
    await c.answer()


@admin.message(EditLimits.limits, F.text, ~F.text.startswith("/"))
async def adm_limits_text(m: Message, state: FSMContext):
    pid = (await state.get_data())["pid"]
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    st = class_stats(pid)
    dist, err = parse_limits(m.text, [r["cls"] for r in st], p["total"])
    if err:
        await m.answer(f"⚠️ {err}")
        return
    for r in st:
        if dist[r["cls"]] < r["taken"]:
            await m.answer(f"⚠️ {r['cls']}-sinfdan {r['taken']} kishi yozilgan, limit undan kam bo'lmasin.")
            return
    for cls, seats in dist.items():
        conn.execute("UPDATE project_classes SET seats=? WHERE project_id=? AND class=?", (seats, pid, cls))
    conn.commit()
    await state.clear()
    await m.answer("✅ Limitlar yangilandi.\n\n" + project_text(p), reply_markup=project_kb(pid, p["status"]))


@admin.callback_query(F.data.startswith("adm:msg:"))
async def adm_msg(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    await state.set_state(Broadcast.text)
    await state.update_data(pid=pid)
    await show(c, "📢 Loyiha qatnashuvchilariga yuboriladigan xabarni yozing:\n(Bekor qilish: /cancel)")
    await c.answer()


@admin.message(Broadcast.text, F.text, ~F.text.startswith("/"))
async def adm_msg_send(m: Message, state: FSMContext, bot: Bot):
    pid = (await state.get_data())["pid"]
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    await state.clear()
    ids = [r["tg_id"] for r in participants(pid)]
    ok, fail = await send_many(bot, ids, f"📢 <b>{esc(p['name'])}</b>\n\n{esc(m.text)}")
    await m.answer(f"Yuborildi: {ok} ta, yetmadi: {fail} ta.", reply_markup=project_kb(pid, p["status"]))


@admin.callback_query(F.data.startswith("ann:"))
async def adm_announce(c: CallbackQuery, bot: Bot):
    pid = int(c.data.split(":")[1])
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    ids = [r["tg_id"] for r in q("SELECT tg_id FROM users")]
    await c.answer("Yuborilmoqda...")
    ok, fail = await send_many(bot, ids, f"🆕 <b>Yangi loyiha!</b>\n\n{project_text(p, False)}\n\n/start bosib ro'yxatdan o'ting.")
    await c.message.answer(f"E'lon yuborildi: {ok} ta, yetmadi: {fail} ta.")


@admin.callback_query(F.data.startswith("adm:close:"))
async def adm_close(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    b = InlineKeyboardBuilder()
    b.button(text="✅ Ha, yakunlash", callback_data=f"adm:closeok:{pid}")
    b.button(text="⬅️ Yo'q", callback_data=f"adm:p:{pid}")
    b.adjust(1)
    await show(c, "Loyiha yakunlansa, yangi ro'yxatdan o'tish yopiladi. Davom etasizmi?", b.as_markup())
    await c.answer()


@admin.callback_query(F.data.startswith("adm:closeok:"))
async def adm_close_ok(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    run("UPDATE projects SET status='closed' WHERE id=?", (pid,))
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    await show(c, "🔒 Loyiha yakunlandi.\n\n" + project_text(p), project_kb(pid, "closed"))
    await c.answer()


# ----------------------------------------------------------------- ISHGA TUSHIRISH (WEBHOOK)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylga BOT_TOKEN=... deb yozing.")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS bo'sh! Admin panel hech kimga ochilmaydi.")
        
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)
    dp.include_router(router)

    # Render tomonidan avtomatik beriladigan tashqi URL va Port
    WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")
    if not WEBHOOK_HOST:
        raise SystemExit("RENDER_EXTERNAL_URL topilmadi. Render'da Web Service sifatida ulanganingizga ishonch hosil qiling.")
    
    WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
    WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    
    WEB_SERVER_HOST = "0.0.0.0"
    WEB_SERVER_PORT = int(os.getenv("PORT", 8080))

    # Telegram'ga webhook manzilini o'rnatamiz
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

    app = web.Application()
    
    # Webhook so'rovlarini qabul qiluvchi handler
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()
    
    logging.info(f"Bot Webhook rejimida ishga tushdi: {WEBHOOK_URL}")
    
    # Server to'xtamasligi uchun cheksiz kutish
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())