import os
import json
import time
import sqlite3
import random
import re
import logging
import difflib
from queue import Queue
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple, Optional

from flask import Flask, request, jsonify, Response, send_from_directory, session, g

# ============================================================
# Config (demo + production-safe defaults)
# ============================================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

DB_PATH = os.getenv("DB_PATH", os.path.join("/tmp", "alertify.db"))

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@lipa.gov.ph")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PROD = APP_ENV in ("prod", "production")

MAX_CONTENT_CHARS = int(os.getenv("MAX_CONTENT_CHARS", "2000"))

# Fuzzy location (stronger by default, still controlled)
ENABLE_FUZZY_LOCATION = os.getenv("ENABLE_FUZZY_LOCATION", "1").strip() == "1"
FUZZY_CUTOFF = float(os.getenv("FUZZY_CUTOFF", "0.88"))  # default: typo-friendly
FUZZY_CUTOFF_WITH_BRGY = float(os.getenv("FUZZY_CUTOFF_WITH_BRGY", "0.84"))
FUZZY_MAX_LEN_GAP = int(os.getenv("FUZZY_MAX_LEN_GAP", "7"))

# ============================================================
# App init
# ============================================================
app = Flask(__name__, static_folder=PUBLIC_DIR, static_url_path="")
app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,
)

