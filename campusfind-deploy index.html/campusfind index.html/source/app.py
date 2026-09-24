"""CampusFind: a small, self-hostable lost-and-found web application.

Run `python app.py` for local development or `gunicorn app:app` behind HTTPS.
Set CAMPUSFIND_DATA_DIR to a *persistent* volume in production.
Set CAMPUSFIND_DEMO=1 for the six clearly labelled, fictional demo listings.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, g, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageOps, UnidentifiedImageError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("CAMPUSFIND_DATA_DIR", str(BASE_DIR / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "campusfind.sqlite3"
DEMO_MODE = os.getenv("CAMPUSFIND_DEMO", "0").strip() == "1"
CATEGORIES = ("Electronics", "Bottles", "Keys", "ID cards", "Bags", "Umbrellas", "Other")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ITEMS = int(os.getenv("CAMPUSFIND_MAX_ITEMS", "5000"))
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}$")
WORD_RE = re.compile(r"[\w]+", re.UNICODE)
STOP_WORDS = {"the", "and", "with", "for", "was", "near", "from", "this", "that", "have", "lost", "found", "item", "things", "some", "one", "small", "blue"}
Image.MAX_IMAGE_PIXELS = 14_000_000

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 7 * 1024 * 1024
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_recent_posts: dict[str, deque[float]] = defaultdict(deque)
_recent_messages: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()

SAMPLES = [
    ("demo-1", "found", "Blue steel water bottle", "Bottles", "Central Library", "A blue reusable steel bottle left near the reading tables. Tell the finder about its stickers before arranging a handover.", "2026-09-22"),
    ("demo-2", "lost", "Wireless headphones", "Electronics", "Student Centre", "Black over-ear headphones in a soft carry case. Last seen after the afternoon study session.", "2026-09-22"),
    ("demo-3", "found", "Keys with a green keyring", "Keys", "Engineering Block", "A small bunch of keys with a green keyring. Please confirm how many keys there are at the campus desk.", "2026-09-21"),
    ("demo-4", "found", "A very rainy-day umbrella", "Umbrellas", "Cafeteria", "A navy folding umbrella left by the cafeteria entrance after the rain.", "2026-09-21"),
    ("demo-5", "lost", "Blue water bottle", "Bottles", "Central Library", "I left my blue steel bottle at the library after a study group. It has a small sticker near the bottom.", "2026-09-20"),
    ("demo-6", "found", "Canvas backpack", "Bags", "Sports Complex", "A beige canvas backpack was handed in near the courts. The owner should describe its contents privately.", "2026-09-20"),
]


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL CHECK (type IN ('lost', 'found')),
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                location TEXT NOT NULL,
                description TEXT NOT NULL,
                contact_email TEXT NOT NULL DEFAULT '',
                date_occurred TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reunited')),
                returned_at TEXT,
                photo_filename TEXT,
                owner_hash TEXT NOT NULL,
                sample INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_items_browse ON items(status, sample, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_items_category ON items(category, type, status);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                reply_email TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_item ON messages(item_id, created_at DESC);
        """)
        if DEMO_MODE:
            for row in SAMPLES:
                item_id, item_type, title, category, location, description, occurred = row
                db.execute("""INSERT OR IGNORE INTO items
                    (id,type,title,category,location,description,date_occurred,created_at,owner_hash,sample)
                    VALUES(?,?,?,?,?,?,?,?,?,1)""",
                    (item_id, item_type, title, category, location, description, occurred,
                     occurred + "T12:00:00+00:00", "not-an-owner-token"))


init_db()


