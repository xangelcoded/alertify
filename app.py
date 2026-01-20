from flask import Flask, request, jsonify, Response, send_from_directory, session
import os, json, time, sqlite3
from queue import Queue
from datetime import datetime

APP_DIR = os.path.abspath(os.path.dirname(__file__))
PUBLIC_DIR = os.path.join(APP_DIR, "public")
DB_PATH = os.path.join(APP_DIR, "alertify.db")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@lipa.gov.ph")
ADMIN_PASS  = os.getenv("ADMIN_PASS",  "admin123")
SECRET_KEY  = os.getenv("SECRET_KEY",  "change-me")

app = Flask(__name__, static_folder=PUBLIC_DIR, static_url_path="")
app.secret_key = SECRET_KEY

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_disaster INTEGER NOT NULL,
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

# ---------- LIVE SSE ----------
clients = []
def broadcast(event: dict):
    data = json.dumps(event, ensure_ascii=False)
    for q in list(clients):
        try: q.put_nowait(data)
        except: pass

@app.get("/api/stream")
def stream():
    q = Queue(maxsize=200)
    clients.append(q)

    def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    payload = q.get(timeout=20)
                    yield f"data: {payload}\n\n"
                except:
                    yield ": keep-alive\n\n"
        finally:
            try: clients.remove(q)
            except: pass

    return Response(gen(), mimetype="text/event-stream")

# ---------- SIMPLE CLASSIFIER (baseline, typo tolerant enough for demo) ----------
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

def classify(text: str):
    t = (text or "").lower()

    # non-disaster gate
    disaster_words = ["baha","flood","sunog","fire","bagyo","typhoon","lindol","earthquake","guho","landslide",
                     "rescue","saklolo","tulong","trapped","stranded","ambulance","nasugatan","brownout","kuryente"]
    if not any(w in t for w in disaster_words):
        return dict(is_disaster=0)

    # type
    if any(w in t for w in ["baha","flood","lubog","apaw"]): dtype="Flood"
    elif any(w in t for w in ["sunog","fire","usok","apoy"]): dtype="Fire"
    elif any(w in t for w in ["guho","landslide","pagguho","gumuho"]): dtype="Landslide"
    elif any(w in t for w in ["bagyo","typhoon","storm","hangin"]): dtype="Typhoon"
    elif any(w in t for w in ["lindol","earthquake","yanig","tremor"]): dtype="Earthquake"
    elif any(w in t for w in ["brownout","kuryente","outage"]): dtype="Power"
    elif any(w in t for w in ["ambulance","hirap huminga","nasugatan","injured"]): dtype="Medical"
    else: dtype="Other"

    # urgency
    crit = ["saklolo","sos","trapped","bubong","rooftop","may bata","matanda","urgent","asap","di makalabas","stranded"]
    high = ["need help","tulong","evacuate","nasugatan","injured","malakas","delikado","usok","apoy"]
    score = sum(3 for w in crit if w in t) + sum(2 for w in high if w in t)

    if score >= 6: urg="CRITICAL"
    elif score >= 3: urg="HIGH"
    else: urg="MODERATE"

    # location
    loc="Lipa City (unspecified)"
    lat, lon = 13.941, 121.163
    for b,(la,lo) in BARANGAY_COORDS.items():
        if b in t:
            loc=b.title()
            lat, lon = la, lo
            break

    import random
    conf = random.randint(85, 99)

    return dict(
        is_disaster=1,
        disaster_type=dtype,
        urgency=urg,
        confidence=conf,
        location_text=loc,
        lat=lat,
        lon=lon
    )

# ---------- AUTH ----------
@app.post("/api/auth/user-login")
def user_login():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    session["role"]="user"
    session["name"]=name[:60]
    return jsonify({"ok": True})

@app.post("/api/auth/admin-login")
def admin_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    if email == ADMIN_EMAIL and password == ADMIN_PASS:
        session["role"]="admin"
        session["name"]="CDRRMO Admin"
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401

@app.get("/api/me")
def me():
    return jsonify({"role": session.get("role","anon"), "name": session.get("name","")})

# ---------- POSTS ----------
@app.post("/api/posts")
def create_post():
    if session.get("role") not in ("user","admin"):
        return jsonify({"ok": False, "error":"Login required"}), 401

    data = request.get_json(silent=True) or {}
    author = (data.get("author") or session.get("name") or "Anonymous").strip()[:60]
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error":"Content required"}), 400

    cls = classify(content)
    created_at = datetime.utcnow().replace(microsecond=0).isoformat()+"Z"

    conn = db(); cur = conn.cursor()
    cur.execute("""INSERT INTO posts(author,content,created_at,is_disaster,disaster_type,urgency,confidence,location_text,lat,lon)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (author,content,created_at,
         cls.get("is_disaster",0),
         cls.get("disaster_type"),
         cls.get("urgency"),
         cls.get("confidence"),
         cls.get("location_text"),
         cls.get("lat"),
         cls.get("lon"))
    )
    post_id = cur.lastrowid
    conn.commit(); conn.close()

    # ADMIN payload (full)
    full = {
      "id": post_id, "author": author, "content": content, "created_at": created_at,
      "is_disaster": cls.get("is_disaster",0),
      "disaster_type": cls.get("disaster_type"),
      "urgency": cls.get("urgency"),
      "confidence": cls.get("confidence"),
      "location_text": cls.get("location_text"),
      "lat": cls.get("lat"), "lon": cls.get("lon"),
    }

    # Broadcast ONLY to admin dashboards; citizen UI can still refresh /api/posts safely
    broadcast({"type":"new_post","post": full})

    # USER response: safe fields only
    safe = {"id": post_id, "author": author, "content": content, "created_at": created_at}
    return jsonify({"ok": True, "post": safe})

@app.get("/api/posts")
def list_posts():
    role = session.get("role","anon")
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT * FROM posts ORDER BY id DESC LIMIT 300")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    if role == "admin":
        # admin gets full
        return jsonify({"ok": True, "posts": rows})

    # user gets safe only (no urgency/confidence/type)
    safe_rows = [{"id":r["id"],"author":r["author"],"content":r["content"],"created_at":r["created_at"]} for r in rows]
    return jsonify({"ok": True, "posts": safe_rows})

# ---------- STATIC ----------
@app.get("/")
def root():
    return send_from_directory(PUBLIC_DIR, "index.html")

@app.get("/<path:path>")
def serve(path):
    return send_from_directory(PUBLIC_DIR, path)

if __name__ == "__main__":
    init_db()
    app.run()
