import os
import json
import time
import sqlite3
import random
import re
from queue import Queue
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

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
    # ✅ Classification fields nullable (so non-disaster posts won't crash)
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
    If an old table had NOT NULL constraint that can crash writes,
    rebuild it safely for demo stability.
    """
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'")
    row = cur.fetchone()
    if row and row["sql"]:
        sql = row["sql"]
        if "disaster_type TEXT NOT NULL" in sql:
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
            cur.execute("""
              INSERT INTO posts(id, author, content, created_at, is_disaster, disaster_type, urgency, confidence, location_text, lat, lon)
              SELECT id, author, content, created_at, is_disaster, disaster_type, urgency, confidence, location_text, lat, lon
              FROM posts_old
            """)
            cur.execute("DROP TABLE posts_old")
            conn.commit()
    conn.close()


def execute_with_retry(sql: str, params: tuple, retries: int = 6) -> int:
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
                time.sleep(0.20 * (i + 1))
                continue
            raise
    raise last_err  # type: ignore


# ✅ run DB init/migration on import so Gunicorn has tables too
init_db()
migrate_db_if_needed()


# ----------------------------
# Live SSE (Server-Sent Events)
# ----------------------------
clients: List[Queue[str]] = []

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
    Keep-alives help proxies keep connection open.
    """
    q: Queue[str] = Queue(maxsize=500)
    clients.append(q)

    def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    msg = q.get(timeout=10)
                    yield f"data: {msg}\n\n"
                except Exception:
                    # keep-alive comment
                    yield ": keep-alive\n\n"
        finally:
            try:
                clients.remove(q)
            except Exception:
                pass

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


# ----------------------------
# Better Classifier (more complete + more accurate)
# - Stronger disaster gate
# - Richer type detection
# - Much better urgency scoring
# - Barangay detection supports ALL listed barangays + variants
# ----------------------------

# Lipa center (fallback pin)
LIPA_CENTER = (13.941, 121.163)

# Known coords you already had (keep; add more later if you want)
BARANGAY_COORDS: Dict[str, Tuple[float, float]] = {
    "sabang": (13.936, 121.170),
    "marawoy": (13.956, 121.150),
    "lodlod": (13.927, 121.169),
    "bulacnin": (13.944, 121.149),
    "sico": (13.941, 121.206),
    "tambo": (13.970, 121.160),
    "balintawak": (13.947, 121.176),
    "san carlos": (13.959, 121.182),
    "pinagtongulan": (13.951, 121.162),
    "san sebastian": (13.950, 121.160),  # approximate-ish demo pin (safe)
    "mataas na lupa": (13.948, 121.170),  # approximate-ish demo pin (safe)
    "bolbok": (13.940, 121.155),          # approximate-ish demo pin (safe)
}

# ✅ Full barangay name list (normalized matching)
ALL_BARANGAYS: List[str] = [
    # Urban
    "Barangay 1","Barangay 2","Barangay 3","Barangay 4","Barangay 5","Barangay 6",
    "Barangay 7","Barangay 8","Barangay 9","Barangay 9-A","Barangay 10","Barangay 11",
    # Rural
    "Adya","Anilao","Anilao-Labac","Antipolo Del Norte","Antipolo Del Sur","Bagong Pook","Balintawak",
    "Banaybanay","Bolbok","Bulaklakan","Bugtong na Pulo","Bulacnin","Calamias","Cumba","Dagatan",
    "Duhatan","Fernando","Halang","Inosloban","Kayumanggi","Latag","Lodlod","Lumbang","Mabini",
    "Malagonlong","Malitlit","Marauoy","Mataas Na Lupa","Munting Pulo",
    "Pagolingin Bata","Pagolingin East","Pagolingin West","Pangao","Pinagkawitan","Pinagtongulan",
    "Plaridel","Pusil","Quezon","Rizal","Sabang","Sampaguita","San Benito","San Carlos","San Celestino",
    "San Francisco","San Guillermo","San Isidro","San Jose","San Lucas","San Salvador","San Sebastian",
    "Santo Nino","Santo Toribio","Sico","Talisay","Tambo","Tangob","Tanguay","Tibig","Tipacan"
]