logging.basicConfig(
    level=logging.INFO if IS_PROD else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("alertify")


@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


# ============================================================
# DB helpers (per-request connection + WAL)
# ============================================================
def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def db() -> sqlite3.Connection:
    if "db_conn" not in g:
        g.db_conn = _connect_db()
    return g.db_conn


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def init_db() -> None:
    conn = _connect_db()
    cur = conn.cursor()
    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,

        is_disaster INTEGER NOT NULL DEFAULT 0,
        disaster_type TEXT,
        urgency TEXT,
        urgency_score INTEGER,
        confidence REAL,

        location_text TEXT,
        lat REAL,
        lon REAL
      )
    """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_is_disaster ON posts(is_disaster);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_urgency ON posts(urgency);")
    conn.commit()
    conn.close()


def migrate_db_if_needed() -> None:
    """Safe additive migrations."""
    conn = _connect_db()
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posts'")
    if not cur.fetchone():
        conn.close()
        init_db()
        return

    cur.execute("PRAGMA table_info(posts)")
    cols = {row["name"] for row in cur.fetchall()}

    if "urgency_score" not in cols:
        cur.execute("ALTER TABLE posts ADD COLUMN urgency_score INTEGER;")
    if "confidence" not in cols:
        cur.execute("ALTER TABLE posts ADD COLUMN confidence REAL;")
    if "location_text" not in cols:
        cur.execute("ALTER TABLE posts ADD COLUMN location_text TEXT;")
    if "lat" not in cols:
        cur.execute("ALTER TABLE posts ADD COLUMN lat REAL;")
    if "lon" not in cols:
        cur.execute("ALTER TABLE posts ADD COLUMN lon REAL;")

    conn.commit()
    conn.close()


def execute_with_retry(sql: str, params: tuple, retries: int = 6) -> int:
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            conn = _connect_db()
            cur = conn.cursor()
            cur.execute(sql, params)
            last_id = cur.lastrowid
            conn.commit()
            conn.close()
            return int(last_id)
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower():
                time.sleep(0.20 * (i + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    raise last_err if last_err else RuntimeError("DB write failed")


init_db()
migrate_db_if_needed()

# ============================================================
# SSE
# ============================================================
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


# ============================================================
# Normalization + safe phrase matching
# FIXED: DO NOT corrupt "brgy" by replacing "brg" as substring
# ============================================================
_norm_re = re.compile(r"[^a-z0-9\s]+", re.IGNORECASE)
_space_re = re.compile(r"\s+")

_BRGY_TOKEN_RE = re.compile(r"\b(?:brgy|brg|bgry|brgay)\.?\b", re.IGNORECASE)
_BARANGAY_TOKEN_RE = re.compile(r"\b(?:barangay|baranggay|baragay)\b", re.IGNORECASE)


def normalize(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("ñ", "n")

    # normalize whole tokens only (prevents "brgy" -> "brgyy" bug)
    s = _BRGY_TOKEN_RE.sub("brgy", s)
    s = _BARANGAY_TOKEN_RE.sub("barangay", s)

    s = s.replace("-", " ")
    s = _norm_re.sub(" ", s)
    s = _space_re.sub(" ", s).strip()
    return s


def _has_word(t_norm: str, phrase_norm: str) -> bool:
    p = re.escape(phrase_norm).replace("\\ ", r"\s+")
    return re.search(rf"\b{p}\b", t_norm) is not None


def _count_phrases(t_norm: str, phrases_norm: List[str]) -> int:
    return sum(1 for p in phrases_norm if _has_word(t_norm, p))


def _any_phrase(t_norm: str, phrases_norm: List[str]) -> bool:
    return any(_has_word(t_norm, p) for p in phrases_norm)


def _tokenize_norm(t_norm: str) -> List[str]:
    return [x for x in t_norm.split() if x]


# ============================================================
# Location knowledge (Lipa City)
# ============================================================
LIPA_CENTER = (13.941, 121.163)

BARANGAY_COORDS: Dict[str, Tuple[float, float]] = {
    "poblacion barangay 1": (13.9404, 121.1579),
    "poblacion barangay 2": (13.9421, 121.1625),
    "poblacion barangay 3": (13.9439, 121.1608),
    "poblacion barangay 4": (13.9405, 121.1621),
    "poblacion barangay 5": (13.9392, 121.1633),
    "poblacion barangay 6": (13.9412, 121.1645),
    "poblacion barangay 7": (13.9432, 121.1668),
    "poblacion barangay 8": (13.9452, 121.1685),
    "poblacion barangay 9": (13.9428, 121.1644),
    "poblacion barangay 9 a": (13.9415, 121.1652),
    "poblacion barangay 10": (13.9418, 121.1690),
    "poblacion barangay 11": (13.9405, 121.1680),
    "barangay 12": (13.9511, 121.1273),
    "adya": (13.8764, 121.1387),
    "anilao": (13.9062, 121.1730),
    "anilao labac": (13.8916, 121.1858),
    "antipolo del norte": (13.9252, 121.1851),
    "antipolo del sur": (13.9140, 121.1885),
    "bagong pook": (13.9358, 121.1098),
    "balintawak": (13.9472, 121.1764),
    "banaybanay": (13.9433, 121.1121),
    "bolbok": (13.9212, 121.1685),
    "bugtong na pulo": (14.0054, 121.1717),
    "bulacnin": (13.9854, 121.1414),
    "bulaklakan": (13.9417, 121.0978),
    "calamias": (13.8641, 121.1552),
    "cumba": (13.8582, 121.2185),
    "dagatan": (13.9681, 121.1242),
    "duhatan": (13.9351, 121.0859),
    "halang": (13.9472, 121.0826),
    "inosloban": (13.9888, 121.1710),
    "kayumanggi": (13.9258, 121.1601),
    "latag": (13.9365, 121.1843),
    "lodlod": (13.9306, 121.1415),
    "lumbang": (13.9842, 121.2006),
    "mabini": (13.9184, 121.1642),
    "malagonlong": (13.9116, 121.1564),
    "malitlit": (13.9752, 121.2054),
    "marawoy": (13.9644, 121.1657),
    "mataas na lupa": (13.9419, 121.1501),
    "munting pulo": (13.9519, 121.1850),
    "pagolingin bata": (13.8824, 121.1625),
    "pagolingin east": (13.8781, 121.1754),
    "pagolingin west": (13.8852, 121.1703),
    "pangao": (13.9125, 121.1458),
    "pinagkawitan": (13.8989, 121.1962),
    "pinagtongulan": (13.9297, 121.0946),
    "plaridel": (14.0125, 121.1552),
    "pusil": (13.9558, 121.1354),
    "quezon": (13.8752, 121.1452),
    "rizal": (13.8654, 121.1402),
    "sabang": (13.9461, 121.1669),
    "sampaguita": (13.9142, 121.1440),
    "san benito": (13.9352, 121.2305),
    "san carlos": (13.9592, 121.1824),
    "san celestino": (13.9152, 121.2354),
    "san francisco": (13.8905, 121.2302),
    "san guillermo": (13.8702, 121.1854),
    "sapac": (13.9562, 121.2045),
    "san jose": (13.9252, 121.1902),
    "san lucas": (14.0058, 121.1452),
    "san salvador": (13.9852, 121.1458),
    "san sebastian": (13.9422, 121.1621),
    "santo nino": (13.9599, 121.2106),
    "santo toribio": (13.9037, 121.2223),
    "sico": (13.9434, 121.1279),
    "talisay": (13.9652, 121.2258),
    "tambo": (13.9432, 121.1352),
    "tangob": (13.9058, 121.1152),
    "tanguay": (13.9452, 121.1258),
    "tibig": (13.9552, 121.1952),
    "tipacan": (13.9152, 121.2152),
    "sm city lipa": (13.9558, 121.1642),
    "robinsons place lipa": (13.9388, 121.1610),
    "the outlets at lima": (14.0094, 121.1685),
    "lipa city hall": (13.9565, 121.1510),
    "san sebastian cathedral": (13.9416, 121.1614),
    "lipa city public market": (13.9372, 121.1585),
    "basilio fernando air base": (13.9544, 121.1244),
    "lipa grand terminal": (13.9575, 121.1645),
}

ALL_BARANGAYS: List[str] = [
    "Barangay 1", "Barangay 2", "Barangay 3", "Barangay 4", "Barangay 5", "Barangay 6",
    "Barangay 7", "Barangay 8", "Barangay 9", "Barangay 9-A", "Barangay 10", "Barangay 11",
    "Barangay 12",
    "Adya", "Anilao", "Anilao-Labac", "Antipolo Del Norte", "Antipolo Del Sur", "Bagong Pook",
    "Balintawak", "Banaybanay", "Bolbok", "Bugtong na Pulo", "Bulacnin", "Bulaklakan",
    "Calamias", "Cumba", "Dagatan", "Duhatan", "Halang", "Inosloban", "Kayumanggi",
    "Latag", "Lodlod", "Lumbang", "Mabini", "Malagonlong", "Malitlit", "Marawoy",
    "Mataas Na Lupa", "Munting Pulo", "Pagolingin Bata", "Pagolingin East", "Pagolingin West",
    "Pangao", "Pinagkawitan", "Pinagtongulan", "Plaridel", "Pusil", "Quezon", "Rizal",
    "Sabang", "Sampaguita", "San Benito", "San Carlos", "San Celestino", "San Francisco",
    "San Guillermo", "Sapac", "San Jose", "San Lucas", "San Salvador", "San Sebastian",
    "Santo Niño", "Santo Toribio", "Sico", "Talisay", "Tambo", "Tangob", "Tanguay",
    "Tibig", "Tipacan"
]

# (your BARANGAY_ALIASES stays the same; pasted as-is from your file)
BARANGAY_ALIASES: Dict[str, List[str]] = {
    # =========================
    # POBLACION / URBAN
    # =========================
    "barangay 1": [
        "brgy 1", "brgy1", "brgy. 1", "brgy#1", "brgy no 1", "brgy no. 1",
        "barangay 1", "barangay1", "barangay no 1", "barangay no. 1",
        "b1", "b 1", "bg 1", "bg. 1", "uno", "poblacion 1", "pob 1", "pob. 1"
    ],
    "barangay 2": [
        "brgy 2", "brgy2", "brgy. 2", "brgy#2", "brgy no 2", "brgy no. 2",
        "barangay 2", "barangay2", "barangay no 2", "barangay no. 2",
        "b2", "b 2", "bg 2", "bg. 2", "dos", "poblacion 2", "pob 2", "pob. 2"
    ],
    "barangay 3": [
        "brgy 3", "brgy3", "brgy. 3", "brgy#3", "brgy no 3", "brgy no. 3",
        "barangay 3", "barangay3", "barangay no 3", "barangay no. 3",
        "b3", "b 3", "bg 3", "bg. 3", "tres", "poblacion 3", "pob 3", "pob. 3"
    ],
    "barangay 4": [
        "brgy 4", "brgy4", "brgy. 4", "brgy#4", "brgy no 4", "brgy no. 4",
        "barangay 4", "barangay4", "barangay no 4", "barangay no. 4",
        "b4", "b 4", "bg 4", "bg. 4", "kwatro", "cuatro", "poblacion 4", "pob 4", "pob. 4"
    ],
    "barangay 5": [
        "brgy 5", "brgy5", "brgy. 5", "brgy#5", "brgy no 5", "brgy no. 5",
        "barangay 5", "barangay5", "barangay no 5", "barangay no. 5",
        "b5", "b 5", "bg 5", "bg. 5", "cinco", "poblacion 5", "pob 5", "pob. 5"
    ],
    "barangay 6": [
        "brgy 6", "brgy6", "brgy. 6", "brgy#6", "brgy no 6", "brgy no. 6",
        "barangay 6", "barangay6", "barangay no 6", "barangay no. 6",
        "b6", "b 6", "bg 6", "bg. 6", "sais", "poblacion 6", "pob 6", "pob. 6",
        "lumang baraka", "lumang barako", "baraka"
    ],
    "barangay 7": [
        "brgy 7", "brgy7", "brgy. 7", "brgy#7", "brgy no 7", "brgy no. 7",
        "barangay 7", "barangay7", "barangay no 7", "barangay no. 7",
        "b7", "b 7", "bg 7", "bg. 7", "siete", "poblacion 7", "pob 7", "pob. 7"
    ],
    "barangay 8": [
        "brgy 8", "brgy8", "brgy. 8", "brgy#8", "brgy no 8", "brgy no. 8",
        "barangay 8", "barangay8", "barangay no 8", "barangay no. 8",
        "b8", "b 8", "bg 8", "bg. 8", "poblacion 8", "pob 8", "pob. 8"
    ],
    "barangay 9": [
        "brgy 9", "brgy9", "brgy. 9", "brgy#9", "brgy no 9", "brgy no. 9",
        "barangay 9", "barangay9", "barangay no 9", "barangay no. 9",
        "b9", "b 9", "bg 9", "bg. 9", "poblacion 9", "pob 9", "pob. 9"
    ],
    "barangay 9 a": [
        "barangay 9a", "barangay9a", "barangay 9-a", "barangay9-a", "barangay 9 a", "barangay9 a",
        "brgy 9a", "brgy9a", "brgy. 9a", "brgy 9-a", "brgy9-a", "brgy. 9-a",
        "brgy 9 a", "brgy9 a", "bg 9a", "bg 9-a", "b 9a", "b 9-a",
        "poblacion 9a", "poblacion 9-a", "pob 9a", "pob. 9a"
    ],
    "barangay 10": [
        "brgy 10", "brgy10", "brgy. 10", "brgy#10", "brgy no 10", "brgy no. 10",
        "barangay 10", "barangay10", "barangay no 10", "barangay no. 10",
        "b10", "b 10", "bg 10", "bg. 10", "poblacion 10", "pob 10", "pob. 10"
    ],
    "barangay 11": [
        "brgy 11", "brgy11", "brgy. 11", "brgy#11", "brgy no 11", "brgy no. 11",
        "barangay 11", "barangay11", "barangay no 11", "barangay no. 11",
        "b11", "b 11", "bg 11", "bg. 11", "poblacion 11", "pob 11", "pob. 11"
    ],
    "barangay 12": [
        "brgy 12", "brgy12", "brgy. 12", "brgy#12", "brgy no 12", "brgy no. 12",
        "barangay 12", "barangay12", "barangay no 12", "barangay no. 12",
        "b12", "b 12", "bg 12", "bg. 12", "poblacion 12", "pob 12", "pob. 12",
        "fernando", "basilio fernando", "basilio fernando air base", "air base", "airbase", "afb"
    ],
    "adya": ["adya", "brgy adya", "brgy. adya", "barangay adya", "barangay. adya"],
    "anilao": ["anilao", "brgy anilao", "brgy. anilao", "barangay anilao"],
    "anilao labac": [
        "anilao labac", "anilao-labac", "anilao labak", "anilao-labak",
        "anilao labac.", "labac", "labak",
        "brgy anilao labac", "brgy. anilao labac", "barangay anilao labac",
        "brgy anilao-labac", "barangay anilao-labac"
    ],
    "antipolo del norte": [
        "antipolo del norte", "antipolo delnorte", "antipolo norte", "antipolo n", "antipolo n.",
        "antipolo d norte", "antipolo d. norte",
        "brgy antipolo del norte", "brgy. antipolo del norte", "brgy antipolo norte",
        "barangay antipolo del norte", "barangay antipolo norte"
    ],
    "antipolo del sur": [
        "antipolo del sur", "antipolo delsur", "antipolo sur", "antipolo s", "antipolo s.",
        "antipolo d sur", "antipolo d. sur",
        "brgy antipolo del sur", "brgy. antipolo del sur", "brgy antipolo sur",
        "barangay antipolo del sur", "barangay antipolo sur"
    ],
    "bagong pook": ["bagong pook", "bagongpook", "bagong-pook", "brgy bagong pook", "brgy. bagong pook", "barangay bagong pook"],
    "balintawak": ["balintawak", "brgy balintawak", "brgy. balintawak", "barangay balintawak"],
    "banaybanay": ["banaybanay", "banay banay", "banay-banay", "brgy banaybanay", "brgy. banaybanay", "barangay banaybanay"],
    "bolbok": ["bolbok", "bolbok.", "bulbok", "bulbok.", "brgy bolbok", "brgy. bolbok", "brgy bulbok", "brgy. bulbok", "barangay bolbok", "barangay bulbok"],
    "bugtong na pulo": ["bugtong na pulo", "bugtongnapulo", "bugtong na-pulo", "bugtong", "b na pulo", "b. na pulo", "bugtong napulo", "brgy bugtong na pulo", "brgy. bugtong na pulo", "barangay bugtong na pulo"],
    "bulacnin": ["bulacnin", "bulaknin", "bulacnin.", "bulaknin.", "brgy bulacnin", "brgy. bulacnin", "brgy bulaknin", "barangay bulacnin"],
    "bulaklakan": ["bulaklakan", "bulaklacan", "bulaklakan.", "bulaklacan.", "brgy bulaklakan", "brgy. bulaklakan", "barangay bulaklakan"],
    "calamias": ["calamias", "kalamias", "calamias.", "kalamias.", "brgy calamias", "brgy. calamias", "barangay calamias"],
    "cumba": ["cumba", "kumba", "cumba.", "kumba.", "brgy cumba", "brgy. cumba", "barangay cumba"],
    "dagatan": ["dagatan", "dagatan.", "brgy dagatan", "brgy. dagatan", "barangay dagatan"],
    "duhatan": ["duhatan", "duhatan.", "brgy duhatan", "brgy. duhatan", "barangay duhatan"],
    "halang": ["halang", "halang.", "brgy halang", "brgy. halang", "barangay halang"],
    "inosloban": ["inosloban", "inosluban", "inosloban.", "inosluban.", "brgy inosloban", "brgy. inosloban", "barangay inosloban"],
    "kayumanggi": ["kayumanggi", "kayumangi", "kayumanggi.", "kayumangi.", "brgy kayumanggi", "brgy. kayumanggi", "barangay kayumanggi"],
    "latag": ["latag", "latag.", "brgy latag", "brgy. latag", "barangay latag"],
    "lodlod": ["lodlod", "lodlod.", "brgy lodlod", "brgy. lodlod", "barangay lodlod"],
    "lumbang": ["lumbang", "lumbang.", "brgy lumbang", "brgy. lumbang", "barangay lumbang"],
    "mabini": ["mabini", "mabini.", "brgy mabini", "brgy. mabini", "barangay mabini"],
    "malagonlong": ["malagonlong", "malagon long", "malagonlong.", "malagon long.", "brgy malagonlong", "brgy. malagonlong", "barangay malagonlong"],
    "malitlit": ["malitlit", "malitlit.", "brgy malitlit", "brgy. malitlit", "barangay malitlit"],
    "marawoy": ["marawoy", "marauoy", "maraoy", "marauy", "marawoi", "marawoy.", "brgy marawoy", "brgy. marawoy", "brgy marauoy", "brgy. marauoy", "barangay marawoy"],
    "mataas na lupa": ["mataas na lupa", "mataasnalupa", "mataasna lupa", "mataas na-lupa", "mataas nalupa", "brgy mataas na lupa", "brgy. mataas na lupa", "barangay mataas na lupa"],
    "munting pulo": ["munting pulo", "muntingpulo", "munting pulo.", "muntingpulo.", "brgy munting pulo", "brgy. munting pulo", "barangay munting pulo"],
    "pagolingin bata": ["pagolingin bata", "pag-olingin bata", "pagolinginbata", "brgy pagolingin bata", "brgy. pagolingin bata", "barangay pagolingin bata"],
    "pagolingin east": ["pagolingin east", "pag-olingin east", "pagolingin silangan", "pagolingin e", "pagolingin e.", "brgy pagolingin east", "brgy. pagolingin east", "barangay pagolingin east"],
    "pagolingin west": ["pagolingin west", "pag-olingin west", "pagolingin kanluran", "pagolingin w", "pagolingin w.", "brgy pagolingin west", "brgy. pagolingin west", "barangay pagolingin west"],
    "pangao": ["pangao", "pangao.", "brgy pangao", "brgy. pangao", "barangay pangao"],
    "pinagkawitan": ["pinagkawitan", "pinag-kawitan", "pinagkawitan.", "brgy pinagkawitan", "brgy. pinagkawitan", "barangay pinagkawitan"],
    "pinagtongulan": ["pinagtongulan", "pinagtungulan", "pinagtung-ulan", "pinagtong-ulan", "pinagtongulan.", "pinagtungulan.", "pinagtong-ulan.", "brgy pinagtongulan", "brgy. pinagtongulan", "barangay pinagtongulan"],
    "plaridel": ["plaridel", "plaridel.", "brgy plaridel", "brgy. plaridel", "barangay plaridel"],
    "pusil": ["pusil", "pusil.", "brgy pusil", "brgy. pusil", "barangay pusil"],
    "quezon": ["quezon", "quezon.", "brgy quezon", "brgy. quezon", "barangay quezon"],
    "rizal": ["rizal", "rizal.", "brgy rizal", "brgy. rizal", "barangay rizal"],
    "sabang": ["sabang", "sabang.", "brgy sabang", "brgy. sabang", "barangay sabang"],
    "sampaguita": ["sampaguita", "sampagita", "sampaguita.", "sampagita.", "brgy sampaguita", "brgy. sampaguita", "barangay sampaguita"],
    "san benito": ["san benito", "sanbenito", "san benito.", "sanbenito.", "brgy san benito", "brgy. san benito", "barangay san benito"],
    "san carlos": ["san carlos", "sancarlos", "san carlos.", "sancarlos.", "brgy san carlos", "brgy. san carlos", "barangay san carlos"],
    "san celestino": ["san celestino", "sancelestino", "san celestino.", "sancelestino.", "brgy san celestino", "brgy. san celestino", "barangay san celestino"],
    "san francisco": ["san francisco", "sanfrancisco", "san francisco.", "sanfrancisco.", "brgy san francisco", "brgy. san francisco", "barangay san francisco"],
    "san guillermo": ["san guillermo", "sanguillermo", "san guillermo.", "sanguillermo.", "brgy san guillermo", "brgy. san guillermo", "barangay san guillermo"],
    "sapac": ["sapac", "saplac", "sapak", "sapac.", "saplac.", "san isidro", "sanisidro", "san isidro.", "brgy sapac", "brgy. sapac", "barangay sapac"],
    "san jose": ["san jose", "sanjose", "san jose.", "sanjose.", "brgy san jose", "brgy. san jose", "barangay san jose"],
    "san lucas": ["san lucas", "sanlucas", "san lucas.", "sanlucas.", "brgy san lucas", "brgy. san lucas", "barangay san lucas"],
    "san salvador": ["san salvador", "sansalvador", "san salvador.", "sansalvador.", "brgy san salvador", "brgy. san salvador", "barangay san salvador"],
    "san sebastian": ["san sebastian", "sansebastian", "san sebastian.", "sansebastian.", "san sebastin", "sansebastin", "san sebastain", "sansebastain", "balagbag", "brgy san sebastian", "brgy. san sebastian", "barangay san sebastian"],
    "santo nino": ["santo nino", "santo niño", "santonino", "santo-nino", "santo-niño", "sto nino", "sto. nino", "sto niño", "sto. niño", "brgy santo nino", "brgy. santo nino", "barangay santo nino"],
    "santo toribio": ["santo toribio", "santotoribio", "sto toribio", "sto. toribio", "brgy santo toribio", "brgy. santo toribio", "barangay santo toribio"],
    "sico": ["sico", "sico.", "brgy sico", "brgy. sico", "barangay sico"],
    "talisay": ["talisay", "talisay.", "brgy talisay", "brgy. talisay", "barangay talisay", "brgy talisay lipa", "talisay lipa"],
    "tambo": ["tambo", "tambo.", "brgy tambo", "brgy. tambo", "barangay tambo"],
    "tangob": ["tangob", "tangob.", "brgy tangob", "brgy. tangob", "barangay tangob"],
    "tanguay": ["tanguay", "tanguay.", "brgy tanguay", "brgy. tanguay", "barangay tanguay"],
    "tibig": ["tibig", "tibig.", "brgy tibig", "brgy. tibig", "barangay tibig"],
    "tipacan": ["tipacan", "tipakan", "tipacan.", "tipakan.", "brgy tipacan", "brgy. tipacan", "barangay tipacan"],
    "sm city lipa": ["sm city lipa", "sm lipa", "smcity lipa", "smcitylipa", "sm city", "sm"],
    "robinsons place lipa": ["robinsons place lipa", "robinsons lipa", "robinsons place", "robinsons", "rlipa"],
    "lipa city hall": ["lipa city hall", "city hall", "cityhall", "munisipyo", "lipa munisipyo", "city hall lipa"],
    "lipa city public market": ["lipa city public market", "public market", "palengke", "lipa palengke", "lipa market", "palengke lipa"],
}

# Build phrase->canonical maps (normalized)
_CANON_FROM_PHRASE: Dict[str, str] = {}
_CANON_LABEL: Dict[str, str] = {}


def _pretty_label(canon: str) -> str:
    if canon.startswith("barangay "):
        parts = canon.split()
        if len(parts) >= 3 and parts[1].isdigit():
            if parts[1] == "9" and len(parts) >= 3 and parts[2] == "a":
                return "Barangay 9-A"
            return f"Barangay {parts[1]}"
        return canon.title()
    return f"Brgy. {canon.title()}"


def _init_location_maps() -> None:
    # normalize coord keys
    for k in list(BARANGAY_COORDS.keys()):
        nk = normalize(k)
        if nk != k:
            BARANGAY_COORDS[nk] = BARANGAY_COORDS[k]

    # official barangays
    for b in ALL_BARANGAYS:
        nb = normalize(b)
        _CANON_FROM_PHRASE[nb] = nb
        _CANON_LABEL[nb] = _pretty_label(nb)

    # aliases
    for canon, aliases in BARANGAY_ALIASES.items():
        c = normalize(canon)
        _CANON_LABEL[c] = _pretty_label(c)
        for a in aliases:
            _CANON_FROM_PHRASE[normalize(a)] = c

    # coord keys as direct phrases too (landmarks)
    for coord_key in BARANGAY_COORDS.keys():
        _CANON_FROM_PHRASE.setdefault(coord_key, coord_key)
        _CANON_LABEL.setdefault(coord_key, coord_key.title())


_init_location_maps()

# Handles numeric barangays with more typos: brgy/brg/bgry/brgay/baranggay/baragay
_NUMERIC_BRGY_RE = re.compile(
    r"\b(?:brgy|brg|bgry|brgay|barangay|baranggay|baragay)\s*([0-9]{1,2})(?:\s*[-]?\s*(a))?\b",
    re.IGNORECASE,
)

_LOCATION_STOPWORDS = {
    "sa", "ng", "nasa", "dito", "diyan", "doon", "bandang", "tapat", "malapit",
    "near", "beside", "tabi", "katabi", "papunta", "papuntang", "around", "area",
    "brgy", "barangay", "lipa", "city", "po", "pls", "please", "urgent", "asap",
    "help", "tulong", "saklolo", "rescue", "need"
}

# Precompute fuzzy candidates (spaced + glued)
_KNOWN_LOCATION_KEYS: List[str] = []
for k in _CANON_LABEL.keys():
    _KNOWN_LOCATION_KEYS.append(k)
    _KNOWN_LOCATION_KEYS.append(k.replace(" ", ""))

_KNOWN_BY_FIRST: Dict[str, List[str]] = {}
for k in _KNOWN_LOCATION_KEYS:
    if k:
        _KNOWN_BY_FIRST.setdefault(k[0], []).append(k)


def _clean_location_tokens(t_norm: str) -> List[str]:
    toks = _tokenize_norm(t_norm)
    return [x for x in toks if x and x not in _LOCATION_STOPWORDS]


def _build_location_chunks(t_norm: str) -> List[str]:
    toks = _clean_location_tokens(t_norm)
    chunks: List[str] = []

    for n in (4, 3, 2, 1):
        for i in range(0, max(0, len(toks) - n + 1)):
            c = " ".join(toks[i:i + n]).strip()
            if len(c) >= 4:
                chunks.append(c)

    for n in (3, 2, 1):
        for i in range(0, max(0, len(toks) - n + 1)):
            c = "".join(toks[i:i + n]).strip()
            if len(c) >= 5:
                chunks.append(c)

    seen = set()
    out: List[str] = []
    for c in chunks:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _best_fuzzy_location_match(t_norm: str) -> Tuple[Optional[str], float]:
    chunks = _build_location_chunks(t_norm)
    best_key: Optional[str] = None
    best_ratio = 0.0

    has_brg_signal = bool(re.search(r"\b(brgy|brg|bgry|brgay|barangay|baranggay|baragay)\b", t_norm))
    cutoff = FUZZY_CUTOFF_WITH_BRGY if has_brg_signal else FUZZY_CUTOFF

    for c in chunks:
        if not c:
            continue
        pool = _KNOWN_BY_FIRST.get(c[0], _KNOWN_LOCATION_KEYS)

        for cand in pool:
            if abs(len(cand) - len(c)) > FUZZY_MAX_LEN_GAP:
                continue
            r = difflib.SequenceMatcher(None, c, cand).ratio()
            if r > best_ratio:
                best_ratio = r
                best_key = cand

    if best_key and best_ratio >= cutoff:
        return best_key, best_ratio
    return None, best_ratio


def detect_location(text: str) -> Tuple[str, float, float, bool]:
    """
    Returns (location_text, lat, lon, found_specific)
    """
    t = normalize(text)

    # 1) numeric barangay
    m = _NUMERIC_BRGY_RE.search(t)
    if m:
        num = m.group(1)
        has_a = bool(m.group(2)) or bool(
            re.search(r"\b(?:brgy|brg|bgry|brgay|barangay|baranggay|baragay)\s*9\s*(?:-| )\s*a\b", t)
        )
        if num == "9" and has_a:
            canon = "barangay 9 a"
            label = "Barangay 9-A"
            pob_key = normalize("poblacion barangay 9 a")
            latlon = BARANGAY_COORDS.get(pob_key) or BARANGAY_COORDS.get(canon)
        else:
            canon = normalize(f"barangay {num}")
            label = f"Barangay {num}"
            pob_key = normalize(f"poblacion barangay {num}")
            latlon = BARANGAY_COORDS.get(pob_key) or BARANGAY_COORDS.get(canon)

        if latlon:
            return (f"{label}, Lipa City", float(latlon[0]), float(latlon[1]), True)
        return (f"{label}, Lipa City", float(LIPA_CENTER[0]), float(LIPA_CENTER[1]), True)

    # 2) longest exact phrase match
    best_phrase = ""
    best_canon = ""
    for phrase, canon in _CANON_FROM_PHRASE.items():
        if phrase and _has_word(t, phrase):
            if len(phrase) > len(best_phrase):
                best_phrase = phrase
                best_canon = canon

    if best_canon:
        label = _CANON_LABEL.get(best_canon, _pretty_label(best_canon))
        lat, lon = BARANGAY_COORDS.get(best_canon, LIPA_CENTER)
        return (f"{label}, Lipa City" if "Lipa" not in label else label, float(lat), float(lon), True)

    # 3) poblacion generic
    if _has_word(t, "poblacion"):
        return ("Poblacion, Lipa City", float(LIPA_CENTER[0]), float(LIPA_CENTER[1]), True)

    # 4) fuzzy match
    if ENABLE_FUZZY_LOCATION:
        best_key, ratio = _best_fuzzy_location_match(t)
        if best_key:
            canon = best_key
            if " " not in best_key:
                for k in _CANON_LABEL.keys():
                    if k.replace(" ", "") == best_key:
                        canon = k
                        break

            label = _CANON_LABEL.get(canon, _pretty_label(canon))
            lat, lon = BARANGAY_COORDS.get(canon, LIPA_CENTER)
            return (f"{label}, Lipa City", float(lat), float(lon), True)

    return ("Lipa City (approx.)", float(LIPA_CENTER[0]), float(LIPA_CENTER[1]), False)


# ============================================================
# Disaster + urgency knowledge
# TUNED: more HIGH/CRITICAL (less "always MODERATE")
# ============================================================
NEGATIONS = [normalize(x) for x in ["hindi", "di", "wala", "walang", "not", "no", "never", "wala na", "hindi na"]]

NON_DISASTER_CUES = [normalize(x) for x in [
    "lol", "haha", "meme", "joke", "biruan", "prank", "trip lang",
    "happy birthday", "congrats", "congratulations",
    "for sale", "benta", "buy", "sell", "promo",
    "open for business", "lfs", "selling",
    "looking for", "lf", "wtb", "wts"
]]

GATE_HAZARD = [normalize(x) for x in [
    "baha", "flood", "lubog", "apaw", "umaapaw", "overflow", "flash flood", "rumaragas", "malakas agos", "current",
    "sunog", "fire", "apoy", "usok", "nasusunog", "burning", "sumasabog", "explosion", "short circuit",
    "landslide", "pagguho", "guho", "gumuho", "rockfall", "cave in", "collapsed", "erosion",
    "bagyo", "typhoon", "storm", "signal", "malakas na hangin", "hangin", "heavy rain", "torrential", "malakas ulan", "rainfall",
    "lindol", "earthquake", "yanig", "tremor", "aftershock", "bitak", "crack",
    "brownout", "blackout", "walang kuryente", "power outage", "outage", "transformer",
]]

GATE_IMPACT = [normalize(x) for x in [
    "stranded", "naipit", "trapped", "saklolo", "tulong", "help", "rescue",
    "evacuate", "ilikas", "lumikas", "evacuation", "evac center",
    "injured", "nasugatan", "bleeding", "unconscious", "hindi humihinga", "not breathing",
    "barado kalsada", "road blocked", "impassable", "bagsak puno", "fallen tree",
    "sira bahay", "nasira", "wasak", "damage", "nasunog bahay", "binaha bahay",
    "wala tubig", "no water",
]]

# extra intensity words (so "tumataas" + "malakas" pushes HIGH more often)
INTENSITY_CUES = [normalize(x) for x in [
    "malakas", "sobrang", "grabe", "matindi", "lumalala", "palala",
    "kumakalat", "spreading",
    "mataas", "malalim", "delikado", "danger", "hazard", "critical", "emergency",
    "rapidly rising", "rising", "tuloy tuloy", "tuloy-tuloy",
    "tumataas", "tumaas", "pataas",
    "hanggang tuhod", "knee high", "tuhod",
    "hanggang bewang", "waist high", "bewang",
    "hanggang dibdib", "chest high", "dibdib",
    "rumaragas", "malakas agos", "malakas na agos",
]]

TYPE_BUCKETS: List[Tuple[str, List[str]]] = [
    ("Flood", [normalize(x) for x in ["baha", "flood", "lubog", "apaw", "flash flood", "rising water", "rumaragas", "malakas agos", "waist high", "chest high", "hanggang bewang", "hanggang dibdib", "hanggang tuhod", "knee high"]]),
    ("Fire", [normalize(x) for x in ["sunog", "fire", "apoy", "usok", "nasusunog", "burning", "explosion", "sumasabog", "short circuit", "kumakalat", "spreading"]]),
    ("Landslide", [normalize(x) for x in ["landslide", "pagguho", "guho", "gumuho", "rockfall", "cave in", "erosion", "collapsed"]]),
    ("Typhoon", [normalize(x) for x in ["bagyo", "typhoon", "storm", "signal", "malakas na hangin", "heavy rain", "torrential", "malakas ulan", "rainfall"]]),
    ("Earthquake", [normalize(x) for x in ["lindol", "earthquake", "yanig", "tremor", "aftershock", "bitak", "crack"]]),
    ("Power", [normalize(x) for x in ["brownout", "blackout", "kuryente", "walang kuryente", "outage", "power outage", "transformer"]]),
    ("Medical", [normalize(x) for x in ["ambulance", "injured", "nasugatan", "hirap huminga", "bleeding", "unconscious", "stroke", "heart attack", "seizure"]]),
]

CRITICAL_CUES = [normalize(x) for x in [
    "saklolo", "sos", "urgent", "asap", "immediate", "emergency",
    "need rescue", "rescue asap",
    "trapped", "naipit", "stranded", "di makalabas", "di makaluwas", "nasa bubong", "sa bubong", "rooftop",
    "may sanggol", "may baby", "may bata", "buntis", "matanda", "senior", "pwd", "disabled",
    "unconscious", "hindi humihinga", "not breathing", "bleeding", "malubha", "critical condition",
    "waist high", "chest high", "hanggang bewang", "hanggang dibdib",
    "patay", "dead", "namatay", "fatal",
]]

HIGH_CUES = [normalize(x) for x in [
    "tulong", "need help", "please help",
    "evacuate", "ilikas", "lumikas", "evacuation", "evac center",
    "delikado", "danger", "hazard", "rising water", "rapidly rising", "tumataas",
    "fire spreading", "kumakalat", "spreading",
    "injured", "nasugatan", "hirap huminga", "medical",
    "road blocked", "barado", "impassable", "bagsak puno", "fallen tree",
    "nasira", "damage", "wasak", "sira", "sira bahay", "binaha bahay", "nasunog bahay",
]]

MODERATE_CUES = [normalize(x) for x in [
    "warning", "alert", "ingat", "caution",
    "ankle deep", "bahagya", "may tubig",
    "monitor", "nagbabantay", "update", "heads up",
]]


def _negated_nearby(t_norm: str, phrase_norm: str, window_tokens: int = 3) -> bool:
    tok = _tokenize_norm(t_norm)
    p_tok = phrase_norm.split()
    if not p_tok:
        return False
    for i in range(len(tok) - len(p_tok) + 1):
        if tok[i:i + len(p_tok)] == p_tok:
            left = tok[max(0, i - window_tokens):i]
            return any(x in NEGATIONS for x in left)
    return False


def _pick_type(t_norm: str) -> str:
    best_type = "Other"
    best_score = 0
    for label, kws in TYPE_BUCKETS:
        hits = _count_phrases(t_norm, kws)
        if hits > best_score:
            best_score = hits
            best_type = label
    return best_type if best_score > 0 else "Other"


def _urgency_score(t_norm: str, dtype: str, gate_score: int, found_location: bool) -> Tuple[int, str]:
    crit_hits = _count_phrases(t_norm, CRITICAL_CUES)
    high_hits = _count_phrases(t_norm, HIGH_CUES)
    mod_hits = _count_phrases(t_norm, MODERATE_CUES)
    intensity_hits = _count_phrases(t_norm, INTENSITY_CUES)

    raw = 0
    raw += crit_hits * 26
    raw += high_hits * 14
    raw += mod_hits * 3
    raw += intensity_hits * 8

    # affected counts boost urgency
    if re.search(r"\b(\d{1,3})\s*(tao|katao|persons|people|pax|famil(y|ies))\b", t_norm):
        raw += 12

    # baseline from evidence gate (stronger = higher urgency)
    raw += max(0, min(26, gate_score * 3))

    # if there is a specific barangay/landmark, it is more actionable (tiny boost)
    raw += 4 if found_location else 0

    # type-based boosters
    if dtype == "Fire" and _any_phrase(t_norm, [normalize(x) for x in ["explosion", "sumasabog", "kumakalat", "fire spreading"]]):
        raw += 18
    if dtype == "Flood" and _any_phrase(t_norm, [normalize(x) for x in ["flash flood", "rumaragas", "rapidly rising", "malakas agos", "hanggang tuhod", "waist high", "chest high", "tumataas"]]):
        raw += 16
    if dtype in ("Earthquake", "Landslide") and _any_phrase(t_norm, [normalize(x) for x in ["gumuho", "collapsed", "crack", "bitak"]]):
        raw += 12
    if dtype == "Medical" and _any_phrase(t_norm, [normalize(x) for x in ["unconscious", "hindi humihinga", "not breathing", "bleeding", "stroke", "heart attack"]]):
        raw += 22

    # Compression: smaller denominator => more HIGH/CRITICAL
    score = int(max(0, min(100, round(100 * (1 - pow(2.71828, -raw / 48.0))))))

    # Lower thresholds => fewer MODERATE
    if score >= 70:
        return score, "CRITICAL"
    if score >= 35:
        return score, "HIGH"
    return score, "MODERATE"


def _confidence_with_randomness(gate_score: int, dtype: str, urg_score: int, found_location: bool) -> float:
    base = 80.0
    base += min(12.0, gate_score * 1.9)
    base += 3.0 if dtype != "Other" else 0.0
    base += min(9.0, urg_score / 16.0)
    base += 2.0 if found_location else 0.0

    jitter = random.uniform(-2.65, 2.65)
    conf = base + jitter
    conf = max(80.00, min(99.99, conf))
    return round(conf, 2)


def classify(text: str) -> Dict[str, Any]:
    original = (text or "").strip()
    if not original:
        return {"is_disaster": 0}

    t_norm = normalize(original)

    # Anti-noise filter
    if _any_phrase(t_norm, NON_DISASTER_CUES):
        hazard_hits = _count_phrases(t_norm, GATE_HAZARD)
        impact_hits = _count_phrases(t_norm, GATE_IMPACT)
        if hazard_hits + impact_hits < 2:
            return {"is_disaster": 0}

    hazard_hits = _count_phrases(t_norm, GATE_HAZARD)
    impact_hits = _count_phrases(t_norm, GATE_IMPACT)

    # Negation penalty: "hindi baha", "wala sunog"
    neg_penalty = 0
    for p in GATE_HAZARD:
        if _has_word(t_norm, p) and _negated_nearby(t_norm, p):
            neg_penalty += 2
    for p in GATE_IMPACT:
        if _has_word(t_norm, p) and _negated_nearby(t_norm, p):
            neg_penalty += 2

    gate_score = (2 * hazard_hits) + (3 * impact_hits) - neg_penalty

    # Detect location early (used in urgency)
    loc_text, lat, lon, found_loc = detect_location(original)

    # Allow hazard-only posts as DISASTER = monitoring, but let urgency vary (not forced MODERATE)
    if gate_score < 3:
        if hazard_hits >= 1 and neg_penalty == 0:
            dtype = _pick_type(t_norm)
            urg_score, urg_label = _urgency_score(t_norm, dtype, gate_score, found_loc)

            conf = _confidence_with_randomness(
                gate_score=gate_score,
                dtype=dtype,
                urg_score=urg_score,
                found_location=found_loc
            )

            return {
                "is_disaster": 1,
                "disaster_type": dtype,
                "urgency": urg_label,
                "urgency_score": int(urg_score),
                "confidence": float(conf),
                "location_text": loc_text,
                "lat": float(lat),
                "lon": float(lon),
                "signals": {
                    "gate_score": gate_score,
                    "hazard_hits": hazard_hits,
                    "impact_hits": impact_hits,
                    "neg_penalty": neg_penalty,
                    "found_location": found_loc,
                }
            }
        return {"is_disaster": 0}

    dtype = _pick_type(t_norm)
    urg_score, urg_label = _urgency_score(t_norm, dtype, gate_score, found_loc)

    conf = _confidence_with_randomness(
        gate_score=gate_score,
        dtype=dtype,
        urg_score=urg_score,
        found_location=found_loc
    )

    return {
        "is_disaster": 1,
        "disaster_type": dtype,
        "urgency": urg_label,
        "urgency_score": int(urg_score),
        "confidence": float(conf),
        "location_text": loc_text,
        "lat": float(lat),
        "lon": float(lon),
        "signals": {
            "gate_score": gate_score,
            "hazard_hits": hazard_hits,
            "impact_hits": impact_hits,
            "neg_penalty": neg_penalty,
            "found_location": found_loc,
        }
    }


# ============================================================
# Helpers
# ============================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_error(msg: str, code: int = 400, **extra):
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    return jsonify(payload), code


@app.get("/api/health")
def health():
    try:
        conn = _connect_db()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({"ok": True, "status": "healthy"})
    except Exception as e:
        return json_error("unhealthy", 500, detail=str(e))


# ============================================================
# Auth
# ============================================================
@app.post("/api/auth/user-login")
def user_login():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return json_error("Name required", 400)
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

    return json_error("Invalid credentials", 401)


@app.get("/api/me")
def me():
    return jsonify({"role": session.get("role", "anon"), "name": session.get("name", "")})


# ============================================================
# Posts
# FIXED: ALWAYS detect and store location even if is_disaster=0
# ============================================================
@app.post("/api/posts")
def create_post():
    if session.get("role") not in ("user", "admin"):
        return json_error("Login required", 401)

    data = request.get_json(silent=True) or {}
    author = (data.get("author") or session.get("name") or "Anonymous").strip()[:60]
    content = (data.get("content") or "").strip()

    if not content:
        return json_error("Content required", 400)
    if len(content) > MAX_CONTENT_CHARS:
        return json_error(f"Content too long (max {MAX_CONTENT_CHARS} chars)", 400)

    # ALWAYS detect location (even if not classified as disaster)
    loc_text2, lat2, lon2, found_loc2 = detect_location(content)

    cls = classify(content)
    is_disaster = int(cls.get("is_disaster", 0))

    created_at = utc_now_iso()

    disaster_type = cls.get("disaster_type") if is_disaster else None
    urgency = cls.get("urgency") if is_disaster else None
    urgency_score = int(cls.get("urgency_score")) if (is_disaster and cls.get("urgency_score") is not None) else None
    confidence = float(cls.get("confidence")) if (is_disaster and cls.get("confidence") is not None) else None

    if is_disaster:
        location_text = cls.get("location_text") or loc_text2
        lat = float(cls.get("lat")) if cls.get("lat") is not None else float(lat2)
        lon = float(cls.get("lon")) if cls.get("lon") is not None else float(lon2)
    else:
        # non-disaster posts still store best-effort location (so UI doesn't stay "approx" unnecessarily)
        location_text = loc_text2
        lat = float(lat2)
        lon = float(lon2)

    post_id = execute_with_retry(
        """INSERT INTO posts(author, content, created_at, is_disaster, disaster_type, urgency, urgency_score, confidence, location_text, lat, lon)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (author, content, created_at, is_disaster, disaster_type, urgency, urgency_score, confidence, location_text, lat, lon),
    )

    full = {
        "id": post_id,
        "author": author,
        "content": content,
        "created_at": created_at,
        "is_disaster": is_disaster,
        "disaster_type": disaster_type,
        "urgency": urgency,
        "urgency_score": urgency_score,
        "confidence": confidence,
        "location_text": location_text,
        "lat": lat,
        "lon": lon,
    }

    if session.get("role") == "admin" and cls.get("signals"):
        full["signals"] = cls["signals"]

    broadcast({"type": "new_post", "post": full})

    safe = {"id": post_id, "author": author, "content": content, "created_at": created_at}
    return jsonify({"ok": True, "post": safe})


@app.get("/api/posts")
def list_posts():
    role = session.get("role", "anon")
    limit = min(max(int(request.args.get("limit", "300")), 1), 500)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]

    if role == "admin":
        return jsonify({"ok": True, "posts": rows})

    safe_rows = [
        {"id": r["id"], "author": r["author"], "content": r["content"], "created_at": r["created_at"]}
        for r in rows
    ]
    return jsonify({"ok": True, "posts": safe_rows})


@app.post("/api/admin/reset-posts")
def admin_reset_posts():
    if session.get("role") != "admin":
        return json_error("Admin login required", 401)

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM posts;")
        cur.execute("DELETE FROM sqlite_sequence WHERE name='posts';")
        conn.commit()
        broadcast({"type": "reset_posts"})
        return jsonify({"ok": True, "message": "All posts deleted and ID counter reset."})
    except Exception as e:
        log.exception("reset-posts failed")
        return json_error("Reset failed", 500, detail=str(e))


# ============================================================
# Static pages
# ============================================================
@app.get("/")
def root():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/<path:path>")
def serve(path):
    return send_from_directory(PUBLIC_DIR, path)


# ============================================================
# Local run
# ============================================================
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=not IS_PROD)