@app.teardown_appcontext
def close_connection(_: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


def failure(message: str, status: int = 400):
    return jsonify({"error": message}), status


def public_item(row: sqlite3.Row, detail: bool = False) -> dict:
    item = {
        "id": row["id"], "type": row["type"], "title": row["title"],
        "category": row["category"], "location": row["location"],
        "description": row["description"], "date": row["date_occurred"],
        "createdAt": row["created_at"], "status": row["status"],
        "sample": bool(row["sample"]),
        "photo": "/uploads/" + row["photo_filename"] if row["photo_filename"] else "",
    }
    if detail:
        item["contactEmail"] = row["contact_email"] if row["status"] == "open" else ""
    return item


def visible_clause() -> str:
    return "" if DEMO_MODE else " AND sample = 0"


def tokenize(text: str) -> set[str]:
    return {word for word in WORD_RE.findall(text.lower()) if len(word) > 2 and word not in STOP_WORDS}


def similar_items(target: sqlite3.Row) -> list[dict]:
    candidates = get_db().execute(
        "SELECT * FROM items WHERE id != ? AND status = 'open' AND type != ? AND sample = ? ORDER BY created_at DESC LIMIT 180",
        (target["id"], target["type"], target["sample"]),
    ).fetchall()
    title_words = tokenize(target["title"])
    detail_words = tokenize(target["description"])
    scored = []
    for candidate in candidates:
        score = 0
        if candidate["category"] == target["category"]:
            score += 4
        if candidate["location"].casefold() == target["location"].casefold():
            score += 3
        score += len(title_words & tokenize(candidate["title"])) * 2
        score += len(detail_words & tokenize(candidate["description"]))
        if score >= 5:
            scored.append((score, candidate))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [public_item(row) for _, row in scored[:3]]


def item_data(form) -> tuple[dict | None, str | None]:
    values = {name: str(form.get(name, "")).strip() for name in
              ("type", "title", "category", "location", "description", "contactEmail", "date")}
    if values["type"] not in ("lost", "found"):
        return None, "Choose whether the item was lost or found."
    if not 3 <= len(values["title"]) <= 90:
        return None, "Use a title between 3 and 90 characters."
    if values["category"] not in CATEGORIES:
        return None, "Choose a valid category."
    if not 3 <= len(values["location"]) <= 100:
        return None, "Add a location between 3 and 100 characters."
    if not 12 <= len(values["description"]) <= 800:
        return None, "Add a helpful description (12–800 characters). Leave identifying details for private verification."
    if values["contactEmail"] and (len(values["contactEmail"]) > 160 or not EMAIL_RE.fullmatch(values["contactEmail"])):
        return None, "Enter a valid email address or leave it blank."
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", values["date"]):
            raise ValueError
        occurred = date.fromisoformat(values["date"])
        if occurred < date(2010, 1, 1) or occurred > date.today() + timedelta(days=1):
            raise ValueError
    except ValueError:
        return None, "Choose a valid date when the item was lost or found."
    if str(form.get("website", "")).strip():
        return None, "Could not submit this post."
    return values, None


def save_photo(upload) -> str | None:
    if upload is None or not upload.filename:
        return None
    if upload.mimetype not in ("image/jpeg", "image/png", "image/webp"):
        raise ValueError("Choose a JPG, PNG, or WebP photo.")
    raw = upload.stream.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Photos must be smaller than 5 MB.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("This file is not a supported image.")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            rgb = Image.new("RGB", image.size, "white")
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                image = image.convert("RGBA")
                rgb.paste(image, mask=image.getchannel("A"))
            else:
                rgb.paste(image.convert("RGB"))
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=83, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("This photo could not be read. Try a different image.") from exc
    filename = secrets.token_hex(18) + ".jpg"
    (UPLOAD_DIR / filename).write_bytes(output.getvalue())
    return filename


def authorised(row: sqlite3.Row) -> bool:
    token = request.headers.get("X-Manage-Token", "")
    return (not row["sample"] and len(token) == 64 and bool(re.fullmatch(r"[0-9a-f]{64}", token))
            and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["owner_hash"]))


def under_rate_limit(bucket: dict[str, deque[float]], per_hour: int) -> bool:
    now = time.monotonic()
    ip = request.remote_addr or "unknown"
    with _rate_lock:
        hits = bucket[ip]
        while hits and hits[0] < now - 3600:
            hits.popleft()
        if len(hits) >= per_hour:
            return False
        hits.append(now)
    return True


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.path == "/":
        html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
        if script:
            digest = base64.b64encode(hashlib.sha256(script.group(1).encode("utf-8")).digest()).decode("ascii")
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'sha256-" + digest + "'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
            )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(413)
