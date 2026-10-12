import base64
import hashlib
import hmac
import json
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
DATABASE_URL = os.environ["DATABASE_URL"]
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SITE_KEY = hashlib.sha256(TOKEN.encode()).hexdigest()[:12]  # havola uchun maxfiy kalit
TZ = ZoneInfo("Asia/Tashkent")
MORNING = time(7, 0, tzinfo=TZ)  # har kuni ertalab soat 7:00

DAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
CATS = {"lor": ("LOR", "👂"), "stom": ("Stomatologiya", "🦷")}

PATTERN = re.compile(
    r"^(?P<name>.+?)\s+(?P<d>\d{1,2})[.\-/](?P<m>\d{1,2})\s+"
    r"(?P<sh>\d{1,2})[:.](?P<sm>\d{2})\s*[-–—]\s*(?P<eh>\d{1,2})[:.](?P<em>\d{2})"
    r"(?:\s+(?P<cat>lor|stom\w*))?$",
    re.I,
)

HELP = (
    "🏥 EMU Clinic · Operatsiya jadvali boti\n\n"
    "Operatsiya qo'shish (shu formatda yozing):\n"
    "Aliyev 15.10 10:00-12:30\n"
    "(ism, kun.oy, boshlanish-taxminiy tugash)\n"
    "Oxiriga lor yoki stom deb yozsangiz, bo'lim ham tanlanadi:\n"
    "Aliyev 15.10 10:00-12:30 lor\n\n"
    "Vaqt o'zgarsa: xuddi shu xabarni yangi vaqt bilan qaytadan yozing, "
    "bot o'zgartirishni taklif qiladi.\n\n"
    "Buyruqlar:\n"
    "/bugun - bugungi jadval\n"
    "/ertaga - ertangi jadval\n"
    "/hafta - 7 kunlik jadval\n"
    "/bekor - oxirgi qo'shgan operatsiyangizni bekor qilish\n"
    "/bekor 3 - 3-raqamli operatsiyani bekor qilish\n"
    "/royxat - o'zingiz qo'shgan operatsiyalar ro'yxati\n"
    "/guruh - shu chatga har kuni ertalab jadval yuborish\n\n"
    "Saytda (havola orqali) jadvalni soatlar bo'yicha ko'rish va operatsiyani "
    "to'g'ridan-to'g'ri band qilish mumkin."
)


# ------------------------------------------------------------------ BAZA ----

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
    return DB()


def init_db():
    con = DB()
    con.execute(
        "CREATE TABLE IF NOT EXISTS ops (id SERIAL PRIMARY KEY, "
        "doctor TEXT, day TEXT, start TEXT, finish TEXT, user_id BIGINT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS chats (chat_id BIGINT PRIMARY KEY)")
    for col in (
        "category TEXT NOT NULL DEFAULT 'lor'",
        "op_type TEXT NOT NULL DEFAULT ''",
        "note TEXT NOT NULL DEFAULT ''",
    ):
        con.execute(f"ALTER TABLE ops ADD COLUMN IF NOT EXISTS {col}")
    con.commit()
    con.close()


# ---------------------------------------------------------------- YORDAMCHI ----

def today() -> date:
    return datetime.now(TZ).date()


def fmt_day(d: date) -> str:
    return f"{d.strftime('%d.%m')} ({DAYS[d.weekday()]})"


def norm_name(s: str) -> str:
    s = re.sub(r"^(dr\.?|doktor)\s+", "", s.strip(), flags=re.I)
    return " ".join(w[:1].upper() + w[1:] for w in s.split())


def norm_time(t: str):
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", t.strip())
    if not m:
        return None
    h, mi = int(m[1]), int(m[2])
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def find_conflicts(con, day_iso, cat, start, end, exclude_id=0):
    return con.execute(
        "SELECT doctor, start, finish FROM ops "
        "WHERE day=? AND category=? AND start<? AND finish>? AND id<>?",
        (day_iso, cat, end, start, exclude_id),
    ).fetchall()


def conflict_text(conflicts) -> str:
    return "\n\n⚠️ Shu vaqtda xona band:\n" + "\n".join(
        f"• Dr. {c[0]} {c[1]}–{c[2]}" for c in conflicts
    )


def op_line(op_id, doctor, start, end, op_type) -> str:
    extra = f" — {op_type}" if op_type else ""
    return f"🕐 {start}–{end}  Dr. {doctor}{extra}  (#{op_id})"


def schedule_text(d: date, title: str = "") -> str:
    con = db()
    rows = con.execute(
        "SELECT id, doctor, start, finish, category, op_type FROM ops WHERE day=? ORDER BY start",
        (d.isoformat(),),
    ).fetchall()
    con.close()
    head = f"📅 {title + ', ' if title else ''}{fmt_day(d)}"
    if not rows:
        return f"{head}\nOperatsiya yo'q ✅"
    parts = [head]
    for cat, (label, emoji) in CATS.items():
        cat_rows = [r for r in rows if r[4] == cat]
        if cat_rows:
            parts.append(f"\n{emoji} {label}")
            parts.extend(op_line(r[0], r[1], r[2], r[3], r[5]) for r in cat_rows)
    return "\n".join(parts)