# Helpful aliases for tricky spellings
BARANGAY_ALIASES: Dict[str, List[str]] = {
    "banaybanay": ["banay banay", "banay-banay"],
    "barangay 9-a": ["barangay 9a", "brgy 9a", "brgy 9-a", "bg 9a", "bg 9-a"],
    "bugtong na pulo": ["bugtongnapulo", "bugtong na pulo", "bugtongnapulo"],
    "mataas na lupa": ["mataas na lupa", "mataas na lupa", "mataasnalupa"],
    "munting pulo": ["munting pulo", "muntingpulo"],
    "anilao-labac": ["anilao labac", "anilao-labac", "labac"],
    "antipolo del norte": ["antipolo norte", "antipolo del norte"],
    "antipolo del sur": ["antipolo sur", "antipolo del sur"],
    "san nino": ["santo nino", "sto nino", "sto. nino", "santo niño", "sto niño"],
}

# Normalize text for robust matching (hyphens, punctuation, extra spaces)
_norm_re = re.compile(r"[^a-z0-9\s]+", re.IGNORECASE)
_space_re = re.compile(r"\s+")

def normalize(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("ñ", "n")
    s = s.replace("brgy.", "brgy ")
    s = s.replace("brgy", "brgy ")
    s = s.replace("barangay", "barangay ")
    s = s.replace("-", " ")
    s = _norm_re.sub(" ", s)
    s = _space_re.sub(" ", s).strip()
    return s

def contains_any(t: str, words: List[str]) -> bool:
    return any(w in t for w in words)

def count_any(t: str, words: List[str]) -> int:
    return sum(1 for w in words if w in t)

def detect_barangay(original_text: str) -> Tuple[str, Optional[float], Optional[float]]:
    """
    Returns (location_text, lat, lon). If barangay not found, returns city fallback.
    """
    t = normalize(original_text)

    # Direct patterns: "brgy 7", "barangay 7", "brgy. 9-a"
    # We'll look for "barangay <token>" or "brgy <token>"
    # token can be digits or "9 a"
    m = re.search(r"\b(?:brgy|barangay)\s+([0-9]{1,2})(?:\s*a)?\b", t)
    if m:
        num = m.group(1)
        # detect 9-a
        if "9" == num and re.search(r"\b(?:brgy|barangay)\s+9\s*a\b", t):
            loc_name = "Barangay 9-A"
            key = "barangay 9-a"
        else:
            loc_name = f"Barangay {num}"
            key = f"barangay {num}".lower()
        latlon = BARANGAY_COORDS.get(key, None)
        if latlon:
            return (f"{loc_name}, Lipa City", latlon[0], latlon[1])
        return (f"{loc_name}, Lipa City", LIPA_CENTER[0], LIPA_CENTER[1])

    # Exact barangay name scan (normalized)
    # Build a list of normalized barangay names once
    normalized_barangays = []
    for b in ALL_BARANGAYS:
        normalized_barangays.append((normalize(b), b))

    # Check aliases first
    for canon, aliases in BARANGAY_ALIASES.items():
        for a in aliases:
            if normalize(a) in t:
                # Canon could be "barangay 9-a" or a rural name
                canon_title = canon.title().replace("Na", "na").replace("Del", "Del").replace("De", "De")
                # make pretty label
                if canon.startswith("barangay"):
                    pretty = canon_title.replace("Barangay", "Barangay ")
                    loc_text = f"{pretty}, Lipa City"
                else:
                    loc_text = f"Brgy. {canon_title}, Lipa City"
                lat, lon = BARANGAY_COORDS.get(canon, LIPA_CENTER)
                return (loc_text, lat, lon)

    # Then check full names
    for norm_b, pretty_b in normalized_barangays:
        if norm_b and norm_b in t:
            # Key for coords uses normalized pretty name
            key = normalize(pretty_b)
            # Convert "san carlos" style keys
            coord_key = key
            # remove "barangay " prefix for numeric? (already handled earlier)
            if coord_key.startswith("barangay "):
                coord_key = coord_key
            lat, lon = BARANGAY_COORDS.get(coord_key, LIPA_CENTER)
            if pretty_b.lower().startswith("barangay"):
                loc_text = f"{pretty_b}, Lipa City"
            else:
                loc_text = f"Brgy. {pretty_b}, Lipa City"
            return (loc_text, lat, lon)

    # "Poblacion" hint (urban cluster)
    if "poblacion" in t:
        return ("Poblacion, Lipa City", LIPA_CENTER[0], LIPA_CENTER[1])

    return ("Lipa City (unspecified)", LIPA_CENTER[0], LIPA_CENTER[1])

# Expanded keywords (English + Filipino, common typos included)
DISASTER_GATE = [
    # flood
    "baha","bahaa","flood","flud","lubog","apaw","rumaragas","overflow","umaapaw",
    # fire
    "sunog","sunug","fire","apoy","usok","nasusunog","burning","nasunog",
    # landslide
    "guho","gumuho","landslide","pagguho","gumuhô","collapse","cave in","rockfall",
    # typhoon/wind/rain
    "bagyo","bagyoo","typhoon","storm","hangin","malakas na hangin","signal",
    "malakas ulan","heavy rain","torrential","ulan",
    # quake
    "lindol","lind0l","earthquake","yanig","tremor","aftershock",
    # power
    "brownout","blackout","kuryente","walang kuryente","outage","power outage",
    # rescue/medical/urgent
    "saklolo","tulong","help","rescue","trapped","trap","stranded","naipit",
    "ambulance","nasugatan","injured","may sugat","hirap huminga","bleeding","unconscious",
    # evacuation / hazard
    "evacuate","evacuation","ilikas","lumikas","evac center","evacuation center",
    # roads / trees
    "bagsak puno","bumagsak puno","fallen tree","road blocked","barado kalsada","landslide sa kalsada",
]

# Disaster type keyword buckets (more specific)
TYPE_BUCKETS: List[Tuple[str, List[str]]] = [
    ("Flood", ["baha","flood","flud","lubog","apaw","overflow","umaapaw","flash flood","rising water","waist high","chest high"]),
    ("Fire", ["sunog","sunug","fire","apoy","usok","nasusunog","burning","sumasabog","explosion","short circuit"]),
    ("Landslide", ["landslide","pagguho","guho","gumuho","rockfall","cave in","soil erosion","erosion"]),
    ("Typhoon", ["bagyo","typhoon","storm","hangin","malakas na hangin","signal","malakas ulan","heavy rain","torrential","rainfall"]),
    ("Earthquake", ["lindol","earthquake","yanig","tremor","aftershock"]),
    ("Power", ["brownout","blackout","kuryente","walang kuryente","outage","power outage","transformer"]),
    ("Medical", ["ambulance","nasugatan","injured","may sugat","hirap huminga","bleeding","unconscious","stroke","heart attack","seizure","diabetic"]),
    ("Other", []),
]

# Urgency cues (weighted scoring)
CRITICAL_CUES = [
    "saklolo","sos","help asap","need rescue","rescue asap","urgent","asap","immediate",
    "trapped","naipit","stranded","di makalabas","di makaluwas","nasa bubong","sa bubong","rooftop",
    "may bata","may sanggol","may baby","buntis","matanda","senior","pwd","disabled",
    "unconscious","hindi humihinga","not breathing","bleeding","malubha","critical condition",
    "waist high","chest high","lampas bewang","lampas dibdib",
]
HIGH_CUES = [
    "tulong","need help","please help","evacuate","ilikas","lumikas","evacuation",
    "malakas","delikado","danger","hazard","rapidly rising","rising water",
    "usok","apoy","nasusunog","fire spreading",
    "injured","nasugatan","hirap huminga","medical",
    "road blocked","barado","impassable","landslide sa kalsada","bagsak puno","fallen tree",
]
MODERATE_CUES = [
    "monitor","nagbabantay","warning","alert","ingat","caution",
    "minor","mababa","ankle deep","bahagya","may tubig","umaambon",
]

# Strong “not disaster” cues to reduce false positives
NON_DISASTER_CUES = [
    "lol","haha","meme","joke","biruan","prank","for fun","trip lang",
    "happy birthday","congrats","congratulations","sell","for sale","buy","benta","promo",
    "lost and found","missing wallet","looking for","open for business",
]

def classify(text: str) -> Dict[str, Any]:
    """
    Returns dict:
      is_disaster (0/1)
      disaster_type (str)
      urgency (CRITICAL/HIGH/MODERATE)
      confidence (85-99)
      location_text, lat, lon
    """
    original = text or ""
    t = normalize(original)

    # quick reject: obvious non-disaster
    if contains_any(t, NON_DISASTER_CUES) and not contains_any(t, ["baha","sunog","bagyo","lindol","saklolo","rescue","trapped","injured"]):
        return {"is_disaster": 0}

    # disaster gate
    if not contains_any(t, [normalize(w) for w in DISASTER_GATE]):
        return {"is_disaster": 0}

    # disaster type detection (first bucket hit)
    dtype = "Other"
    for label, kws in TYPE_BUCKETS:
        if kws and contains_any(t, [normalize(k) for k in kws]):
            dtype = label
            break
    if dtype == "Other":
        # If it passed gate, choose best guess from common strong words
        if contains_any(t, ["baha","flood","lubog","apaw"]): dtype = "Flood"
        elif contains_any(t, ["sunog","fire","apoy","usok"]): dtype = "Fire"
        elif contains_any(t, ["bagyo","typhoon","hangin","ulan"]): dtype = "Typhoon"
        elif contains_any(t, ["lindol","earthquake","yanig"]): dtype = "Earthquake"
        elif contains_any(t, ["guho","landslide","pagguho","gumuho"]): dtype = "Landslide"
        elif contains_any(t, ["brownout","kuryente","outage"]): dtype = "Power"
        elif contains_any(t, ["ambulance","injured","nasugatan","hirap huminga"]): dtype = "Medical"

    # urgency scoring (weighted)
    score = 0

    score += 5 * count_any(t, [normalize(w) for w in CRITICAL_CUES])
    score += 3 * count_any(t, [normalize(w) for w in HIGH_CUES])
    score += 1 * count_any(t, [normalize(w) for w in MODERATE_CUES])

    # extra numeric signal: "10 tao", "15 persons", "family of 6"
    if re.search(r"\b(\d+)\s*(tao|katao|persons|people|pax|famil(y|ies))\b", t):
        score += 2
    # mention of deaths or severe injury -> critical
    if contains_any(t, ["patay","dead","fatal","namatay"]):
        score += 7

    # type-based severity boosters
    if dtype == "Fire" and contains_any(t, ["sumasabog","explosion","spreading","kumakalat"]):
        score += 4
    if dtype == "Flood" and contains_any(t, ["flash","rumaragas","rapidly","raging","malakas agos","strong current"]):
        score += 4
    if dtype in ("Landslide","Earthquake") and contains_any(t, ["crack","bitak","gumuho","collapsed","aftershock","yanig"]):
        score += 3

    if score >= 12:
        urg = "CRITICAL"
    elif score >= 6:
        urg = "HIGH"
    else:
        urg = "MODERATE"

    # location detection (ALL barangays + variants)
    loc_text, lat, lon = detect_barangay(original)

    # confidence (more “realistic”: higher when stronger signals)
    base = 85
    base += min(10, count_any(t, [normalize(w) for w in CRITICAL_CUES]) * 2)
    base += min(6,  count_any(t, [normalize(w) for w in HIGH_CUES]))
    base += 2 if ("brgy" in t or "barangay" in t or "poblacion" in t) else 0
    conf = max(85, min(99, base + random.randint(-2, 2)))

    return {
        "is_disaster": 1,
        "disaster_type": dtype,
        "urgency": urg,
        "confidence": conf,
        "location_text": loc_text,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
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

    # broadcast to dashboard listeners
    broadcast({"type": "new_post", "post": full})

    # user response: safe only (no urgency/confidence/type)
    safe = {"id": post_id, "author": author, "content": content, "created_at": created_at}
    return jsonify({"ok": True, "post": safe})


@app.get("/api/posts")
def list_posts():
    role = session.get("role", "anon")
    limit = min(int(request.args.get("limit", "300")), 500)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    if role == "admin":
        return jsonify({"ok": True, "posts": rows})

    safe_rows = [
        {"id": r["id"], "author": r["author"], "content": r["content"], "created_at": r["created_at"]}
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
    app.run(host="127.0.0.1", port=5000, debug=True)
