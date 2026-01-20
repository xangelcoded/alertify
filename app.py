import os
import json
import time
import sqlite3
import random
from queue import Queue
from datetime import datetime
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify, Response, send_from_directory, session

# ----------------------------
# Paths / Config
# ----------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

# ✅ Render-safe writable path (ephemeral, good for demos)
DB_PATH = os.path.join("/tmp", "alertify.db")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@lipa.gov.ph")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

app = Flask(__name__, static_folder=PUBLIC_DIR, static_url_path="")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")


# ----------------------------
# DB Helpers (WAL + busy timeout)
# ----------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,                # wait for locks
        check_same_thread=False    # safer under threaded gunicorn
    )
    conn.row_factory = sqlite3.Row

    # ✅ reduce "database is locked"
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    # ✅ Classification fields are nullable (so non-disaster posts won't crash)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,

        is_disaster INTEGER NOT NULL DEFAULT 0,

        disaster_type TEXT,
        urgency TEXT,
        confidence INTEGER,

        location_text TEXT,
        lat REAL,
        lon REAL
      )
    """)
    conn.commit()
    conn.close()


def migrate_db_if_needed() -> None:
    """
    If your old table had 'disaster_type TEXT NOT NULL' we rebuild it safely for demo stability.
    This runs at boot and avoids your 500 NOT NULL error forever.
    """
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'")
    row = cur.fetchone()
    if row and row["sql"]:
        sql = row["sql"]
        if "disaster_type TEXT NOT NULL" in sql:
            # rebuild table
            cur.execute("ALTER TABLE posts RENAME TO posts_old")
            cur.execute("""
              CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_disaster INTEGER NOT NULL DEFAULT 0,
                disaster_type TEXT,
                urgency TEXT,
                confidence INTEGER,
                location_text TEXT,
                lat REAL,
                lon REAL
              )
            """)
            # copy what exists (some cols may not exist in very old versions)
            cur.execute("""
              INSERT INTO posts(id, author, content, created_at, is_disaster, disaster_type, urgency, confidence, location_text, lat, lon)
              SELECT id, author, content, created_at, is_disaster, disaster_type, urgency, confidence, location_text, lat, lon
              FROM posts_old
            """)
            cur.execute("DROP TABLE posts_old")
            conn.commit()
    conn.close()


def execute_with_retry(sql: str, params: tuple, retries: int = 5) -> int:
    """
    Write helper that retries if SQLite is temporarily locked.
    Returns lastrowid.
    """
    last_err = None
    for i in range(retries):
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute(sql, params)
            last_id = cur.lastrowid
            conn.commit()
            conn.close()
            return last_id
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower():
                time.sleep(0.15 * (i + 1))
                continue
            raise
    raise last_err  # type: ignore


# ✅ run DB init/migration on import so Gunicorn has tables too
init_db()
migrate_db_if_needed()


# ----------------------------
# Live SSE (Server-Sent Events)
# ----------------------------
clients = []  # list[Queue[str]]

def broadcast(event: Dict[str, Any]) -> None:
    payload = json.dumps(event, ensure_ascii=False)
    for q in list(clients):
        try:
            q.put_nowait(payload)
        except Exception:
            pass


@app.get("/api/stream")
def stream() -> Response:
    """
    SSE endpoint for LGU dashboard real-time updates.
    We keep sending keep-alive comments so proxies don't drop connection.
    """
    q: Queue[str] = Queue(maxsize=500)
    clients.append(q)

    def gen():
        try:
            # initial hello
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except Exception:
                    # keep-alive
                    yield ": keep-alive\n\n"
        finally:
            try:
                clients.remove(q)
            except Exception:
                pass

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # helps some proxies
    return resp


# ----------------------------
# Classifier (demo but stable + typo tolerant enough)
# ----------------------------
BARANGAY_COORDS = {
    "sabang": (13.936, 121.170),
    "marawoy": (13.956, 121.150),
    "lodlod": (13.927, 121.169),
    "bulacnin": (13.944, 121.149),
    "sico": (13.941, 121.206),
    "tambo": (13.970, 121.160),
    "balintawak": (13.947, 121.176),
    "san carlos": (13.959, 121.182),
    "pinagtongulan": (13.951, 121.162),
}

DISASTER_KEYWORDS = [
    "baha","flood","lubog","apaw",
    "sunog","fire","usok","apoy",
    "bagyo","typhoon","storm","hangin",
    "lindol","earthquake","yanig","tremor",
    "guho","landslide","pagguho","gumuho",
    "rescue","saklolo","tulong","trapped","stranded",
    "ambulance","nasugatan","injured",
    "brownout","kuryente","outage"
]

def classify(text: str) -> Dict[str, Any]:
    t = (text or "").lower()

    # gate: if no disaster keywords -> non-disaster
    if not any(w in t for w in DISASTER_KEYWORDS):
        return {"is_disaster": 0}

    # type
    if any(w in t for w in ["baha","flood","lubog","apaw"]):
        dtype = "Flood"
    elif any(w in t for w in ["sunog","fire","usok","apoy"]):
        dtype = "Fire"
    elif any(w in t for w in ["guho","landslide","pagguho","gumuho"]):
        dtype = "Landslide"
    elif any(w in t for w in ["bagyo","typhoon","storm","hangin"]):
        dtype = "Typhoon"
    elif any(w in t for w in ["lindol","earthquake","yanig","tremor"]):
        dtype = "Earthquake"
    elif any(w in t for w in ["brownout","kuryente","outage"]):
        dtype = "Power"
    elif any(w in t for w in ["ambulance","hirap huminga","nasugatan","injured"]):
        dtype = "Medical"
    else:
        dtype = "Other"

    # urgency scoring
    crit = ["saklolo","sos","trapped","bubong","rooftop","may bata","matanda","urgent","asap","di makalabas","stranded"]
    high = ["need help","tulong","evacuate","nasugatan","injured","malakas","delikado","usok","apoy"]

    score = sum(3 for w in crit if w in t) + sum(2 for w in high if w in t)

    if score >= 6:
        urg = "CRITICAL"
    elif score >= 3:
        urg = "HIGH"
    else:
        urg = "MODERATE"

    # location detection
    loc_text = "Lipa City (unspecified)"
    lat, lon = 13.941, 121.163
    for b, (la, lo) in BARANGAY_COORDS.items():
        if b in t:
            loc_text = f"Brgy. {b.title()}, Lipa City"
            lat, lon = la, lo
            break

    conf = random.randint(85, 99)

    return {
        "is_disaster": 1,
        "disaster_type": dtype,
        "urgency": urg,
        "confidence": conf,
        "location_text": loc_text,
        "lat": lat,
        "lon": lon
    }


# ----------------------------
# Auth
# ----------------------------
@app.post("/api/auth/user-login")
def user_login():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    session["role"] = "user"
    session["name"] = name[:60]
    return jsonify({"ok": True})


@app.post("/api/auth/admin-login")
def admin_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()

    if email == ADMIN_EMAIL and password == ADMIN_PASS:
        session["role"] = "admin"
        session["name"] = "CDRRMO Admin"
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Invalid credentials"}), 401


@app.get("/api/me")
def me():
    return jsonify({"role": session.get("role", "anon"), "name": session.get("name", "")})


# ----------------------------
# Posts
# ----------------------------
@app.post("/api/posts")
def create_post():
    if session.get("role") not in ("user", "admin"):
        return jsonify({"ok": False, "error": "Login required"}), 401

    data = request.get_json(silent=True) or {}
    author = (data.get("author") or session.get("name") or "Anonymous").strip()[:60]
    content = (data.get("content") or "").strip()

    if not content:
        return jsonify({"ok": False, "error": "Content required"}), 400

    cls = classify(content) or {}
    is_disaster = int(cls.get("is_disaster", 0))

    # ✅ ensure safe defaults (prevents NOT NULL crashes even if schema changes)
    disaster_type = cls.get("disaster_type") if is_disaster else None
    urgency = cls.get("urgency") if is_disaster else None
    confidence = int(cls.get("confidence")) if (is_disaster and cls.get("confidence") is not None) else None
    location_text = cls.get("location_text") if is_disaster else None
    lat = float(cls.get("lat")) if (is_disaster and cls.get("lat") is not None) else None
    lon = float(cls.get("lon")) if (is_disaster and cls.get("lon") is not None) else None

    created_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    post_id = execute_with_retry(
        """INSERT INTO posts(author, content, created_at, is_disaster, disaster_type, urgency, confidence, location_text, lat, lon)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (author, content, created_at, is_disaster, disaster_type, urgency, confidence, location_text, lat, lon)
    )

    # payload for admin dashboards (full details)
    full = {
        "id": post_id,
        "author": author,
        "content": content,
        "created_at": created_at,
        "is_disaster": is_disaster,
        "disaster_type": disaster_type,
        "urgency": urgency,
        "confidence": confidence,
        "location_text": location_text,
        "lat": lat,
        "lon": lon,
    }

    # broadcast to dashboard (dashboard filters is_disaster anyway)
    broadcast({"type": "new_post", "post": full})

    # user response: safe only (no urgency/confidence/type)
    safe = {"id": post_id, "author": author, "content": content, "created_at": created_at}
    return jsonify({"ok": True, "post": safe})


@app.get("/api/posts")
def list_posts():
    role = session.get("role", "anon")
    include_filtered = request.args.get("include_filtered", "0") == "1"
    limit = min(int(request.args.get("limit", "300")), 500)

    conn = db()
    cur = conn.cursor()

    if role == "admin":
        # admin sees all
        cur.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"ok": True, "posts": rows})

    # user sees all posts in citizen feed,
    # but ONLY safe fields (no urgency/confidence/type).
    cur.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    safe_rows = [
        {
            "id": r["id"],
            "author": r["author"],
            "content": r["content"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]

    return jsonify({"ok": True, "posts": safe_rows})


# ----------------------------
# Static pages
# ----------------------------
@app.get("/")
def root():
    return send_from_directory(PUBLIC_DIR, "index.html")

@app.get("/<path:path>")
def serve(path):
    return send_from_directory(PUBLIC_DIR, path)


# ----------------------------
# Local run
# ----------------------------
if __name__ == "__main__":
    # for local dev only
    app.run(host="127.0.0.1", port=5000, debug=True)