def day_link(d: date, cat: str = "lor") -> str:
    if not PUBLIC_URL:
        return ""
    return f"{PUBLIC_URL}/?k={SITE_KEY}&c={cat}&d={d.isoformat()}"


def with_link(text: str, d: date) -> str:
    link = day_link(d)
    return f"{text}\n\n📆 Soatlar bo'yicha ko'rish va band qilish:\n{link}" if link else text


# ----------------------------------------------------------- TELEGRAM BOT ----

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
    cat = None
    if m["cat"]:
        cat = "lor" if m["cat"].lower().startswith("lor") else "stom"
    return norm_name(m["name"]), d, start, end, cat


def insert_op(con, name, d, start, end, uid, cat, op_type="", note="") -> str:
    conflicts = find_conflicts(con, d.isoformat(), cat, start, end)
    op_id = con.execute(
        "INSERT INTO ops (doctor, day, start, finish, user_id, category, op_type, note) "
        "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (name, d.isoformat(), start, end, uid, cat, op_type, note),
    ).fetchone()[0]
    con.commit()
    label, emoji = CATS[cat]
    reply = f"✅ Saqlandi #{op_id}\n{emoji} {label}\nDr. {name}, {fmt_day(d)}, {start}–{end}"
    if conflicts:
        reply += conflict_text(conflicts)
    return reply


