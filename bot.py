import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg2

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
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
    "Buyruqlar:\n"
    "/bugun - bugungi jadval\n"
    "/ertaga - ertangi jadval\n"
    "/hafta - 7 kunlik jadval\n"
    "/bekor - o'zingiz qo'shgan operatsiyalar ro'yxati\n"
    "/bekor 3 - 3-raqamli operatsiyani o'chirish\n"
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

    con = db()
    overlaps = con.execute(
        "SELECT doctor, start, finish FROM ops WHERE day=? AND start<? AND finish>?",
        (d.isoformat(), end, start),
    ).fetchall()
    cur = con.execute(
        "INSERT INTO ops (doctor, day, start, finish, user_id) VALUES (?,?,?,?,?) RETURNING id",
        (name, d.isoformat(), start, end, update.effective_user.id),
    )
    op_id = cur.fetchone()[0]
    con.commit()
    con.close()

    reply = f"✅ Saqlandi #{op_id}\nDr. {name}, {fmt_day(d)}, {start}–{end}"
    if overlaps:
        reply += "\n\n⚠️ Shu vaqtda boshqa operatsiya ham bor:\n"
        reply += "\n".join(f"• Dr. {o[0]} {o[1]}–{o[2]}" for o in overlaps)
    await msg.reply_text(reply)


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
        cur = con.execute(
            "DELETE FROM ops WHERE id=? AND user_id=?", (int(ctx.args[0]), uid)
        )
        con.commit()
        con.close()
        ok = cur.rowcount > 0
        await update.message.reply_text(
            "🗑 O'chirildi" if ok else "Topilmadi yoki bu operatsiyani siz qo'shmagansiz."
        )
        return
    rows = con.execute(
        "SELECT id, doctor, day, start, finish FROM ops WHERE user_id=? AND day>=? ORDER BY day, start",
        (uid, today().isoformat()),
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
        "Sizning operatsiyalaringiz:\n" + "\n".join(lines) + "\n\nO'chirish: /bekor RAQAM"
    )


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
    app.add_handler(CommandHandler("guruh", cmd_group))
    app.add_handler(CommandHandler("oper", add_op))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_op))
    app.job_queue.run_daily(morning, MORNING)
    app.run_polling()


if __name__ == "__main__":
    main()
