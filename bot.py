import os
import re
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg2

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo("Asia/Tashkent")
MORNING = time(7, 0, tzinfo=TZ)  # har kuni ertalab soat 7:00
DATABASE_URL = os.environ["DATABASE_URL"]

DAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]

PATTERN = re.compile(
    r"^(?P<name>.+?)\s+(?P<d>\d{1,2})[.\-/](?P<m>\d{1,2})\s+"
    r"(?P<sh>\d{1,2})[:.](?P<sm>\d{2})\s*[-–—]\s*(?P<eh>\d{1,2})[:.](?P<em>\d{2})$"
)

HELP = (
    "🏥 Operatsiya jadvali boti\n\n"
    "Operatsiya qo'shish (shu formatda yozing):\n"
    "Aliyev 15.10 10:00-12:30\n"
    "(ism, kun.oy, boshlanish-taxminiy tugash)\n\n"
    "Vaqt o'zgarsa: xuddi shu xabarni yangi vaqt bilan qaytadan yozing, "
    "bot o'zgartirishni taklif qiladi.\n\n"
    "Buyruqlar:\n"
    "/bugun - bugungi jadval\n"
    "/ertaga - ertangi jadval\n"
    "/hafta - 7 kunlik jadval\n"
    "/bekor - oxirgi qo'shgan operatsiyangizni bekor qilish\n"
    "/bekor 3 - 3-raqamli operatsiyani bekor qilish\n"
    "/royxat - o'zingiz qo'shgan operatsiyalar ro'yxati\n"
    "/guruh - shu chatga har kuni ertalab jadval yuborish"
)


class DB:
    def __init__(self):
        self.con = psycopg2.connect(DATABASE_URL)

    def execute(self, sql, params=()):
        cur = self.con.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def commit(self):
        self.con.commit()

    def close(self):
        self.con.close()


def db():
    con = DB()
    con.execute(
        "CREATE TABLE IF NOT EXISTS ops (id SERIAL PRIMARY KEY, "
        "doctor TEXT, day TEXT, start TEXT, finish TEXT, user_id BIGINT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS chats (chat_id BIGINT PRIMARY KEY)")
    con.commit()
    return con


def today() -> date:
    return datetime.now(TZ).date()


def fmt_day(d: date) -> str:
    return f"{d.strftime('%d.%m')} ({DAYS[d.weekday()]})"


def schedule_text(d: date, title: str = "") -> str:
    con = db()
    rows = con.execute(
        "SELECT id, doctor, start, finish FROM ops WHERE day=? ORDER BY start", (d.isoformat(),)
    ).fetchall()
    con.close()
    head = f"📅 {title + ', ' if title else ''}{fmt_day(d)}"
    if not rows:
        return f"{head}\nOperatsiya yo'q ✅"
    lines = [f"🕐 {s}–{e}  Dr. {doc}  (#{i})" for i, doc, s, e in rows]
    return head + "\n" + "\n".join(lines)


def parse(text: str):
    m = PATTERN.match(text.strip())
    if not m:
        return None
    day, month = int(m["d"]), int(m["m"])
    sh, sm, eh, em = int(m["sh"]), int(m["sm"]), int(m["eh"]), int(m["em"])
    if not (0 <= sh < 24 and 0 <= eh < 24 and sm < 60 and em < 60):
        return None
    year = today().year
    try:
        d = date(year, month, day)
        if d < today() - timedelta(days=30):
            d = date(year + 1, month, day)
    except ValueError:
        return None
    start, end = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
    if end <= start:
        return None
    name = m["name"].strip().title()
    name = re.sub(r"^(Dr\.?|Doktor)\s+", "", name, flags=re.I)
    return name, d, start, end


def insert_op(con, name, d, start, end, uid) -> str:
    overlaps = con.execute(
        "SELECT doctor, start, finish FROM ops WHERE day=? AND start<? AND finish>?",
        (d.isoformat(), end, start),
    ).fetchall()
    op_id = con.execute(
        "INSERT INTO ops (doctor, day, start, finish, user_id) VALUES (?,?,?,?,?) RETURNING id",
        (name, d.isoformat(), start, end, uid),
    ).fetchone()[0]
    con.commit()
    reply = f"✅ Saqlandi #{op_id}\nDr. {name}, {fmt_day(d)}, {start}–{end}"
    if overlaps:
        reply += "\n\n⚠️ Shu vaqtda boshqa operatsiya ham bor:\n"
        reply += "\n".join(f"• Dr. {o[0]} {o[1]}–{o[2]}" for o in overlaps)
    return reply