def process(ctx, p):
    """Yangi operatsiyani saqlaydi yoki (shu doktor, shu kun) takror bo'lsa so'raydi."""
    con = db()
    d = p["day"]
    existing = con.execute(
        "SELECT id, start, finish FROM ops WHERE lower(doctor)=lower(?) AND day=? AND category=? "
        "ORDER BY id DESC",
        (p["name"], d.isoformat(), p["cat"]),
    ).fetchall()
    if not existing:
        text = insert_op(con, p["name"], d, p["start"], p["end"], p["uid"], p["cat"])
        con.close()
        return text, None
    con.close()
    if any(e[1] == p["start"] and e[2] == p["end"] for e in existing):
        return "Bu operatsiya allaqachon jadvalda bor ✅", None

    old = existing[0]
    key = uuid.uuid4().hex[:8]
    ctx.bot_data.setdefault("pending", {})[key] = dict(p, op_id=old[0])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✏️ {p['start']}–{p['end']} ga o'zgartirish", callback_data=f"upd:{key}")],
        [InlineKeyboardButton("➕ Yangi operatsiya qo'shish", callback_data=f"new:{key}")],
    ])
    text = (
        f"Dr. {p['name']}ning {fmt_day(d)} kuni {old[1]}–{old[2]} operatsiyasi bor.\n"
        "Nima qilamiz?"
    )
    return text, kb


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
    name, d, start, end, cat = res
    p = {"uid": update.effective_user.id, "name": name, "day": d,
         "start": start, "end": end, "cat": cat}

    if cat is None:
        key = uuid.uuid4().hex[:8]
        ctx.bot_data.setdefault("pending", {})[key] = p
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👂 LOR", callback_data=f"cat:{key}:lor"),
            InlineKeyboardButton("🦷 Stomatologiya", callback_data=f"cat:{key}:stom"),
        ]])
        await msg.reply_text(
            f"Dr. {name}, {fmt_day(d)}, {start}–{end}\nQaysi bo'lim?", reply_markup=kb
        )
        return
    reply, kb = process(ctx, p)
    await msg.reply_text(reply, reply_markup=kb)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    action, key = parts[0], parts[1]
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

    if action == "cat":
        p["cat"] = parts[2] if parts[2] in CATS else "lor"
        reply, kb = process(ctx, p)
        await q.edit_message_text(reply, reply_markup=kb)
        return

    con = db()
    if action == "upd":
        conflicts = find_conflicts(con, p["day"].isoformat(), p["cat"], p["start"], p["end"], p["op_id"])
        con.execute(
            "UPDATE ops SET start=?, finish=?, user_id=? WHERE id=?",
            (p["start"], p["end"], p["uid"], p["op_id"]),
        )
        con.commit()
        label, emoji = CATS[p["cat"]]
        reply = (
            f"✏️ O'zgartirildi #{p['op_id']}\n{emoji} {label}\n"
            f"Dr. {p['name']}, {fmt_day(p['day'])}, {p['start']}–{p['end']}"
        )
        if conflicts:
            reply += conflict_text(conflicts)
    else:
        reply = insert_op(con, p["name"], p["day"], p["start"], p["end"], p["uid"], p["cat"])
    con.close()
    await q.edit_message_text(reply)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


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
    cols = "doctor, day, start, finish, category"
    if ctx.args and ctx.args[0].isdigit():
        row = con.execute(
            f"DELETE FROM ops WHERE id=? AND user_id=? RETURNING {cols}",
            (int(ctx.args[0]), uid),
        ).fetchone()
    else:
        row = con.execute(
            f"DELETE FROM ops WHERE id = (SELECT id FROM ops WHERE user_id=? "
            f"ORDER BY id DESC LIMIT 1) RETURNING {cols}",
            (uid,),
        ).fetchone()
    con.commit()
    con.close()
    if not row:
        await update.message.reply_text("Bekor qilinadigan operatsiya topilmadi.")
        return
    doc, d, s, e, cat = row
    emoji = CATS.get(cat, ("", ""))[1]
    await update.message.reply_text(
        f"🗑 Bekor qilindi: {emoji} Dr. {doc}, {fmt_day(date.fromisoformat(d))}, {s}–{e}"
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    rows = con.execute(
        "SELECT id, doctor, day, start, finish, category FROM ops "
        "WHERE user_id=? AND day>=? ORDER BY day, start",
        (update.effective_user.id, today().isoformat()),
    ).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("Sizning kelgusi operatsiyalaringiz yo'q.")
        return
    lines = [
        f"#{i}  {date.fromisoformat(d).strftime('%d.%m')}  {s}–{e}  "
        f"{CATS.get(c, ('', ''))[1]} Dr. {doc}"
        for i, doc, d, s, e, c in rows
    ]
    await update.message.reply_text(
        "Sizning operatsiyalaringiz:\n" + "\n".join(lines) + "\n\nBittasini bekor qilish: /bekor RAQAM"
    )


async def cmd_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    con.execute(
        "INSERT INTO chats (chat_id) VALUES (?) ON CONFLICT DO NOTHING", (update.effective_chat.id,)
    )
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


# ------------------------------------------------------------------- SAYT ----

def ok_key(q) -> bool:
    return hmac.compare_digest(q.get("k", [""])[0].encode(), SITE_KEY.encode())


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", cache="no-store"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self.send(200, "ok", "text/plain")
        if u.path == "/logo.png":
            return self.send(200, LOGO_PNG, "image/png", "public, max-age=86400")
        if not ok_key(q):
            return self.send(403, "Havola noto'g'ri yoki eskirgan.", "text/plain; charset=utf-8")
        if u.path == "/":
            return self.send(200, PAGE)
        if u.path == "/api/ops":
            return self.api_list(q)
        self.send(404, "Topilmadi", "text/plain; charset=utf-8")

    def api_list(self, q):
        try:
            d = date.fromisoformat(q.get("d", [""])[0])
        except ValueError:
            d = today()
        c = q.get("c", ["lor"])[0]
        if c not in CATS:
            c = "lor"
        try:
            con = db()
            rows = con.execute(
                "SELECT id, doctor, op_type, day, start, finish, category, note "
                "FROM ops WHERE day=? AND category=? ORDER BY start",
                (d.isoformat(), c),
            ).fetchall()
            counts = dict(
                con.execute(
                    "SELECT category, COUNT(*) FROM ops WHERE day=? GROUP BY category",
                    (d.isoformat(),),
                ).fetchall()
            )
            docs = [
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT doctor FROM ops WHERE day>=? ORDER BY doctor",
                    ((today() - timedelta(days=120)).isoformat(),),
                ).fetchall()
            ]
            con.close()
        except Exception as e:
            print("Sayt xatosi:", e)
            return self.send_json(500, {"error": "Bazaga ulanib bo'lmadi"})
        ops = [
            {"id": r[0], "doctor": r[1], "op_type": r[2], "day": r[3],
             "start": r[4], "end": r[5], "category": r[6], "note": r[7]}
            for r in rows
        ]
        self.send_json(200, {"ops": ops, "counts": counts, "doctors": docs})

    # ---- POST
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not ok_key(q):
            return self.send_json(403, {"error": "Havola noto'g'ri yoki eskirgan."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20000:
            return self.send_json(413, {"error": "Juda katta so'rov"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json(400, {"error": "Noto'g'ri so'rov"})
        try:
            if u.path == "/api/save":
                return self.api_save(data)
            if u.path == "/api/delete":
                return self.api_delete(data)
        except Exception as e:
            print("Sayt xatosi:", e)
            return self.send_json(500, {"error": "Xatolik. Qayta urinib ko'ring."})
        self.send_json(404, {"error": "Topilmadi"})

    def api_save(self, data):
        doctor = norm_name(str(data.get("doctor", "")))
        if not doctor or len(doctor) > 60:
            return self.send_json(400, {"error": "Doktor ismini yozing"})
        op_type = str(data.get("op_type", "")).strip()[:80]
        note = str(data.get("note", "")).strip()[:200]
        cat = data.get("category")
        if cat not in CATS:
            return self.send_json(400, {"error": "Bo'limni tanlang"})
        try:
            day = date.fromisoformat(str(data.get("day", "")))
        except ValueError:
            return self.send_json(400, {"error": "Sanani tanlang"})
        st, en = norm_time(str(data.get("start", ""))), norm_time(str(data.get("end", "")))
        if not st or not en:
            return self.send_json(400, {"error": "Boshlanish va tugash vaqtini kiriting"})
        if en <= st:
            return self.send_json(400, {"error": "Tugash vaqti boshlanishdan keyin bo'lishi kerak"})
        try:
            oid = int(data.get("id") or 0)
        except (TypeError, ValueError):
            oid = 0

        con = db()
        conflicts = find_conflicts(con, day.isoformat(), cat, st, en, oid)
        if conflicts and not data.get("force"):
            con.close()
            return self.send_json(
                409, {"conflicts": [{"doctor": c[0], "start": c[1], "end": c[2]} for c in conflicts]}
            )
        if oid:
            row = con.execute(
                "UPDATE ops SET doctor=?, op_type=?, note=?, category=?, day=?, start=?, finish=? "
                "WHERE id=? RETURNING id",
                (doctor, op_type, note, cat, day.isoformat(), st, en, oid),
            ).fetchone()
            if not row:
                con.close()
                return self.send_json(404, {"error": "Operatsiya topilmadi (o'chirilgan bo'lishi mumkin)"})
        else:
            row = con.execute(
                "INSERT INTO ops (doctor, day, start, finish, user_id, category, op_type, note) "
                "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
                (doctor, day.isoformat(), st, en, 0, cat, op_type, note),
            ).fetchone()
        con.commit()
        con.close()
        self.send_json(200, {"ok": True, "id": row[0]})

    def api_delete(self, data):
        try:
            oid = int(data.get("id") or 0)
        except (TypeError, ValueError):
            oid = 0
        con = db()
        con.execute("DELETE FROM ops WHERE id=?", (oid,))
        con.commit()
        con.close()
        self.send_json(200, {"ok": True})


def start_site():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    init_db()
    start_site()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("bugun", cmd_today))
    app.add_handler(CommandHandler("ertaga", cmd_tomorrow))
    app.add_handler(CommandHandler("hafta", cmd_week))
    app.add_handler(CommandHandler("bekor", cmd_cancel))
    app.add_handler(CommandHandler("royxat", cmd_list))
    app.add_handler(CommandHandler("guruh", cmd_group))
    app.add_handler(CommandHandler("oper", add_op))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(upd|new|cat):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_op))
    app.job_queue.run_daily(morning, MORNING)
    app.run_polling()


# ------------------------------------------------- LOGO VA SAYT SAHIFASI ----

LOGO_PNG = base64.b64decode("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")

PAGE = r"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>EMU Clinic · Operatsiyalar</title>
<link rel="icon" href="/logo.png">
<script>
(function(){var t;try{t=localStorage.getItem('theme')}catch(e){}
if(!t)t=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
document.documentElement.dataset.theme=t;})();
</script>
<style>
:root{--bg:#f8f4f8;--card:#fff;--text:#241a23;--muted:#7a6b78;--line:#eadfe8;--brand:#6b1156;
--brand-2:#8a2a73;--soft:#f4e7f1;--off:rgba(107,17,86,.045);--danger:#c0262d;--shadow:0 1px 3px rgba(60,10,50,.14)}
:root[data-theme=dark]{--bg:#120d13;--card:#1c151d;--text:#f2e9f1;--muted:#a592a3;--line:#34283a;
--brand:#9b2f82;--brand-2:#c15aa8;--soft:#2a1a29;--off:rgba(255,255,255,.035);--danger:#ef6a70;--shadow:0 1px 3px rgba(0,0,0,.55)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
padding-bottom:env(safe-area-inset-bottom)}
button,input,textarea{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line);
padding-top:env(safe-area-inset-top);box-shadow:var(--shadow)}
.in{max-width:760px;margin:0 auto;padding:0 12px}
.bar{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0 6px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.logo{height:42px;width:auto;display:block}
:root[data-theme=dark] .logo{filter:brightness(0) invert(1) opacity(.92)}
.brand b{display:block;font-size:17px;color:var(--brand);letter-spacing:.2px;line-height:1.15}
:root[data-theme=dark] .brand b{color:var(--brand-2)}
.brand span{display:block;font-size:12px;color:var(--muted)}
.actions{display:flex;gap:8px;align-items:center}
.ic{width:40px;height:40px;border-radius:12px;border:1px solid var(--line);background:var(--card);
font-size:18px;cursor:pointer;display:grid;place-items:center}
.plus{width:44px;height:44px;border-radius:14px;border:0;background:var(--brand);color:#fff;font-size:28px;
line-height:1;cursor:pointer;box-shadow:0 2px 6px rgba(107,17,86,.4)}
.tabs{display:flex;gap:8px;padding:4px 0 8px}
.tab{flex:1;padding:10px 6px;border-radius:12px;border:1px solid var(--line);background:var(--card);
cursor:pointer;font-weight:600;font-size:15px;display:flex;align-items:center;justify-content:center;gap:6px}
.tab.on{background:var(--brand);color:#fff;border-color:var(--brand)}
.tab i{font-style:normal;font-size:12px;min-width:20px;padding:1px 6px;border-radius:10px;
background:var(--soft);color:var(--brand)}
:root[data-theme=dark] .tab i{color:var(--brand-2)}
:root .tab.on i,:root[data-theme=dark] .tab.on i{background:rgba(255,255,255,.25);color:#fff}
.tab i:empty{display:none}
.dates{display:flex;gap:8px;align-items:stretch;padding:0 0 10px}
.dates button{border:1px solid var(--line);background:var(--card);border-radius:12px;cursor:pointer;
padding:0 14px;font-size:20px}
.dates .today{font-size:14px;font-weight:600;color:var(--brand)}
:root[data-theme=dark] .dates .today{color:var(--brand-2)}
.dpick{position:relative;flex:1;border:1px solid var(--line);border-radius:12px;background:var(--card);
display:flex;flex-direction:column;justify-content:center;align-items:center;padding:6px 8px;cursor:pointer;min-width:0}
.dpick b{font-size:16px}
.dpick small{color:var(--muted);font-size:12px}
.dpick input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}
main{max-width:760px;margin:0 auto;padding:6px 12px 90px}
.hint{color:var(--muted);font-size:13px;text-align:center;margin:10px 0 0}
.cal{position:relative;margin:18px 0 0;background:var(--card);border:1px solid var(--line);border-radius:14px}
.hour{position:absolute;left:0;right:0;border-top:1px solid var(--line);cursor:pointer}
.hour.off{background:var(--off)}
.hour:first-child{border-top-color:transparent}
.hour span{position:absolute;left:8px;top:-9px;background:var(--card);padding:0 4px;font-size:12px;color:var(--muted)}
.hour:hover{background:var(--soft)}
.events{position:absolute;left:58px;right:8px;top:0;bottom:0;pointer-events:none}
.ev{position:absolute;pointer-events:auto;cursor:pointer;border-radius:9px;padding:5px 8px;color:#fff;
font-size:13px;line-height:1.25;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.3);
border-left:4px solid rgba(255,255,255,.55);z-index:2}
.ev b{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ev .ty,.ev .tm{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.95}
.ev .tm{font-size:12px;opacity:.85}
.ev.sm{display:flex;align-items:center;gap:8px;padding:2px 8px}
.ev.sm b{flex:0 1 auto}
.ev.sm .ty{flex:1 1 auto;min-width:0}
.ev.sm .tm{flex:0 0 auto}
.now{position:absolute;left:52px;right:0;border-top:2px solid #ef4444;z-index:3;pointer-events:none}
.now:before{content:"";position:absolute;left:-5px;top:-6px;width:10px;height:10px;border-radius:50%;background:#ef4444}
.shade{position:fixed;inset:0;background:rgba(10,4,10,.55);z-index:50;display:none;align-items:flex-end;justify-content:center}
.shade.open{display:flex}
@media(min-width:620px){.shade{align-items:center}}
.sheet{width:100%;max-width:480px;max-height:94vh;overflow:auto;background:var(--card);border-radius:20px 20px 0 0;
padding:18px 16px calc(18px + env(safe-area-inset-bottom));box-shadow:0 -4px 24px rgba(0,0,0,.35)}
@media(min-width:620px){.sheet{border-radius:20px}}
.sheet h2{margin:0 0 12px;font-size:18px;color:var(--brand)}
:root[data-theme=dark] .sheet h2{color:var(--brand-2)}
label.f{display:block;margin:0 0 12px;font-size:13px;color:var(--muted);font-weight:600}
label.f input,label.f textarea{display:block;width:100%;margin-top:5px;padding:11px 12px;font-size:16px;
border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);outline:none}
label.f input:focus,label.f textarea:focus{border-color:var(--brand)}
label.f textarea{resize:vertical;min-height:62px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.seg{display:flex;gap:8px;margin:0 0 12px}
.seg label{flex:1;position:relative}
.seg input{position:absolute;opacity:0}
.seg span{display:block;text-align:center;padding:10px 6px;border-radius:12px;border:1px solid var(--line);
background:var(--bg);font-weight:600;cursor:pointer}
.seg input:checked+span{background:var(--brand);color:#fff;border-color:var(--brand)}
.err{color:var(--danger);font-size:14px;margin:0 0 10px;min-height:0}
.btns{display:flex;gap:8px;margin-top:6px}
.btns button{flex:1;padding:13px;border-radius:12px;border:1px solid var(--line);background:var(--card);
cursor:pointer;font-weight:600;font-size:16px}
.btns .ok{background:var(--brand);border-color:var(--brand);color:#fff;flex:2}
.btns .del{color:var(--danger);flex:0 0 auto;padding:13px 16px}
.btns button:disabled{opacity:.6}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));transform:translate(-50%,20px);
background:#241a23;color:#fff;padding:11px 16px;border-radius:12px;font-size:14px;opacity:0;
transition:.25s;z-index:60;pointer-events:none;max-width:90vw;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
</style>
</head>
<body>
<header>
  <div class="in">
    <div class="bar">
      <div class="brand">
        <img class="logo" src="/logo.png" alt="EMU">
        <div><b>EMU Clinic</b><span>Operatsiyalar jadvali</span></div>
      </div>
      <div class="actions">
        <button class="ic" id="theme" title="Kun / tun rejimi" aria-label="Kun yoki tun rejimi">🌙</button>
        <button class="plus" id="add" title="Operatsiya qo'shish" aria-label="Operatsiya qo'shish">+</button>
      </div>
    </div>
    <div class="tabs">
      <button class="tab" data-c="lor">👂 LOR <i id="n-lor"></i></button>
      <button class="tab" data-c="stom">🦷 Stomatologiya <i id="n-stom"></i></button>
    </div>
    <div class="dates">
      <button id="prev" aria-label="Oldingi kun">‹</button>
      <label class="dpick"><b id="dtitle"></b><small id="dwd"></small><input type="date" id="dpick"></label>
      <button id="next" aria-label="Keyingi kun">›</button>
      <button class="today" id="today">Bugun</button>
    </div>
  </div>
</header>

<main>
  <div class="cal" id="cal"></div>
  <p class="hint">Bo'sh soatga bosing, operatsiyani band qilasiz. Band vaqtga bossangiz, o'zgartirasiz.</p>
</main>

<div class="shade" id="shade">
  <form class="sheet" id="form" autocomplete="off">
    <h2 id="ftitle">Yangi operatsiya</h2>
    <label class="f">Kim qiladi? (doktor)
      <input id="f-doctor" list="docs" placeholder="Masalan: Aliyev" maxlength="60" required>
      <datalist id="docs"></datalist>
    </label>
    <label class="f">Qanday operatsiya?
      <input id="f-type" placeholder="Masalan: Septoplastika" maxlength="80">
    </label>
    <div class="seg" id="f-cat">
      <label><input type="radio" name="fcat" value="lor"><span>👂 LOR</span></label>
      <label><input type="radio" name="fcat" value="stom"><span>🦷 Stomatologiya</span></label>
    </div>
    <label class="f">Sana
      <input type="date" id="f-day" required>
    </label>
    <div class="row">
      <label class="f">Boshlanishi
        <input type="time" id="f-start" step="300" required>
      </label>
      <label class="f">Taxminiy tugashi
        <input type="time" id="f-end" step="300" required>
      </label>
    </div>
    <label class="f">Izoh (ixtiyoriy)
      <textarea id="f-note" maxlength="200" placeholder="Masalan: sedatsiya kerak. Bemor ismini yozmang."></textarea>
    </label>
    <div class="err" id="ferr"></div>
    <div class="btns">
      <button type="button" class="del" id="fdel">🗑</button>
      <button type="button" id="fcancel">Yopish</button>
      <button type="submit" class="ok" id="fsave">Saqlash</button>
    </div>
  </form>
</div>
<div class="toast" id="toast"></div>

<script>
(function(){
const $ = s => document.querySelector(s);
const P = new URLSearchParams(location.search);
const KEY = P.get('k') || '';
const PX = 56;
const CATS = {lor: 'LOR', stom: 'Stomatologiya'};
const WD = ['Yakshanba','Dushanba','Seshanba','Chorshanba','Payshanba','Juma','Shanba'];
const COLORS = ['#6b1156','#0f766e','#1d4ed8','#b45309','#be185d','#4d7c0f','#7c3aed','#0e7490'];
const TZ = {timeZone: 'Asia/Tashkent'};
const pad = n => String(n).padStart(2, '0');
const todayStr = () => new Date().toLocaleDateString('sv-SE', TZ);
const nowMin = () => {
  const p = new Date().toLocaleTimeString('en-GB', {...TZ, hour: '2-digit', minute: '2-digit', hour12: false}).split(':');
  return (+p[0] % 24) * 60 + (+p[1]);
};
const toMin = t => { const p = t.split(':'); return (+p[0]) * 60 + (+p[1]); };
const addDays = (s, n) => { const p = s.split('-').map(Number); return new Date(Date.UTC(p[0], p[1]-1, p[2]+n)).toISOString().slice(0,10); };
const wdOf = s => WD[new Date(s + 'T12:00:00Z').getUTCDay()];
const dmy = s => s.split('-').reverse().join('.');
function el(tag, cls, text){ const e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e; }
function hashColor(name){ let h = 0; const s = name.toLowerCase(); for(let i=0;i<s.length;i++) h = (h*31 + s.charCodeAt(i)) >>> 0; return COLORS[h % COLORS.length]; }

let cat = P.get('c') === 'stom' ? 'stom' : 'lor';
let day = /^\d{4}-\d{2}-\d{2}$/.test(P.get('d') || '') ? P.get('d') : todayStr();
let ops = [], editing = null, busy = false;

function toast(t){ const e = $('#toast'); e.textContent = t; e.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => e.classList.remove('show'), 2600); }

/* ---------- kun/tun ---------- */
const root = document.documentElement;
function setTheme(t){ root.dataset.theme = t; try{ localStorage.setItem('theme', t); }catch(e){} $('#theme').textContent = t === 'dark' ? '☀️' : '🌙'; }
$('#theme').onclick = () => setTheme(root.dataset.theme === 'dark' ? 'light' : 'dark');
setTheme(root.dataset.theme);

/* ---------- joylashuv ---------- */
function layout(list){
  const sorted = [...list].sort((a,b) => toMin(a.start) - toMin(b.start) || toMin(a.end) - toMin(b.end));
  const out = []; let cluster = [], lanes = [], cEnd = -1;
  const flush = () => cluster.forEach(([o, l]) => out.push([o, l, lanes.length]));
  for(const o of sorted){
    const s = toMin(o.start), e = toMin(o.end);
    if(cluster.length && s >= cEnd){ flush(); cluster = []; lanes = []; cEnd = -1; }
    let lane = lanes.findIndex(x => x <= s);
    if(lane === -1){ lane = lanes.length; lanes.push(e); } else lanes[lane] = e;
    cluster.push([o, lane]); cEnd = Math.max(cEnd, e);
  }
  if(cluster.length) flush();
  return out;
}

function render(){
  const cal = $('#cal'); cal.innerHTML = ''; cal.style.height = (24 * PX) + 'px';
  for(let h = 0; h < 24; h++){
    const r = el('div', 'hour' + ((h < 7 || h >= 21) ? ' off' : '')); r.style.top = (h * PX) + 'px'; r.style.height = PX + 'px';
    r.appendChild(el('span', '', pad(h) + ':00')); cal.appendChild(r);
  }
  const box = el('div', 'events'); cal.appendChild(box);
  for(const [o, lane, n] of layout(ops)){
    const h = Math.max((toMin(o.end) - toMin(o.start)) * PX / 60, 26);
    const e = el('div', 'ev' + (h < 48 ? ' sm' : '')); e.dataset.id = o.id;
    e.style.top = (toMin(o.start) * PX / 60) + 'px'; e.style.height = h + 'px';
    e.style.left = `calc(${lane} * 100% / ${n})`; e.style.width = `calc(100% / ${n} - 4px)`;
    e.style.background = hashColor(o.doctor);
    e.appendChild(el('b', '', (n >= 3 ? '' : 'Dr. ') + o.doctor));
    if(o.op_type) e.appendChild(el('span', 'ty', o.op_type));
    e.appendChild(el('span', 'tm', o.start + '–' + o.end));
    box.appendChild(e);
  }
  if(day === todayStr()){
    const m = nowMin(); const l = el('div', 'now'); l.style.top = (m * PX / 60) + 'px'; cal.appendChild(l);
  }
  $('#dtitle').textContent = dmy(day); $('#dwd').textContent = wdOf(day); $('#dpick').value = day;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.c === cat));
}

$('#cal').addEventListener('click', e => {
  const ev = e.target.closest('.ev');
  if(ev){ const o = ops.find(x => String(x.id) === ev.dataset.id); if(o) openForm(o); return; }
  const y = e.clientY - $('#cal').getBoundingClientRect().top;
  const h = Math.max(0, Math.min(23, Math.floor(y / PX)));
  openForm(null, {start: pad(h) + ':00', end: h < 23 ? pad(h + 1) + ':00' : '23:59'});
});

function scrollTarget(){
  let h = 7;
  if(ops.length) h = Math.floor(Math.min(...ops.map(o => toMin(o.start))) / 60);
  else if(day === todayStr()) h = Math.floor(nowMin() / 60);
  const top = $('#cal').getBoundingClientRect().top + window.scrollY + Math.max(0, h - 1) * PX - $('header').offsetHeight - 8;
  window.scrollTo({top: Math.max(0, top)});
}

function syncUrl(){
  const u = new URL(location.href); u.searchParams.set('k', KEY); u.searchParams.set('c', cat); u.searchParams.set('d', day);
  history.replaceState(null, '', u);
}

async function load(scroll){
  try{
    const r = await fetch(`/api/ops?k=${encodeURIComponent(KEY)}&d=${day}&c=${cat}`);
    if(r.status === 403){ $('#cal').innerHTML = ''; $('#cal').appendChild(el('p', 'hint', "Havola eskirgan. Guruhdagi eng yangi havolani oching.")); return; }
    const j = await r.json();
    ops = j.ops || [];
    $('#n-lor').textContent = j.counts && j.counts.lor ? j.counts.lor : '';
    $('#n-stom').textContent = j.counts && j.counts.stom ? j.counts.stom : '';
    const dl = $('#docs'); dl.innerHTML = '';
    (j.doctors || []).forEach(n => { const o = document.createElement('option'); o.value = n; dl.appendChild(o); });
    render(); syncUrl(); if(scroll) scrollTarget();
  }catch(e){ toast('Aloqa xatosi. Internetni tekshiring.'); }
}

/* ---------- boshqaruv ---------- */
document.querySelectorAll('.tab').forEach(t => t.onclick = () => { cat = t.dataset.c; load(true); });
$('#prev').onclick = () => { day = addDays(day, -1); load(true); };
$('#next').onclick = () => { day = addDays(day, 1); load(true); };
$('#today').onclick = () => { day = todayStr(); load(true); };
$('#dpick').onchange = e => { if(e.target.value){ day = e.target.value; load(true); } };
$('#dpick').addEventListener('click', e => { try{ e.target.showPicker && e.target.showPicker(); }catch(_){} });

/* ---------- forma ---------- */
function openForm(o, preset){
  editing = o;
  $('#ftitle').textContent = o ? "Operatsiyani o'zgartirish" : 'Yangi operatsiya';
  $('#f-doctor').value = o ? o.doctor : '';
  $('#f-type').value = o ? o.op_type : '';
  $('#f-day').value = o ? o.day : day;
  let st = o ? o.start : (preset && preset.start) || '', en = o ? o.end : (preset && preset.end) || '';
  if(!st){ const h = day === todayStr() ? Math.min(22, Math.floor(nowMin() / 60) + 1) : 9; st = pad(h) + ':00'; en = pad(h + 1) + ':00'; }
  $('#f-start').value = st; $('#f-end').value = en;
  $('#f-note').value = o ? o.note : '';
  const c = o ? o.category : cat;
  document.querySelectorAll('input[name=fcat]').forEach(r => r.checked = r.value === c);
  $('#fdel').style.display = o ? '' : 'none';
  $('#ferr').textContent = '';
  $('#shade').classList.add('open');
  if(!o) setTimeout(() => $('#f-doctor').focus(), 50);
}
function closeForm(){ $('#shade').classList.remove('open'); editing = null; }
$('#add').onclick = () => openForm(null, {});
$('#fcancel').onclick = closeForm;
$('#shade').addEventListener('mousedown', e => { if(e.target.id === 'shade') closeForm(); });
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeForm(); });
$('#f-start').addEventListener('change', () => {
  const s = $('#f-start').value, e = $('#f-end').value;
  if(s && (!e || toMin(e) <= toMin(s))){ const m = Math.min(toMin(s) + 60, 23 * 60 + 59); $('#f-end').value = pad(Math.floor(m / 60)) + ':' + pad(m % 60); }
});

async function save(force){
  if(busy) return;
  const body = {
    id: editing ? editing.id : 0,
    doctor: $('#f-doctor').value.trim(), op_type: $('#f-type').value.trim(),
    day: $('#f-day').value, start: $('#f-start').value, end: $('#f-end').value,
    note: $('#f-note').value.trim(),
    category: (document.querySelector('input[name=fcat]:checked') || {}).value, force: !!force
  };
  const err = m => { $('#ferr').textContent = m; };
  if(!body.doctor) return err('Doktor ismini yozing');
  if(!body.day) return err('Sanani tanlang');
  if(!body.start || !body.end) return err('Boshlanish va tugash vaqtini kiriting');
  if(toMin(body.end) <= toMin(body.start)) return err("Tugash vaqti boshlanishdan keyin bo'lishi kerak");
  busy = true; $('#fsave').disabled = true; $('#ferr').textContent = '';
  try{
    const r = await fetch(`/api/save?k=${encodeURIComponent(KEY)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if(r.status === 409){
      busy = false; $('#fsave').disabled = false;
      const list = j.conflicts.map(c => `• Dr. ${c.doctor} ${c.start}–${c.end}`).join('\n');
      if(confirm(`Bu vaqtda ${CATS[body.category]} xonasi band:\n${list}\n\nBaribir saqlaymizmi?`)) return await save(true);
      return;
    }
    if(!r.ok){ err(j.error || 'Xatolik. Qayta urinib ko\'ring.'); return; }
    const wasEdit = !!editing; closeForm(); cat = body.category; day = body.day; toast(wasEdit ? "✅ O'zgartirildi" : '✅ Saqlandi');
    await load(true);
  }catch(e){ err('Aloqa xatosi. Internetni tekshiring.'); }
  finally{ busy = false; $('#fsave').disabled = false; }
}
$('#form').addEventListener('submit', e => { e.preventDefault(); save(false); });

$('#fdel').onclick = async () => {
  if(!editing || !confirm("Bu operatsiyani o'chiramizmi?")) return;
  try{
    await fetch(`/api/delete?k=${encodeURIComponent(KEY)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: editing.id})});
    closeForm(); toast("🗑 O'chirildi"); load(false);
  }catch(e){ $('#ferr').textContent = 'Aloqa xatosi.'; }
};

load(true);
setInterval(() => { if(!$('#shade').classList.contains('open')) load(false); }, 60000);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