def too_large(_):
    return failure("The upload is too large. Choose a photo under 5 MB.", 413)


@app.get("/")
def home():
    return send_file(BASE_DIR / "index.html")


@app.get("/health")
def health():
    get_db().execute("SELECT 1").fetchone()
    return jsonify({"status": "ok"})


@app.get("/uploads/<filename>")
def upload_file(filename: str):
    if not re.fullmatch(r"[0-9a-f]{36}\.jpg", filename):
        return failure("Image not found.", 404)
    response = send_from_directory(UPLOAD_DIR, filename)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.get("/api/config")
def config():
    return jsonify({"shared": True, "demo": DEMO_MODE, "maxPhotoMb": 5})


@app.get("/api/items")
def list_items():
    query = request.args.get("q", "").strip()[:100]
    item_type = request.args.get("type", "all")
    category = request.args.get("category", "all")
    sort = request.args.get("sort", "newest")
    if item_type not in ("all", "found", "lost") or category not in ("all", *CATEGORIES) or sort not in ("newest", "oldest"):
        return failure("Invalid filter.")
    try:
        limit = min(24, max(1, int(request.args.get("limit", 9))))
        offset = min(10000, max(0, int(request.args.get("offset", 0))))
    except ValueError:
        return failure("Invalid page number.")
    where = "status = 'open'" + visible_clause()
    args: list = []
    if item_type != "all":
        where += " AND type = ?"; args.append(item_type)
    if category != "all":
        where += " AND category = ?"; args.append(category)
    for token in query.split()[:7]:
        token = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND (title LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\')"
        args.extend([f"%{token}%"] * 4)
    db = get_db()
    count = db.execute(f"SELECT count(*) FROM items WHERE {where}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT * FROM items WHERE {where} ORDER BY created_at {'DESC' if sort == 'newest' else 'ASC'}, id ASC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    stats = db.execute("SELECT type, status, count(*) AS n FROM items WHERE 1=1" + visible_clause() + " GROUP BY type,status").fetchall()
    totals = {"found": 0, "lost": 0, "reunited": 0}
    for row in stats:
        if row["status"] == "reunited": totals["reunited"] += row["n"]
        else: totals[row["type"]] += row["n"]
    return jsonify({"items": [public_item(row) for row in rows], "count": count, "stats": totals,
                    "hasMore": offset + len(rows) < count})


@app.get("/api/items/<item_id>")
def get_item(item_id: str):
    row = get_db().execute("SELECT * FROM items WHERE id = ?" + visible_clause(), (item_id,)).fetchone()
    if row is None:
        return failure("This listing was not found.", 404)
    return jsonify({"item": public_item(row, detail=True),
                    "matches": similar_items(row) if row["status"] == "open" else []})


@app.post("/api/items")
def create_item():
    values, err = item_data(request.form)
    if err: return failure(err)
    db = get_db()
    if db.execute("SELECT count(*) FROM items WHERE sample = 0").fetchone()[0] >= MAX_ITEMS:
        return failure("Posting is temporarily full. Please try later.", 503)
    try:
        filename = save_photo(request.files.get("photo"))
    except ValueError as exc:
        return failure(str(exc))
    if not under_rate_limit(_recent_posts, 12):
        if filename: (UPLOAD_DIR / filename).unlink(missing_ok=True)
        return failure("You have posted too many items in one hour. Please try again later.", 429)
    item_id = secrets.token_hex(16)
    token = secrets.token_hex(32)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        db.execute("""INSERT INTO items
            (id,type,title,category,location,description,contact_email,date_occurred,created_at,photo_filename,owner_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, values["type"], values["title"], values["category"], values["location"],
             values["description"], values["contactEmail"], values["date"], created,
             filename, hashlib.sha256(token.encode()).hexdigest()))
        db.commit()
    except Exception:
        if filename: (UPLOAD_DIR / filename).unlink(missing_ok=True)
        raise
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"item": public_item(row, detail=True), "manageToken": token}), 201


@app.patch("/api/items/<item_id>")
def update_item(item_id: str):
    db = get_db()
    previous = db.execute("SELECT * FROM items WHERE id = ? AND sample = 0", (item_id,)).fetchone()
    if not previous: return failure("This listing was not found.", 404)
    if not authorised(previous): return failure("You need your private management link to edit this post.", 403)
    if previous["status"] != "open": return failure("A reunited listing can no longer be edited.", 409)
    values, err = item_data(request.form)
    if err: return failure(err)
    try:
        filename = save_photo(request.files.get("photo"))
    except ValueError as exc:
        return failure(str(exc))
    try:
        db.execute("""UPDATE items SET type=?,title=?,category=?,location=?,description=?,contact_email=?,
            date_occurred=?,photo_filename=? WHERE id=?""",
            (values["type"], values["title"], values["category"], values["location"],
             values["description"], values["contactEmail"], values["date"],
             filename or previous["photo_filename"], item_id))
        db.commit()
    except Exception:
        if filename: (UPLOAD_DIR / filename).unlink(missing_ok=True)
        raise
    if filename and previous["photo_filename"]:
        (UPLOAD_DIR / previous["photo_filename"]).unlink(missing_ok=True)
    return jsonify({"item": public_item(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone(), detail=True)})


@app.post("/api/items/<item_id>/resolve")
def resolve_item(item_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=? AND sample=0", (item_id,)).fetchone()
    if not row: return failure("This listing was not found.", 404)
    if not authorised(row): return failure("You need your private management link to manage this post.", 403)
    if row["status"] != "open": return failure("This listing has already been marked reunited.", 409)
    db.execute("UPDATE items SET status='reunited', returned_at=? WHERE id=?",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"), item_id))
    db.commit()
    return jsonify({"item": public_item(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone(), detail=True)})


@app.post("/api/items/<item_id>/messages")
def send_message(item_id: str):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ? AND status = 'open'" + visible_clause(), (item_id,)).fetchone()
    if not item or item["sample"]:
        return failure("This listing cannot receive messages.", 404)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return failure("Invalid message.")
    email = str(data.get("replyEmail", "")).strip()
    body = str(data.get("message", "")).strip()
    if len(email) > 160 or not EMAIL_RE.fullmatch(email):
        return failure("Enter a valid reply email address.")
    if not 12 <= len(body) <= 1000:
        return failure("Write a message between 12 and 1,000 characters.")
    if str(data.get("website", "")).strip():
        return failure("Could not send this message.")
    if not under_rate_limit(_recent_messages, 20):
        return failure("Too many messages sent. Please try again later.", 429)
    db.execute("INSERT INTO messages(id,item_id,reply_email,body,created_at) VALUES(?,?,?,?,?)",
               (secrets.token_hex(16), item_id, email, body,
                datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.commit()
    return jsonify({"ok": True, "note": "Your message is in the post owner's private inbox. No automatic email is sent."}), 201


@app.get("/api/items/<item_id>/messages")
def list_messages(item_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=? AND sample=0", (item_id,)).fetchone()
    if not row: return failure("This listing was not found.", 404)
    if not authorised(row): return failure("You need your private management link to see messages.", 403)
    messages = db.execute("SELECT reply_email,body,created_at FROM messages WHERE item_id=? ORDER BY created_at DESC LIMIT 100", (item_id,)).fetchall()
    return jsonify({"messages": [{"replyEmail": m["reply_email"], "message": m["body"], "createdAt": m["created_at"]} for m in messages]})


@app.delete("/api/items/<item_id>")
def delete_item(item_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=? AND sample=0", (item_id,)).fetchone()
    if not row: return failure("This listing was not found.", 404)
    if not authorised(row): return failure("You need your private management link to remove this post.", 403)
    db.execute("DELETE FROM items WHERE id=?", (item_id,))
    db.commit()
    if row["photo_filename"]:
        (UPLOAD_DIR / row["photo_filename"]).unlink(missing_ok=True)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8001")), debug=False)