async def add_op(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text or ""
    if text.startswith("/oper"):
        text = text[5:]
    res = parse(text)
    if not res:
        if msg.chat.type == "private":
            await msg.reply_text("Tushunmadim. Namuna:\nAliyev 15.10 10:00-12:30")
        return  # guruhda boshqa gaplarga javob bermaydi
    name, d, start, end = res
    uid = update.effective_user.id

    con = db()
    existing = con.execute(
        "SELECT id, start, finish FROM ops WHERE lower(doctor)=lower(?) AND day=? ORDER BY id DESC",
        (name, d.isoformat()),
    ).fetchall()
    if not existing:
        reply = insert_op(con, name, d, start, end, uid)
        con.close()
        await msg.reply_text(reply)
        return
    con.close()

    if any(e[1] == start and e[2] == end for e in existing):
        await msg.reply_text("Bu operatsiya allaqachon jadvalda bor ✅")
        return

    old = existing[0]
    key = uuid.uuid4().hex[:8]
    ctx.bot_data.setdefault("pending", {})[key] = {
        "uid": uid, "name": name, "day": d, "start": start, "end": end, "op_id": old[0],
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✏️ {start}–{end} ga o'zgartirish", callback_data=f"upd:{key}")],
        [InlineKeyboardButton("➕ Yangi operatsiya qo'shish", callback_data=f"new:{key}")],
    ])
    await msg.reply_text(
        f"Dr. {name}ning {fmt_day(d)} kuni {old[1]}–{old[2]} operatsiyasi bor.\nNima qilamiz?",
        reply_markup=kb,
    )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, key = q.data.split(":", 1)
    p = ctx.bot_data.get("pending", {}).get(key)
    if not p:
        await q.answer()
        await q.edit_message_text("Muddati o'tgan. Operatsiyani qaytadan yozing.")
        return
    if q.from_user.id != p["uid"]:
        await q.answer("Bu tugma sizga emas.", show_alert=True)
        return
    await q.answer()
    ctx.bot_data["pending"].pop(key, None)

    con = db()
    if action == "upd":
        overlaps = con.execute(
            "SELECT doctor, start, finish FROM ops WHERE day=? AND start<? AND finish>? AND id<>?",
            (p["day"].isoformat(), p["end"], p["start"], p["op_id"]),
        ).fetchall()
        con.execute(
            "UPDATE ops SET start=?, finish=?, user_id=? WHERE id=?",
            (p["start"], p["end"], p["uid"], p["op_id"]),
        )
        con.commit()
        reply = (
            f"✏️ O'zgartirildi #{p['op_id']}\n"
            f"Dr. {p['name']}, {fmt_day(p['day'])}, {p['start']}–{p['end']}"
        )
        if overlaps:
            reply += "\n\n⚠️ Shu vaqtda boshqa operatsiya ham bor:\n"
            reply += "\n".join(f"• Dr. {o[0]} {o[1]}–{o[2]}" for o in overlaps)
    else:
        reply = insert_op(con, p["name"], p["day"], p["start"], p["end"], p["uid"])
    con.close()
    await q.edit_message_text(reply)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(schedule_text(today(), "Bugun"))


