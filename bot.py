import hashlib
import hmac
import html
import os
import re
import threading
import uuid
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
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
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SITE_KEY = hashlib.sha256(TOKEN.encode()).hexdigest()[:12]  # havola uchun maxfiy kalit

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


# ---------------------------------------------------------------- SAYT ----

PX = 64  # 1 soat = 64 piksel

PAGE = """<!doctype html>
<html lang="uz"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>Operatsiyalar __DATE__</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e8eaed;--muted:#9aa0a6;--line:#2a2f3a;--accent:#60a5fa}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:12px}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0 12px}
h1{margin:0;font-size:22px}
.sub{color:var(--muted);font-size:14px;margin-top:2px}
nav{display:flex;gap:8px;margin-top:10px}
nav a{flex:1;text-align:center;padding:10px;border:1px solid var(--line);background:var(--card);
 border-radius:10px;color:var(--text);text-decoration:none;font-size:15px}
nav a.today{background:var(--accent);color:#fff;border-color:var(--accent)}
.cal{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;
 margin:16px 0 24px;padding:0;height:__HEIGHT__px}
.hour{position:absolute;left:0;right:0;border-top:1px solid var(--line)}
.hour span{position:absolute;left:8px;top:-9px;background:var(--card);padding:0 4px;
 font-size:12px;color:var(--muted)}
.events{position:absolute;left:58px;right:8px;top:0;bottom:0}
.ev{position:absolute;border-radius:8px;padding:6px 8px;color:#fff;font-size:13px;line-height:1.25;
 overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.25);border-left:4px solid rgba(255,255,255,.55)}
.ev b{display:block;font-size:14px}
.now{position:absolute;left:52px;right:0;border-top:2px solid #ef4444;z-index:3}
.now:before{content:"";position:absolute;left:-5px;top:-6px;width:10px;height:10px;border-radius:50%;background:#ef4444}
.empty{padding:14px;text-align:center;color:var(--muted)}
</style></head><body><div class="wrap">
<header>
<h1>📅 __DATE__ · __WEEKDAY__</h1>
<div class="sub">__COUNT__</div>
<nav><a href="__PREV__">‹ Kecha</a><a class="today" href="__TODAY__">Bugun</a><a href="__NEXT__">Ertaga ›</a></nav>
</header>
<div class="cal">__HOURS__<div class="events">__EVENTS__</div>__NOW__</div>
</div></body></html>"""


def to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def layout(ops):
    """Bir vaqtga to'g'ri kelgan operatsiyalarni yonma-yon joylashtiradi."""
    ops = sorted(ops, key=lambda o: (to_min(o[2]), to_min(o[3])))
    result, cluster, lane_ends, cluster_end = [], [], [], -1

    def flush():
        n = len(lane_ends)
        result.extend((op, lane, n) for op, lane in cluster)

    for op in ops:
        st, en = to_min(op[2]), to_min(op[3])
        if cluster and st >= cluster_end:
            flush()
            cluster, lane_ends, cluster_end = [], [], -1
        for i, le in enumerate(lane_ends):
            if le <= st:
                lane, lane_ends[i] = i, en
                break
        else:
            lane = len(lane_ends)
            lane_ends.append(en)
        cluster.append((op, lane))
        cluster_end = max(cluster_end, en)
    if cluster:
        flush()
    return result


def render_day(d: date, ops, key: str) -> str:
    first, last = 7, 19
    if ops:
        first = min(first, min(to_min(o[2]) for o in ops) // 60)
        last = max(last, -(-max(to_min(o[3]) for o in ops) // 60))
    height = (last - first) * PX

    hours = "".join(
        f'<div class="hour" style="top:{(h - first) * PX}px"><span>{h:02d}:00</span></div>'
        for h in range(first, last + 1)
    )
    events = ""
    for (op_id, doctor, st, en), lane, n in layout(ops):
        top = (to_min(st) - first * 60) * PX / 60
        h = max((to_min(en) - to_min(st)) * PX / 60, 30)
        hue = int(hashlib.md5(doctor.lower().encode()).hexdigest()[:4], 16) % 360
        events += (
            f'<div class="ev" style="top:{top:.0f}px;height:{h:.0f}px;'
            f'left:calc({lane} * 100% / {n});width:calc(100% / {n} - 4px);'
            f'background:hsl({hue} 62% 40%)">'
            f"<b>Dr. {html.escape(doctor)}</b>{st}–{en}</div>"
        )
    now_html = ""
    if d == today():
        now = datetime.now(TZ)
        mins = now.hour * 60 + now.minute
        if first * 60 <= mins <= last * 60:
            now_html = f'<div class="now" style="top:{(mins - first * 60) * PX / 60:.0f}px"></div>'

    def link(x):
        return f"/?k={key}&d={x.isoformat()}"

    count = f"{len(ops)} ta operatsiya" if ops else "Bu kuni operatsiya yo'q ✅"
    return (
        PAGE.replace("__DATE__", d.strftime("%d.%m.%Y"))
        .replace("__WEEKDAY__", DAYS[d.weekday()])
        .replace("__COUNT__", count)
        .replace("__HEIGHT__", str(height))
        .replace("__HOURS__", hours)
        .replace("__EVENTS__", events)
        .replace("__NOW__", now_html)
        .replace("__PREV__", link(d - timedelta(days=1)))
        .replace("__NEXT__", link(d + timedelta(days=1)))
        .replace("__TODAY__", link(today()))
    )


def day_link(d: date) -> str:
    return f"{PUBLIC_URL}/?k={SITE_KEY}&d={d.isoformat()}" if PUBLIC_URL else ""


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/health":
            return self.reply(200, "ok", "text/plain")
        if not hmac.compare_digest(q.get("k", [""])[0], SITE_KEY):
            return self.reply(403, "Havola noto'g'ri yoki eskirgan.", "text/plain; charset=utf-8")
        try:
            d = date.fromisoformat(q.get("d", [""])[0])
        except ValueError:
            d = today()
        try:
            con = db()
            ops = con.execute(
                "SELECT id, doctor, start, finish FROM ops WHERE day=? ORDER BY start",
                (d.isoformat(),),
            ).fetchall()
            con.close()
            self.reply(200, render_day(d, ops, SITE_KEY))
        except Exception as e:
            print("Sayt xatosi:", e)
            self.reply(500, "Xatolik. Birozdan keyin qayta urinib ko'ring.", "text/plain; charset=utf-8")


def start_site():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


def with_link(text: str, d: date) -> str:
    link = day_link(d)
    return f"{text}\n\n📆 Soatlar bo'yicha ko'rish:\n{link}" if link else text


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(with_link(schedule_text(today(), "Bugun"), today()))


async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = today() + timedelta(days=1)
    await update.message.reply_text(with_link(schedule_text(d, "Ertaga"), d))


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
    text = "☀️ Xayrli tong!\n\n" + with_link(schedule_text(today(), "Bugun"), today())
    for chat_id in chats:
        try:
            await ctx.bot.send_message(chat_id, text)
        except Exception as e:
            print("Yuborib bo'lmadi:", chat_id, e)


def main():
    db().close()
    start_site()
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
