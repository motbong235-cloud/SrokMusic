#!/usr/bin/env python3
"""
SrokMusic — Bot ស្វែងរកចម្រៀង + Admin Panel
ភាសាខ្មែរ 100% · iTunes API · Preview 30s
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("srokmusic")

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in (os.environ.get("ADMIN_IDS") or "").split(",")
    if x.strip().isdigit()
}
DATA_DIR = Path(os.environ.get("DATA_DIR") or "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "db.json"
_lock = threading.RLock()

JAMENDO_ID = (os.environ.get("JAMENDO_CLIENT_ID") or "").strip()
JAMENDO_URL = "https://api.jamendo.com/v3.0/tracks/"
ITUNES_URL = "https://itunes.apple.com/search"
UA = "SrokMusic/2.0"
_PREVIEW_CACHE: dict[str, dict[str, str]] = {}
_TRACK_CACHE: dict[str, dict] = {}

# pending broadcast: admin_id -> True
_broadcast_wait: set[int] = set()


def _default_db() -> dict:
    return {
        "users": {},          # uid -> {name, username, joined, searches}
        "searches": 0,
        "previews": 0,
        "blocked": [],
        "maintenance": False,
        "recent": [],         # last 30 searches {q, uid, at}
        "broadcasts": 0,
    }


def db_read() -> dict:
    with _lock:
        if not DB_FILE.exists():
            return _default_db()
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            return _default_db()


def db_write(d: dict) -> None:
    with _lock:
        tmp = DB_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DB_FILE)


def is_admin(uid: int | None) -> bool:
    return bool(uid and uid in ADMIN_IDS)


def track_user(user) -> None:
    if not user:
        return
    d = db_read()
    u = d.setdefault("users", {})
    key = str(user.id)
    row = u.get(key) or {
        "id": user.id,
        "joined": datetime.now(timezone.utc).isoformat(),
        "searches": 0,
    }
    row["name"] = (user.full_name or "")[:80]
    row["username"] = (user.username or "")[:64]
    row["last_seen"] = datetime.now(timezone.utc).isoformat()
    u[key] = row
    d["users"] = u
    db_write(d)


def track_search(uid: int, query: str) -> None:
    d = db_read()
    d["searches"] = int(d.get("searches") or 0) + 1
    users = d.setdefault("users", {})
    key = str(uid)
    if key in users:
        users[key]["searches"] = int(users[key].get("searches") or 0) + 1
    recent = d.setdefault("recent", [])
    recent.insert(0, {
        "q": query[:80],
        "uid": uid,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    d["recent"] = recent[:30]
    db_write(d)


def is_blocked(uid: int) -> bool:
    d = db_read()
    return uid in (d.get("blocked") or [])


def itunes_search(term: str, limit: int = 6) -> list[dict[str, Any]]:
    q = urllib.parse.urlencode({
        "term": term, "media": "music", "entity": "song",
        "limit": limit, "country": "US",
    })
    req = urllib.request.Request(f"{ITUNES_URL}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    return list(data.get("results") or [])


def jamendo_search(term: str, limit: int = 3) -> list[dict[str, Any]]:
    """បទពេញស្របច្បាប់ (Creative Commons) ពី Jamendo។"""
    if not JAMENDO_ID:
        return []
    q = urllib.parse.urlencode({
        "client_id": JAMENDO_ID, "format": "json", "limit": limit,
        "search": term, "audioformat": "mp32", "order": "popularity_total",
    })
    req = urllib.request.Request(f"{JAMENDO_URL}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    out = []
    for r in data.get("results") or []:
        if not r.get("audio"):
            continue
        out.append({
            "trackId": f"jm{r.get('id')}",
            "trackName": r.get("name"),
            "artistName": r.get("artist_name"),
            "collectionName": r.get("album_name"),
            "trackTimeMillis": int(r.get("duration") or 0) * 1000,
            "primaryGenreName": "Jamendo",
            "releaseDate": r.get("releasedate") or "",
            "fullUrl": r.get("audio"),
            "trackViewUrl": r.get("shareurl") or "",
            "artworkUrl100": r.get("image") or "",
        })
    return out


def youtube_url(title: str, artist: str = "") -> str:
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote(
        f"{title} {artist}".strip()
    )


def format_ms(ms: int | None) -> str:
    if not ms:
        return "—"
    s = int(ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def cache_preview(track: dict) -> str:
    tid = str(track.get("trackId") or abs(hash(track.get("trackName") or "")) % 10**10)
    _PREVIEW_CACHE[tid] = {
        "url": track.get("previewUrl") or "",
        "title": track.get("trackName") or "preview",
        "artist": track.get("artistName") or "",
    }
    if len(_PREVIEW_CACHE) > 200:
        for k in list(_PREVIEW_CACHE.keys())[:50]:
            _PREVIEW_CACHE.pop(k, None)
    return tid


def build_caption(track: dict, idx: int) -> str:
    title = track.get("trackName") or "—"
    artist = track.get("artistName") or "—"
    album = track.get("collectionName") or "—"
    dur = format_ms(track.get("trackTimeMillis"))
    genre = track.get("primaryGenreName") or "—"
    year = (track.get("releaseDate") or "")[:4]
    lines = [
        f"<b>{idx}. {title}</b>",
        f"🎤 សិល្បករ៖ {artist}",
        f"💿 អាល់ប៊ុម៖ {album}" + (f" ({year})" if year else ""),
        f"⏱ រយៈពេល៖ {dur} · 🏷 {genre}",
    ]
    if track.get("fullUrl"):
        lines.append("🆓 <b>បទពេញ</b> · Creative Commons (Jamendo)")
    elif track.get("previewUrl"):
        lines.append("🎧 មាន <b>Preview 30 វិនាទី</b> — ចុចប៊ូតុងទាញ")
    else:
        lines.append("⚠️ បទនេះគ្មាន preview ផ្លូវការ")
    return "\n".join(lines)


def build_keyboard(track: dict) -> InlineKeyboardMarkup:
    title = track.get("trackName") or ""
    artist = track.get("artistName") or ""
    preview = track.get("previewUrl") or ""
    apple = track.get("trackViewUrl") or ""
    yt = youtube_url(title, artist)
    rows: list[list[InlineKeyboardButton]] = []
    if preview:
        key = cache_preview(track)
        rows.append([InlineKeyboardButton("⬇️ ទាញ Preview (MP3/M4A)", callback_data=f"dl:{key}")])
    row = []
    if apple:
        row.append(InlineKeyboardButton("🍎 Apple Music", url=apple))
    row.append(InlineKeyboardButton("▶️ YouTube", url=yt))
    rows.append(row)
    return InlineKeyboardMarkup(rows)


def track_key(track: dict) -> str:
    return str(track.get("trackId") or abs(hash(track.get("trackName") or "")) % 10**10)


def remember_tracks(results: list[dict]) -> None:
    for t in results:
        _TRACK_CACHE[track_key(t)] = t
    if len(_TRACK_CACHE) > 400:
        for k in list(_TRACK_CACHE.keys())[:100]:
            _TRACK_CACHE.pop(k, None)


def build_list_text(results: list[dict], query: str) -> str:
    lines = [f"✅ រកឃើញ <b>{len(results)}</b> បទ · <i>{query}</i>\n"]
    for i, t in enumerate(results, 1):
        lines.append(
            f"<b>{i}.</b> {'🆓 ' if t.get('fullUrl') else ''}{t.get('trackName') or '—'} — {t.get('artistName') or '—'} "
            f"· ⏱ {format_ms(t.get('trackTimeMillis'))}"
        )
    lines.append("\n🆓 = បទពេញ\n👇 ចុចលេខបទដែលចង់បាន")
    return "\n".join(lines)


def build_pick_keyboard(results: list[dict]) -> InlineKeyboardMarkup:
    btns = [
        InlineKeyboardButton(f"{i}", callback_data=f"pk:{track_key(t)}")
        for i, t in enumerate(results, 1)
    ]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return InlineKeyboardMarkup(rows)


def link_keyboard(track: dict) -> InlineKeyboardMarkup:
    row = []
    if track.get("trackViewUrl"):
        label = "🎼 Jamendo" if track.get("fullUrl") else "🍎 Apple Music (បទពេញ)"
        row.append(InlineKeyboardButton(label, url=track["trackViewUrl"]))
    row.append(InlineKeyboardButton("▶️ YouTube", url=youtube_url(
        track.get("trackName") or "", track.get("artistName") or "")))
    return InlineKeyboardMarkup([row])


def admin_keyboard() -> InlineKeyboardMarkup:
    d = db_read()
    maint = "🟢 បើក Bot" if d.get("maintenance") else "🔴 Maintenance"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 ស្ថិតិ", callback_data="adm:stats"),
            InlineKeyboardButton("👥 អ្នកប្រើ", callback_data="adm:users"),
        ],
        [
            InlineKeyboardButton("🔎 ស្វែងថ្មីៗ", callback_data="adm:recent"),
            InlineKeyboardButton("📢 Broadcast", callback_data="adm:bc"),
        ],
        [
            InlineKeyboardButton(maint, callback_data="adm:maint"),
            InlineKeyboardButton("🚫 Block list", callback_data="adm:blocks"),
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm:home")],
    ])


def stats_text() -> str:
    d = db_read()
    n_users = len(d.get("users") or {})
    return (
        "📊 <b>ស្ថិតិ SrokMusic</b>\n\n"
        f"👥 អ្នកប្រើ៖ <b>{n_users}</b>\n"
        f"🔎 ស្វែងរកសរុប៖ <b>{d.get('searches') or 0}</b>\n"
        f"⬇️ Preview៖ <b>{d.get('previews') or 0}</b>\n"
        f"📢 Broadcast៖ <b>{d.get('broadcasts') or 0}</b>\n"
        f"🚫 Block៖ <b>{len(d.get('blocked') or [])}</b>\n"
        f"🛠 Maintenance៖ <b>{'ON' if d.get('maintenance') else 'OFF'}</b>\n"
    )


# ── handlers ──

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    track_user(user)
    if user and is_blocked(user.id):
        await update.effective_message.reply_text("⛔ គណនីនេះត្រូវបានបិទ។")
        return
    text = (
        "🎵 <b>សូមស្វាគមន៍មកកាន់ SrokMusic</b>\n\n"
        "ស្វែងរក និងស្តាប់ចម្រៀងដែលអ្នកចូលចិត្ត យ៉ាងងាយស្រួល។\n\n"
        "<b>របៀបប្រើ</b>\n"
        "1️⃣ វាយ <b>ឈ្មោះបទ</b> ឬ <b>ឈ្មោះសិល្បករ</b>\n"
        "2️⃣ ចុច <b>លេខបទ</b> ដែលអ្នកចង់បាន\n"
        "3️⃣ Bot នឹងផ្ញើបទនោះជូនអ្នកភ្លាមៗ 🎧\n\n"
        "<b>ឧទាហរណ៍</b>\n"
        "• <code>Shape of You</code>\n"
        "• <code>Perfect Ed Sheeran</code>\n\n"
        "🍎 ស្តាប់បទពេញតាម Apple Music · ▶️ YouTube\n\n"
        "/help — ជំនួយ"
    )
    if user and is_admin(user.id):
        text += "\n\n🔐 Admin: /admin"
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "<b>របៀបប្រើ SrokMusic</b>\n"
        "1. វាយឈ្មោះបទ\n"
        "2. ជ្រើសលទ្ធផល\n"
        "3. ចុច ទាញ Preview ឬ YouTube\n\n"
        "/start — ម៉ឺនុយ",
        parse_mode="HTML",
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("⛔ សម្រាប់ Admin តែប៉ុណ្ណោះ។")
        return
    await update.effective_message.reply_text(
        "🔐 <b>SrokMusic Admin Panel</b>\n\nជ្រើសមុខងារ៖",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("ប្រើ៖ /block <user_id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id ត្រូវជាលេខ")
        return
    d = db_read()
    blocked = list(d.get("blocked") or [])
    if tid not in blocked:
        blocked.append(tid)
    d["blocked"] = blocked
    db_write(d)
    await update.effective_message.reply_text(f"🚫 Blocked: <code>{tid}</code>", parse_mode="HTML")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("ប្រើ៖ /unblock <user_id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id ត្រូវជាលេខ")
        return
    d = db_read()
    blocked = [x for x in (d.get("blocked") or []) if x != tid]
    d["blocked"] = blocked
    db_write(d)
    await update.effective_message.reply_text(f"✅ Unblocked: <code>{tid}</code>", parse_mode="HTML")


async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("adm:"):
        return
    user = update.effective_user
    if not user or not is_admin(user.id):
        await q.answer("Admin only", show_alert=True)
        return
    await q.answer()
    action = q.data.split(":", 1)[1]
    d = db_read()

    if action == "home":
        await q.edit_message_text(
            "🔐 <b>SrokMusic Admin Panel</b>\n\nជ្រើសមុខងារ៖",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
    elif action == "stats":
        await q.edit_message_text(
            stats_text(), parse_mode="HTML", reply_markup=admin_keyboard()
        )
    elif action == "users":
        users = list((d.get("users") or {}).values())
        users.sort(key=lambda x: int(x.get("searches") or 0), reverse=True)
        lines = ["👥 <b>អ្នកប្រើ (Top 15)</b>\n"]
        for u in users[:15]:
            un = u.get("username")
            name = u.get("name") or "—"
            tag = f"@{un}" if un else name
            lines.append(
                f"• <code>{u.get('id')}</code> {tag} · 🔎 {u.get('searches') or 0}"
            )
        lines.append(f"\nសរុប៖ {len(users)} នាក់")
        lines.append("\nBlock៖ /block <id> · /unblock <id>")
        await q.edit_message_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=admin_keyboard()
        )
    elif action == "recent":
        recent = d.get("recent") or []
        lines = ["🔎 <b>ស្វែងថ្មីៗ</b>\n"]
        if not recent:
            lines.append("មិនទាន់មាន")
        for r in recent[:15]:
            lines.append(f"• <code>{r.get('q')}</code> · uid {r.get('uid')}")
        await q.edit_message_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=admin_keyboard()
        )
    elif action == "bc":
        _broadcast_wait.add(user.id)
        await q.edit_message_text(
            "📢 <b>Broadcast</b>\n\n"
            "ផ្ញើសារមកឥឡូវនេះ (text) ដើម្បីផ្ញើទៅអ្នកប្រើទាំងអស់។\n"
            "បោះបង់៖ /admin",
            parse_mode="HTML",
        )
    elif action == "maint":
        d["maintenance"] = not d.get("maintenance")
        db_write(d)
        await q.edit_message_text(
            stats_text() + "\n✅ បានប្តូរ Maintenance",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
    elif action == "blocks":
        blocked = d.get("blocked") or []
        text = "🚫 <b>Block list</b>\n\n"
        if not blocked:
            text += "គ្មាន"
        else:
            text += "\n".join(f"• <code>{x}</code>" for x in blocked)
        text += "\n\n/block <id> · /unblock <id>"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=admin_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.text or not user:
        return
    query = msg.text.strip()
    if not query or query.startswith("/"):
        return

    track_user(user)

    # broadcast mode
    if is_admin(user.id) and user.id in _broadcast_wait:
        _broadcast_wait.discard(user.id)
        d = db_read()
        users = d.get("users") or {}
        ok = fail = 0
        status = await msg.reply_text(f"📢 កំពុងផ្ញើទៅ {len(users)} នាក់…")
        for uid_s in list(users.keys()):
            try:
                await context.bot.send_message(chat_id=int(uid_s), text=query, parse_mode="HTML")
                ok += 1
                time.sleep(0.05)
            except Exception:
                fail += 1
        d["broadcasts"] = int(d.get("broadcasts") or 0) + 1
        db_write(d)
        await status.edit_text(f"✅ Broadcast រួច · ជោគជ័យ {ok} · បរាជ័យ {fail}")
        return

    if is_blocked(user.id):
        await msg.reply_text("⛔ គណនីនេះត្រូវបានបិទ។")
        return

    d = db_read()
    if d.get("maintenance") and not is_admin(user.id):
        await msg.reply_text("🛠 Bot កំពុងថែទាំ។ សូមមកវិញពេលក្រោយ។")
        return

    if len(query) > 120:
        await msg.reply_text("សូមសរសេរខ្លីជាងនេះ (អតិបរមា 120 តួ)។")
        return

    status = await msg.reply_text(f"🔎 កំពុងស្វែងរក៖ <b>{query}</b> …", parse_mode="HTML")
    try:
        try:
            full = jamendo_search(query, limit=3)
        except Exception:
            log.exception("jamendo")
            full = []
        results = full + itunes_search(query, limit=6)
    except Exception as e:
        log.exception("search")
        await status.edit_text(f"❌ ស្វែងរកមិនបាន។ សាកម្តងទៀត។")
        return

    track_search(user.id, query)

    if not results:
        await status.edit_text(
            f"😕 មិនឃើញបទ៖ <b>{query}</b>\nសាកបន្ថែមឈ្មោះសិល្បករ។",
            parse_mode="HTML",
        )
        return

    remember_tracks(results)
    await status.edit_text(
        build_list_text(results, query),
        parse_mode="HTML",
        reply_markup=build_pick_keyboard(results),
    )


async def send_track(message, track: dict) -> None:
    """ផ្ញើបទដែលបានជ្រើស (cover + audio preview)។"""
    title = re.sub(r'[\\/:*?"<>|]', "", track.get("trackName") or "preview")[:80]
    artist = track.get("artistName") or ""
    preview = track.get("fullUrl") or track.get("previewUrl") or ""
    caption = build_caption(track, 1).replace("<b>1. ", "<b>")
    kb = link_keyboard(track)

    if not preview:
        await message.reply_text(
            caption + "\n\n⚠️ បទនេះគ្មាន preview។ សូមប្រើ Apple Music / YouTube។",
            parse_mode="HTML", reply_markup=kb,
        )
        return

    req = urllib.request.Request(preview, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    if len(data) < 1000:
        await message.reply_text("❌ ឯកសារមិនត្រឹមត្រូវ។")
        return
    ext = "mp3" if track.get("fullUrl") else "m4a"
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        with open(path, "rb") as f:
            await message.reply_audio(
                audio=f,
                filename=f"{title}.{ext}",
                title=title,
                performer=artist,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb,
            )
        d = db_read()
        d["previews"] = int(d.get("previews") or 0) + 1
        db_write(d)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("pk:"):
        return
    user = update.effective_user
    if user and is_blocked(user.id):
        await q.answer("Blocked", show_alert=True)
        return
    track = _TRACK_CACHE.get(q.data[3:])
    if not track:
        await q.answer("ផុតកំណត់ — ស្វែងម្តងទៀត", show_alert=True)
        return
    await q.answer("កំពុងផ្ញើបទ…")
    try:
        await send_track(q.message, track)
    except Exception as e:
        log.exception("pick")
        await q.message.reply_text(f"❌ ផ្ញើមិនបាន៖ {e}")


async def on_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("dl:"):
        return
    user = update.effective_user
    if user and is_blocked(user.id):
        await q.answer("Blocked", show_alert=True)
        return
    await q.answer("កំពុងទាញ preview…")
    key = q.data[3:]
    info = _PREVIEW_CACHE.get(key)
    if not info or not info.get("url"):
        await q.message.reply_text("⚠️ Preview ផុតកំណត់។ សូមស្វែងម្តងទៀត។")
        return
    url = info["url"]
    title = re.sub(r'[\\/:*?"<>|]', "", info.get("title") or "preview")[:80]
    artist = info.get("artist") or ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        if len(data) < 1000:
            await q.message.reply_text("❌ ឯកសារមិនត្រឹមត្រូវ។")
            return
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        try:
            with open(path, "rb") as f:
                await q.message.reply_audio(
                    audio=f,
                    filename=f"{title}.m4a",
                    title=title,
                    performer=artist,
                    caption=(
                        f"🎧 <b>Preview 30 វិនាទី</b>\n{title} — {artist}\n"
                        f"<i>ផ្លូវការ iTunes · មិនមែនបទពេញ</i>"
                    ),
                    parse_mode="HTML",
                )
            d = db_read()
            d["previews"] = int(d.get("previews") or 0) + 1
            db_write(d)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception as e:
        log.exception("dl")
        await q.message.reply_text(f"❌ ទាញមិនបាន៖ {e}")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("កំណត់ BOT_TOKEN")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS ទទេ — /admin នឹងមិនដំណើរការ")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pk:"))
    app.add_handler(CallbackQueryHandler(on_download, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("SrokMusic started · admins=%s", ADMIN_IDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