async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(schedule_text(today() + timedelta(days=1), "Ertaga"))


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = [schedule_text(today() + timedelta(days=i)) for i in range(7)]
    await update.message.reply_text("\n\n".join(parts))


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    if ctx.args and ctx.args[0].isdigit():
        row = con.execute(
            "DELETE FROM ops WHERE id=? AND user_id=? RETURNING doctor, day, start, finish",
            (int(ctx.args[0]), uid),
        ).fetchone()
    else:
        row = con.execute(
            "DELETE FROM ops WHERE id = (SELECT id FROM ops WHERE user_id=? "
            "ORDER BY id DESC LIMIT 1) RETURNING doctor, day, start, finish",
            (uid,),
        ).fetchone()
    con.commit()
    con.close()
    if not row:
        await update.message.reply_text("Bekor qilinadigan operatsiya topilmadi.")
        return
    doc, d, s, e = row
    await update.message.reply_text(
        f"🗑 Bekor qilindi: Dr. {doc}, {fmt_day(date.fromisoformat(d))}, {s}–{e}"
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    rows = con.execute(
        "SELECT id, doctor, day, start, finish FROM ops WHERE user_id=? AND day>=? ORDER BY day, start",
        (update.effective_user.id, today().isoformat()),
    ).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("Sizning kelgusi operatsiyalaringiz yo'q.")
        return
    lines = [
        f"#{i}  {date.fromisoformat(d).strftime('%d.%m')}  {s}–{e}  Dr. {doc}"
        for i, doc, d, s, e in rows
    ]
    await update.message.reply_text(
        "Sizning operatsiyalaringiz:\n" + "\n".join(lines) + "\n\nBittasini o'chirish: /bekor RAQAM"
    )


CHANGE = re.compile(
    r"^(?:(?P<id>\d+)\s+)?(?:(?P<d>\d{1,2})[.\-/](?P<m>\d{1,2})\s+)?"
    r"(?P<sh>\d{1,2})[:.](?P<sm>\d{2})\s*[-–—]\s*(?P<eh>\d{1,2})[:.](?P<em>\d{2})$"
)


async def cmd_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    m = CHANGE.match(" ".join(ctx.args).strip())
    if not m:
        await msg.reply_text(
            "Namuna:\n/ozgar 11:00-13:30  (oxirgi operatsiya vaqti)\n"
            "/ozgar 16.10 11:00-13:30  (kun va vaqt)\n"
            "/ozgar 3 11:00-13:30  (3-raqamli operatsiya)"
        )
        return
    sh, sm, eh, em = int(m["sh"]), int(m["sm"]), int(m["eh"]), int(m["em"])
    start, end = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
    if not (sh < 24 and eh < 24 and sm < 60 and em < 60) or end <= start:
        await msg.reply_text("Vaqt noto'g'ri. Namuna: 11:00-13:30")
        return

    uid = update.effective_user.id
    con = db()
    if m["id"]:
        row = con.execute(
            "SELECT id, doctor, day FROM ops WHERE id=? AND user_id=?", (int(m["id"]), uid)
        ).fetchone()
    else:
        row = con.execute(
            "SELECT id, doctor, day FROM ops WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()
    if not row:
        con.close()
        await msg.reply_text("O'zgartiriladigan operatsiya topilmadi.")
        return
    op_id, doctor, day = row
    if m["d"]:
        try:
            d = date(today().year, int(m["m"]), int(m["d"]))
            if d < today() - timedelta(days=30):
                d = date(today().year + 1, int(m["m"]), int(m["d"]))
        except ValueError:
            con.close()
            await msg.reply_text("Sana noto'g'ri. Namuna: 16.10")
            return
        day = d.isoformat()
    overlaps = con.execute(
        "SELECT doctor, start, finish FROM ops WHERE day=? AND start<? AND finish>? AND id<>?",
        (day, end, start, op_id),
    ).fetchall()
    con.execute(
        "UPDATE ops SET day=?, start=?, finish=? WHERE id=?", (day, start, end, op_id)
    )
    con.commit()
    con.close()

    reply = f"✏️ O'zgartirildi #{op_id}\nDr. {doctor}, {fmt_day(date.fromisoformat(day))}, {start}–{end}"
    if overlaps:
        reply += "\n\n⚠️ Shu vaqtda boshqa operatsiya ham bor:\n"
        reply += "\n".join(f"• Dr. {o[0]} {o[1]}–{o[2]}" for o in overlaps)
    await msg.reply_text(reply)


async def cmd_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    con.execute("INSERT INTO chats (chat_id) VALUES (?) ON CONFLICT DO NOTHING", (update.effective_chat.id,))
    con.commit()
    con.close()
    await update.message.reply_text("✅ Har kuni soat 07:00 da shu yerga bugungi jadval yuboriladi.")


async def morning(ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    chats = [r[0] for r in con.execute("SELECT chat_id FROM chats")]
    con.close()
    text = "☀️ Xayrli tong!\n\n" + schedule_text(today(), "Bugun")
    for chat_id in chats:
        try:
            await ctx.bot.send_message(chat_id, text)
        except Exception as e:
            print("Yuborib bo'lmadi:", chat_id, e)


def main():
    db().close()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("bugun", cmd_today))
    app.add_handler(CommandHandler("ertaga", cmd_tomorrow))
    app.add_handler(CommandHandler("hafta", cmd_week))
    app.add_handler(CommandHandler("bekor", cmd_cancel))
    app.add_handler(CommandHandler("royxat", cmd_list))
    app.add_handler(CommandHandler("ozgar", cmd_change))
    app.add_handler(CommandHandler("guruh", cmd_group))
    app.add_handler(CommandHandler("oper", add_op))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(upd|new):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_op))
    app.job_queue.run_daily(morning, MORNING)
    app.run_polling()


if __name__ == "__main__":
    main()
