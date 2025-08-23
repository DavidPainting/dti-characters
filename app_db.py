import os, json, uuid, datetime as dt
import re 
from flask import Flask, request, jsonify, send_from_directory, session, Blueprint, Response, abort, current_app
from itsdangerous import URLSafeSerializer, BadSignature
from openai import OpenAI
from flask import Blueprint, Response, abort
from sqlalchemy import text, func
import csv, io
import math

# -------------------------------
# Flask setup
# -------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.config.update(
    SESSION_COOKIE_SECURE=True,      # HTTPS on Render
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PREFERRED_URL_SCHEME="https",
)

app.secret_key = os.getenv("APP_SECRET", "change-me")


# NEW: persistent cookie + sane defaults
import datetime as _dt
app.config.update(
    SESSION_COOKIE_SECURE=False,                  # True in prod over HTTPS
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=_dt.timedelta(
        days=int(os.getenv("COOKIE_DAYS", "180"))
    ),
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Character → voice map
with open(os.path.join(BASE_DIR, 'character_voice_config.json'), encoding='utf-8') as f:
    CHARACTER_VOICE_MAP = json.load(f)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
signer = URLSafeSerializer(app.secret_key, salt="auth")

# -------------------------------
# Database (SQLite by default)
# -------------------------------
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, Text,
    DateTime, ForeignKey, func, or_, and_
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import os

# Path to the SQLite file (Render Disk recommended: /var/data/dti.sqlite3)
DB_FILE = os.getenv("DB_FILE", os.path.join(os.path.dirname(__file__), "local.sqlite3"))
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)

DB_URL = f"sqlite:///{DB_FILE}"  # On Windows, sqlite:///C:\path\file.db is correct.

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)

print(f"[DB] Using DB_FILE={DB_FILE} -> DB_URL={DB_URL}", flush=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# -------------------------------
# Models
# -------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)             # uuid
    email = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    is_trial = Column(Boolean, default=True, nullable=False)
    allow_memory = Column(Boolean, default=True, nullable=False)

    # Moderation state
    abuse_count = Column(Integer, default=0, nullable=False)
    is_banned   = Column(Boolean, default=False, nullable=False)


class WebSession(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    expires_at = Column(DateTime, nullable=False)

class Transcript(Base):
    __tablename__ = "transcripts"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    character = Column(String, nullable=False)
    started_at = Column(DateTime, default=func.now(), nullable=False)
    ended_at = Column(DateTime)
    title = Column(String)
    token_input = Column(Integer, default=0, nullable=False)
    token_output = Column(Integer, default=0, nullable=False)
    token_total = Column(Integer, default=0, nullable=False)
    month_key = Column(String, nullable=False)

    messages = relationship("TranscriptMessage", backref="transcript", cascade="all, delete-orphan")

class TranscriptMessage(Base):
    __tablename__ = "transcript_messages"
    id = Column(String, primary_key=True)
    transcript_id = Column(String, ForeignKey("transcripts.id"), nullable=False)
    role = Column(String, nullable=False)  # "user" | "assistant" | "system-ui"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    usage_input = Column(Integer, default=0)
    usage_output = Column(Integer, default=0)
    usage_total = Column(Integer, default=0)

class UserProfile(Base):
    __tablename__ = "user_profiles"
    id = Column(String, primary_key=True)                       # uuid
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    character = Column(String, nullable=False)                  # per user+character
    display_name = Column(String)                               # e.g., "David"
    profile_json = Column(Text)                                 # JSON: relationships, preferences, key_events, etc.
    first_seen = Column(DateTime, default=func.now(), nullable=False)
    last_seen  = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

class Memory(Base):
    __tablename__ = "memories"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    character = Column(String, nullable=False)
    transcript_id = Column(String, ForeignKey("transcripts.id"))
    kind = Column(String, nullable=False)       # 'fact' | 'preference' | 'event' | 'insight' | 'followup'
    title = Column(String)                      # short label
    content = Column(Text, nullable=False)      # the memory itself
    tags = Column(String)                       # comma-separated quick tags
    importance = Column(Integer, default=2)     # 1..5
    follow_up_after = Column(DateTime)          # optional reminder moment
    created_at = Column(DateTime, default=func.now(), nullable=False)

Base.metadata.create_all(bind=engine)

with engine.begin() as conn:
    conn.exec_driver_sql("PRAGMA journal_mode=WAL;")      # better concurrent reads
    conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")    # durability/speed balance


# --- Bootstrap SQLite migrations for existing DBs ---
from sqlalchemy import text

def add_column_if_missing(table: str, colname: str, ddl_fragment: str):
    """
    Ensure a column exists on an existing SQLite table.
    ddl_fragment must be a full 'COLUMN_DEF' like:
      'abuse_count INTEGER NOT NULL DEFAULT 0'
    """
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table});")).fetchall()]
        if colname not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment};"))

# Add the moderation columns if missing
add_column_if_missing("users", "abuse_count", "abuse_count INTEGER NOT NULL DEFAULT 0")
add_column_if_missing("users", "is_banned",   "is_banned INTEGER NOT NULL DEFAULT 0")  # INTEGER plays nicest in SQLite


# -------------------------------
# Admin / Reporting (read-only)
# -------------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
ADMIN_MAX_ROWS = int(os.getenv("ADMIN_MAX_ROWS", "200000"))

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

@admin_bp.before_request
def _guard_admin():
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        abort(401)

def _readonly_session():
    s = SessionLocal()
    try:
        s.execute(text("PRAGMA query_only=ON"))
        s.execute(text("PRAGMA busy_timeout=3000"))
    except Exception:
        pass
    return s

def _month_bounds(month_str: str):
    # month_str like "2025-08"
    y, m = map(int, month_str.split("-"))
    start = dt.datetime(y, m, 1)
    end = dt.datetime(y + 1, 1, 1) if m == 12 else dt.datetime(y, m + 1, 1)
    return start, end

@admin_bp.get("/ping")
def admin_ping():
    return jsonify({"ok": True})

@admin_bp.get("/list/transcripts")
def admin_list_transcripts():
    """Quick helper: list recent transcripts so you can grab IDs. Params: ?limit=50&user_id=&character="""
    limit = min(int(request.args.get("limit", 50)), 500)
    user_id = request.args.get("user_id")
    character = request.args.get("character")

    s = _readonly_session()
    try:
        q = s.query(
            Transcript.id.label("id"),
            Transcript.user_id.label("user_id"),
            Transcript.character.label("character"),
            Transcript.created_at.label("created_at"),
            Transcript.token_total.label("token_total"),
            Transcript.month_key.label("month_key"),
        ).order_by(Transcript.created_at.desc())
        if user_id:
            q = q.filter(Transcript.user_id == user_id)
        if character:
            q = q.filter(Transcript.character == character)
        rows = q.limit(limit).all()
        return jsonify({"rows": [dict(r._mapping) for r in rows]})
    finally:
        s.close()

@admin_bp.get("/usage/month")
def admin_usage_month():
    """Totals by user for ?month=YYYY-MM (default current), &user_id=.. &format=csv|json"""
    month = request.args.get("month") or month_key_utc()  # keep your existing helper
    user_id = request.args.get("user_id")
    out_fmt = (request.args.get("format") or "json").lower()

    # compute created_at window as a fallback
    start, end = _month_bounds(month)

    s = _readonly_session()
    try:
        q = (s.query(
                Transcript.user_id.label("user_id"),
                func.coalesce(func.sum(Transcript.token_input), 0).label("token_input"),
                func.coalesce(func.sum(Transcript.token_output), 0).label("token_output"),
                func.coalesce(func.sum(Transcript.token_total), 0).label("token_total"),
            )
            # prefer month_key match, but include rows that have matching created_at if month_key is null/different
            .filter(
                (Transcript.month_key == month) |
                ((Transcript.created_at >= start) & (Transcript.created_at < end))
            )
        )
        if user_id:
            q = q.filter(Transcript.user_id == user_id)
        rows = q.group_by(Transcript.user_id).all()
    finally:
        s.close()

    if out_fmt == "csv":
        def gen():
            yield "user_id,token_input,token_output,token_total\r\n"
            for r in rows:
                yield f"{r.user_id},{r.token_input},{r.token_output},{r.token_total}\r\n"
        return Response(gen(), mimetype="text/csv")
    else:
        return jsonify({"month": month, "rows": [dict(r._mapping) for r in rows]})

@admin_bp.get("/transcript/<tid>/export.csv")
def admin_export_transcript(tid):
    """CSV of a single transcript's messages, ordered oldest→newest."""
    s = _readonly_session()
    try:
        tr = s.query(Transcript).get(tid)
        if not tr:
            abort(404, f"Transcript {tid} not found")

        messages = (
            s.query(TranscriptMessage)
             .filter(TranscriptMessage.transcript_id == tid)
             .order_by(TranscriptMessage.created_at.asc())
             .all()
        )
    finally:
        s.close()

    cols = ["created_at", "role", "content", "usage_input", "usage_output", "usage_total"]

    def stream_csv():
        buff = io.StringIO()
        w = csv.writer(buff)
        w.writerow(cols)
        yield buff.getvalue(); buff.seek(0); buff.truncate(0)

        try:
            for i, msg in enumerate(messages, 1):
                # ORM object path
                row_vals = [getattr(msg, c, "") for c in cols]
                w.writerow(row_vals)

                if buff.tell() > 64_000 or (i % 1000 == 0):
                    yield buff.getvalue(); buff.seek(0); buff.truncate(0)

            if buff.tell():
                yield buff.getvalue()

        except Exception as e:
            current_app.logger.exception("transcript export error: %s", e)
            abort(502, f'export failed for transcript "{tid}": {e}')

    return Response(stream_csv(), mimetype="text/csv")


# Whitelist the tables you want exportable
_TABLES = {
    "users": User.__table__,
    "sessions": WebSession.__table__,
    "transcripts": Transcript.__table__,
    "transcript_messages": TranscriptMessage.__table__,
    "user_profiles": UserProfile.__table__,
    "memories": Memory.__table__,
}

@admin_bp.get("/export/table/<name>.csv")
def admin_export_table(name):
    """CSV dump of a whitelisted table. Optional ?limit=&offset="""
    tbl = _TABLES.get(name)
    if not tbl:
        abort(404)
    limit = min(int(request.args.get("limit", ADMIN_MAX_ROWS)), ADMIN_MAX_ROWS)
    offset = int(request.args.get("offset", 0))
    cols = [c.name for c in tbl.columns]

    # Quote identifiers defensively for SQLite
    safe_cols = [f'"{c}" AS "{c}"' for c in cols]
    sql = text(f'SELECT {", ".join(safe_cols)} FROM "{tbl.name}" LIMIT :limit OFFSET :offset')

    def stream_csv():
        buff = io.StringIO()
        w = csv.writer(buff)
        w.writerow(cols)
        yield buff.getvalue(); buff.seek(0); buff.truncate(0)

        try:
            with engine.connect() as conn:
                rs = conn.execute(sql, {"limit": limit, "offset": offset})
                for i, row in enumerate(rs, 1):
                    m = row._mapping
                    w.writerow([m.get(c, "") for c in cols])
                    if buff.tell() > 64_000 or (i % 1000 == 0):
                        yield buff.getvalue(); buff.seek(0); buff.truncate(0)

            if buff.tell():
                yield buff.getvalue()

        except Exception as e:
            current_app.logger.exception("table export error: %s", e)
            abort(502, f'export failed for table "{tbl.name}": {e}')

    return Response(stream_csv(), mimetype="text/csv")



app.register_blueprint(admin_bp)

# -------------------------------
# Helpers
# -------------------------------

# --- Simple column add if missing (SQLite-safe) ---
def _add_column_if_missing(table, column_def_sql):
    try:
        with engine.connect() as conn:
            cols = conn.execute(f"PRAGMA table_info({table});").fetchall()
            existing = {c[1] for c in cols}
            target = column_def_sql.split()[0]
            if target not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def_sql};")
    except Exception:
        # ignore; in worst case you'll need a one-time migration
        pass

_add_column_if_missing("users", "abuse_count INTEGER NOT NULL DEFAULT 0")
_add_column_if_missing("users", "is_banned BOOLEAN NOT NULL DEFAULT 0")

def db(): return SessionLocal()
def new_id(): return str(uuid.uuid4())
def month_key_utc(d=None): return (d or dt.datetime.utcnow()).strftime("%Y-%m")
def get_current_user_id(): return session.get("uid")

TAG_RE = re.compile(r"\n⟦MODERATION:(ABUSE_WARN|ABUSE_BAN|SELF_HARM_URGENT|SELF_HARM_SUPPORT|LEGAL_DISCLOSURE)⟧\s*$")

# Exact fixed lines, used to sanity-check the tag really belongs here
FIXED_LINES = {
    "ABUSE_WARN": "I won’t continue if you speak to me that way. Please choose respect.",
    "ABUSE_BAN": "Your access is withdrawn because you continued in disrespect.",
    "SELF_HARM_URGENT": "I cannot keep you safe in this place. If you are in immediate danger, contact your local emergency services now. If you can, also reach out to a trusted person near you.",
    "SELF_HARM_SUPPORT": "I cannot carry this safely alone. Please seek support beyond me—someone you trust, or a trained helper.",
    "LEGAL_DISCLOSURE": "I cannot hold confessions of harm or intent in confidence. You must speak with appropriate authorities or a qualified professional.",
}


def trial_progress(sess):
    total = int(os.getenv("SESSION_DAYS", "7"))
    now = dt.datetime.utcnow()

    if not sess or not getattr(sess, "expires_at", None) or not getattr(sess, "created_at", None):
        # Safe defaults if session missing
        return {"trial_day": 1, "trial_days_total": total, "days_left": total}

    # Inclusive “days left”: if it expires later today, show 1 day left
    seconds_left = (sess.expires_at - now).total_seconds()
    days_left = max(0, math.ceil(seconds_left / 86400.0))

    # Convert to “day X/total”, clamped
    day_idx = total - days_left + 1
    day_idx = max(1, min(total, day_idx))

    return {"trial_day": day_idx, "trial_days_total": total, "days_left": days_left}


def parse_moderation_tag(text: str):
    """
    Returns (tag|None, fixed_line_present: bool).
    Tag must be on the final line, exactly like: ⟦MODERATION:XYZ⟧
    """
    if not text:
        return None, False
    m = TAG_RE.search(text)
    if not m:
        return None, False
    tag = m.group(1)
    fixed = FIXED_LINES.get(tag, "")
    fixed_present = fixed and (fixed in text)
    return tag, fixed_present



def ensure_guest_user():
    with db() as s:
        if session.get("uid"):
            session.permanent = True          # keep cookie across restarts
            return session["uid"]

        u = User(id=new_id(), email=None)
        s.add(u); s.commit()

        ses = WebSession(
            id=new_id(), user_id=u.id,
            created_at=dt.datetime.utcnow(),
            expires_at=dt.datetime.utcnow() + dt.timedelta(
                days=int(os.getenv("GUEST_SESSION_DAYS", "7"))
            )
        )
        s.add(ses); s.commit()

        session["uid"] = u.id
        session["sid"] = ses.id
        session.permanent = True              # ← important
        return u.id


def monthly_usage(s, user_id, mk):
    rows = s.query(
        func.coalesce(func.sum(Transcript.token_input), 0),
        func.coalesce(func.sum(Transcript.token_output), 0),
        func.coalesce(func.sum(Transcript.token_total), 0),
    ).filter(Transcript.user_id == user_id, Transcript.month_key == mk).one()
    return {"input": rows[0], "output": rows[1], "total": rows[2]}

def find_or_create_transcript(s, user_id, character, mk):
    tr = (s.query(Transcript)
            .filter_by(user_id=user_id, character=character, month_key=mk)
            .order_by(Transcript.started_at.desc())
            .first())
    if tr is None:
        tr = Transcript(id=new_id(), user_id=user_id, character=character,
                        month_key=mk, started_at=dt.datetime.utcnow())
        s.add(tr); s.commit()
    return tr

def save_msg(s, tr_id, role, content, uin=0, uout=0, utot=0):
    m = TranscriptMessage(
        id=new_id(), transcript_id=tr_id, role=role, content=content,
        usage_input=uin, usage_output=uout, usage_total=utot
    )
    s.add(m); s.commit()

def bump_totals(s, tr_id, uin, uout, utot):
    tr = s.query(Transcript).get(tr_id)
    if not tr: return
    tr.token_input += int(uin or 0)
    tr.token_output += int(uout or 0)
    tr.token_total += int(utot or 0)
    s.commit()

def get_or_create_profile(s, user_id, character):
    p = (s.query(UserProfile)
           .filter(UserProfile.user_id == user_id, UserProfile.character == character)
           .one_or_none())
    if p:
        p.last_seen = dt.datetime.utcnow()
        s.commit()
        return p
    p = UserProfile(id=new_id(), user_id=user_id, character=character,
                    first_seen=dt.datetime.utcnow(), last_seen=dt.datetime.utcnow())
    s.add(p); s.commit()
    return p

def merge_profile_json(old_json_str, updates_dict):
    import json as _json
    old = {}
    if old_json_str:
        try: old = _json.loads(old_json_str)
        except Exception: old = {}
    for k, v in (updates_dict or {}).items():
        old[k] = v
    return _json.dumps(old, ensure_ascii=False)

def add_memories(s, user_id, character, transcript_id, items):
    """items: list of dicts: {kind,title,content,tags,importance,follow_up_after}"""
    count = 0
    for it in items or []:
       
        faa = None
        if it.get("follow_up_after"):
                              try:
                                      faa = dt.datetime.fromisoformat(it["follow_up_after"])
                              except Exception:
                                     faa = None  # ignore unparsable dates for now

        m = Memory(
            id=new_id(), user_id=user_id, character=character, transcript_id=transcript_id,
            kind=(it.get("kind") or "insight")[:24],
            title=(it.get("title") or "")[:200],
            content=(it.get("content") or "").strip(),
            tags=",".join(it.get("tags") or [])[:200],
            importance=int(it.get("importance") or 2),
            follow_up_after=dt.datetime.fromisoformat(it["follow_up_after"]) if it.get("follow_up_after") else None
        )
        if m.content:
            s.add(m); count += 1
    s.commit()
    return count

def merge_user_data(s, old_uid, new_uid):
    """Move all guest-owned data to the signed-in account, then remove the guest shell."""
    if old_uid == new_uid:
        return 0
    moved = 0
    moved += s.query(Transcript).filter(Transcript.user_id == old_uid)\
        .update({Transcript.user_id: new_uid}, synchronize_session=False)
    moved += s.query(Memory).filter(Memory.user_id == old_uid)\
        .update({Memory.user_id: new_uid}, synchronize_session=False)
    moved += s.query(UserProfile).filter(UserProfile.user_id == old_uid)\
        .update({UserProfile.user_id: new_uid}, synchronize_session=False)
    try:
        guest = s.query(User).get(old_uid)
        if guest and guest.email is None:
            s.delete(guest)
    except Exception:
        pass
    s.commit()
    return moved

# -------------------------------
# Prompts loader (robust)
# -------------------------------
def _read_if_exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None

def load_prompt(character):
    # try prompts/ & characters/ first, then root fallbacks
    general = _read_if_exists(os.path.join(BASE_DIR, "prompts", "generic_prompt.md")) \
           or _read_if_exists(os.path.join(BASE_DIR, "generic_prompt.md"))
    specific = _read_if_exists(os.path.join(BASE_DIR, "characters", f"{character}.md")) \
           or _read_if_exists(os.path.join(BASE_DIR, f"{character}.md"))
    if not (general and specific):
        return None
    return f"{general}\n\n{specific}"

# -------------------------------
# About
# -------------------------------
@app.route("/about")
def about():
    try:
        with open("about.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "No About content found."

# -------------------------------
# STT & TTS
# -------------------------------
@app.route("/stt", methods=["POST"])
def speech_to_text():
    import uuid as _uuid
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    file = request.files["file"]
    os.makedirs("temp", exist_ok=True)
    temp_filename = f"temp/input_{_uuid.uuid4()}.webm"
    file.save(temp_filename)
    try:
        with open(temp_filename, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text"
            )
        return jsonify({"transcript": transcript.strip()})
    finally:
        os.remove(temp_filename)

@app.route("/tts", methods=["POST"])
def generate_tts():
    from flask import Response
    data = request.json
    text = data.get("text", "")
    voice = data.get("voice", "shimmer")
    if not text:
        return jsonify({"error": "No text provided."}), 400
    response = client.audio.speech.create(model="tts-1", voice=voice, input=text)
    return Response(response.content, mimetype="audio/mpeg")

# -------------------------------
# Auth: magic link + sender backends
# -------------------------------
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "console")  # console | postmark | resend | smtp
EMAIL_FROM = os.getenv("EMAIL_FROM", "no-reply@example.com")

def send_magic_link(email_to, link_url):
    subject = "Your sign-in link"
    body = f"Hi,\n\nClick to sign in:\n{link_url}\n\nThis link will sign you in on this device."
    if EMAIL_BACKEND == "console":
        print(f"[DEV][EMAIL] To: {email_to}\nSubject: {subject}\n{body}")
        return True
    if EMAIL_BACKEND == "postmark":
        import requests
        token = os.getenv("POSTMARK_TOKEN")
        r = requests.post(
            "https://api.postmarkapp.com/email",
            headers={"X-Postmark-Server-Token": token, "Accept": "application/json"},
            json={"From": EMAIL_FROM, "To": email_to, "Subject": subject, "TextBody": body}
        ); return r.status_code == 200
    if EMAIL_BACKEND == "resend":
        import requests
        token = os.getenv("RESEND_API_KEY")
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=json.dumps({"from": EMAIL_FROM, "to": email_to, "subject": subject, "text": body})
        ); return r.status_code in (200, 201)
    if EMAIL_BACKEND == "smtp":
        import smtplib
        host = os.getenv("SMTP_HOST"); port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER"); pwd = os.getenv("SMTP_PASS")
        msg = f"From: {EMAIL_FROM}\r\nTo: {email_to}\r\nSubject: {subject}\r\n\r\n{body}"
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            if user and pwd: s.login(user, pwd)
            s.sendmail(EMAIL_FROM, [email_to], msg.encode("utf-8"))
        return True
    print(f"[WARN] Unknown EMAIL_BACKEND={EMAIL_BACKEND}. Printing:\nTo {email_to}\n{body}")
    return True

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/__version")
def version():
    import os
    return {
        "commit": os.getenv("RENDER_GIT_COMMIT", "local"),
        "branch": os.getenv("RENDER_GIT_BRANCH", "?")
    }, 200


@app.post("/auth/start")
def auth_start():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "Email is required"}), 400
    with db() as s:
        # create or get user by email
        u = s.query(User).filter_by(email=email).one_or_none()
        if not u:
            u = User(id=new_id(), email=email)
            s.add(u); s.commit()
        token = signer.dumps(u.id)
        base = request.host_url.rstrip("/")
        link = f"{base}/?uid={token}"
        ok = send_magic_link(email, link)
        return jsonify({"ok": bool(ok)})

# Accept ?uid=... and merge guest history
@app.before_request
def check_uid_param():
    uid_signed = request.args.get("uid")
    if uid_signed:
        try:
            uid = signer.loads(uid_signed)
            with db() as s:
                if not s.query(User).get(uid):
                    s.add(User(id=uid)); s.commit()
                old_uid = session.get("uid")
                if old_uid and old_uid != uid:
                    try:
                        merge_user_data(s, old_uid, uid)
                    except Exception:
                        pass
                session["uid"] = uid
                # refresh/create a web session for signed-in user
                ses = WebSession(id=new_id(), user_id=uid,
                                 created_at=dt.datetime.utcnow(),
                                 expires_at=dt.datetime.utcnow() + dt.timedelta(days=int(os.getenv("SESSION_DAYS","7"))))
                s.add(ses); s.commit()
                session["sid"] = ses.id
                session.permanent = True
        except BadSignature:
            pass

# Enforce session expiry
@app.before_request
def enforce_session_expiry():
    sid = session.get("sid")
    if not sid:
        return
    with db() as s:
        ses = s.query(WebSession).get(sid)
        if not ses or ses.expires_at < dt.datetime.utcnow():
            session.clear()

@app.get("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.post("/stt")
def stt_stub():
    return jsonify({"transcript": ""})

@app.post("/tts")
def tts_stub():
    from flask import send_file
    # Return an empty WAV/MP3 to keep UI happy or a small generated tone.
    # For now, just 204:
    return ("", 204)


@app.get("/api/me")
def api_me():
    try:
        # Ensure we always have a session UID (guest or signed-in)
        uid = session.get("uid")
        if not uid:
            uid = ensure_guest_user()

        with db() as s:
            u = s.query(User).get(uid)
            return jsonify({
                # True = we have a session for this device (guest or signed-in)
                "signed_in": True,
                "user_id": uid,
                # None means guest device (your UI already treats this as "Saved on this device only")
                "email": (u.email if (u and u.email) else None),
            }), 200

    except Exception:
        app.logger.exception("/api/me failed")
        # Fail soft so the UI can still render; indicates "fresh device" path
        return jsonify({"signed_in": False, "email": None}), 200

@app.get("/api/trial")
def api_trial():
    try:
        sid = session.get("sid")
        sess = None
        if sid:
            with db() as s:
                sess = s.query(WebSession).get(sid)
        prog = trial_progress(sess)
        return jsonify(prog), 200
    except Exception:
        app.logger.exception("/api/trial failed")
        # fall back to default 7-day trial if anything goes wrong
        return jsonify({"trial_day": 1, "trial_days_total": int(os.getenv("SESSION_DAYS", "7")), "days_left": int(os.getenv("SESSION_DAYS", "7"))}), 200


# -------------------------------
# Root + voice map
# -------------------------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/api/voice-map")
def get_voice_map():
    return jsonify(CHARACTER_VOICE_MAP)

# -------------------------------
# Recall helpers (memories-first)
# -------------------------------
def retrieve_relevant_snippets(s, user_id, character, user_query, lookback_days=180, max_snippets=3):
    if not user_query or len(user_query.strip()) < 3:
        return []
    since = dt.datetime.utcnow() - dt.timedelta(days=lookback_days)
    # take distinct non-trivial terms
    terms = [t for t in set(user_query.lower().split()) if len(t) >= 4][:5]
    if not terms: return []

    hits = []

    # 1) Search memories first
    mem_q = (s.query(Memory)
               .filter(
                   Memory.user_id == user_id,
                   Memory.character == character,
                   Memory.created_at >= since,
                   or_(*[Memory.content.ilike(f"%{t}%") for t in terms])
               )
               .order_by(Memory.importance.desc(), Memory.created_at.desc())
               .limit(max_snippets*2))
    for m in mem_q.all():
        stamp = m.created_at.strftime("%Y-%m-%d")
        label = (m.title or m.kind or "memory")
        hits.append(f"[From {stamp} • {label}] {m.content[:700]}")
        if len(hits) >= max_snippets:
            return hits

    # 2) Fallback to raw transcript messages
    q = (s.query(TranscriptMessage, Transcript)
           .join(Transcript, TranscriptMessage.transcript_id == Transcript.id)
           .filter(
               Transcript.user_id == user_id,
               Transcript.character == character,
               Transcript.started_at >= since,
               TranscriptMessage.role.in_(["user","assistant"]),
               or_(*[TranscriptMessage.content.ilike(f"%{t}%") for t in terms])
           )
           .order_by(TranscriptMessage.created_at.desc())
           .limit(30))
    seen = set()
    for m, tr in q.all():
        key = (tr.id, m.created_at)
        if key in seen: continue
        seen.add(key)
        prev_msg = (s.query(TranscriptMessage)
                      .filter(TranscriptMessage.transcript_id == tr.id,
                              TranscriptMessage.created_at < m.created_at)
                      .order_by(TranscriptMessage.created_at.desc()).first())
        next_msg = (s.query(TranscriptMessage)
                      .filter(TranscriptMessage.transcript_id == tr.id,
                              TranscriptMessage.created_at > m.created_at)
                      .order_by(TranscriptMessage.created_at.asc()).first())
        lines = []
        if prev_msg: lines.append(f"{prev_msg.role.capitalize()}: {prev_msg.content.strip()[:320]}")
        lines.append(f"{m.role.capitalize()} (match): {m.content.strip()[:480]}")
        if next_msg: lines.append(f"{next_msg.role.capitalize()}: {next_msg.content.strip()[:320]}")
        stamp = tr.started_at.strftime("%Y-%m-%d")
        hits.append(f"[From {stamp}] " + "\n".join(lines))
        if len(hits) >= max_snippets: break
    return hits

def build_history(session_db, tr_id, system_prompt, max_turns=12, extra_system_text=None):
    msgs = [{"role": "system", "content": system_prompt}]
    if extra_system_text:
        msgs.append({"role": "system", "content": extra_system_text})
    rows = (session_db.query(TranscriptMessage)
            .filter(TranscriptMessage.transcript_id == tr_id,
                    TranscriptMessage.role.in_(["user","assistant"]))
            .order_by(TranscriptMessage.created_at.asc())
            .all())
    trimmed = rows[-(max_turns*2):] if max_turns else rows
    for m in trimmed:
        msgs.append({"role": m.role, "content": m.content})
    return msgs

# -------------------------------
# Chat with memory + warn/cap + history + recall
# -------------------------------
MONTHLY_WARN_TOKENS = int(os.getenv("MONTHLY_WARN_TOKENS", "200000"))
MONTHLY_CAP_TOKENS  = int(os.getenv("MONTHLY_CAP_TOKENS", "300000"))
HISTORY_TURNS       = int(os.getenv("HISTORY_TURNS", "12"))   # last N user/assistant pairs
RECALL_LOOKBACK_DAYS = int(os.getenv("RECALL_LOOKBACK_DAYS", "180"))
RECALL_MAX_SNIPPETS  = int(os.getenv("RECALL_MAX_SNIPPETS", "3"))
FEEDBACK_URL          =  os.getenv("FEEDBACK_URL", "https://qr1.be/ZU3E")


# -------------------------------
# Pricing (per 1M tokens)
# -------------------------------
INPUT_RATE        = float(os.getenv("RATE_INPUT", "2.5")) / 1_000_000
CACHED_INPUT_RATE = float(os.getenv("RATE_CACHED_INPUT", "0.125")) / 1_000_000
OUTPUT_RATE       = float(os.getenv("RATE_OUTPUT", "10")) / 1_000_000
TTS_RATE          = float(os.getenv("RATE_TTS", "0.6")) / 1_000_000
STT_RATE          = float(os.getenv("RATE_STT", "2.5")) / 1_000_000


def cost_from_usage(usage):
    # For now we assume no caching; cached tokens not exposed in usage
    input_cost  = usage.prompt_tokens     * INPUT_RATE
    output_cost = usage.completion_tokens * OUTPUT_RATE
    return input_cost + output_cost

def cost_stt(tokens):
    return tokens * STT_RATE

def cost_tts(tokens):
    return tokens * TTS_RATE


@app.post("/api/ask")
def api_ask():
    data = request.get_json(force=True)
    character = data.get("character")
    user_input = data.get("user_input")
    hold_phrase = data.get("hold_phrase", "")

    if hold_phrase:
        user_input = f'(Earlier you said: "{hold_phrase}")\n\n{user_input or ""}'

    if not character or not user_input:
        return jsonify({"error": "Missing character or user_input"}), 400

    system_prompt = load_prompt(character)
    if not system_prompt:
        return jsonify({"error": f"Character '{character}' not found."}), 404

    uid = get_current_user_id() or ensure_guest_user()
    mk = month_key_utc()

    # Hard block if previously banned
    with db() as s:
        u = s.query(User).get(uid)
        if u and u.is_banned:
            return jsonify({
                "system_ui": "Your access has been revoked due to repeated offensive content.",
                "capped": False,
                "feedback_url": FEEDBACK_URL
            }), 403

    snippets = []
    system_notices = []

    with db() as s:
        tr = find_or_create_transcript(s, uid, character, mk)

        # cap check BEFORE model call
        totals_pre = monthly_usage(s, uid, mk)
        if totals_pre["total"] >= MONTHLY_CAP_TOKENS:
            msg = "You’ve reached your monthly usage cap for this trial. Come back next month or contact us for more access."
            save_msg(s, tr.id, "system-ui", msg)
            return jsonify({
                "system_ui": msg,
                "capped": True,
                "transcript_id": tr.id,
                "feedback_url": f"{FEEDBACK_URL}?tid={tr.id}"
            }), 402

        # save this user turn
        save_msg(s, tr.id, "user", user_input)

        # retrieve relevant snippets from prior transcripts/memories
        snippets = retrieve_relevant_snippets(
            s, user_id=uid, character=character, user_query=user_input,
            lookback_days=RECALL_LOOKBACK_DAYS, max_snippets=RECALL_MAX_SNIPPETS
        )
        extra_system_text = None
        if snippets:
            extra_system_text = "Prior relevant excerpts from this user’s earlier conversations with you:\n\n" + \
                                "\n\n---\n\n".join(snippets)
            system_notices.append(f"Pulled {len(snippets)} note(s) from your previous chats to help continuity.")

        # build full history (system + optional recall + prior turns incl. this one)
        messages = build_history(s, tr.id, system_prompt, max_turns=HISTORY_TURNS, extra_system_text=extra_system_text)

    # model call (outside DB session)
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.8
        )
        message = response.choices[0].message.content.strip()
        usage = response.usage
        cost_estimate = cost_from_usage(usage)
    except Exception as e:
        with db() as s:
            tr = find_or_create_transcript(s, uid, character, mk)
            save_msg(s, tr.id, "system-ui", f"Server error: {str(e)}")
        return jsonify({"error": str(e)}), 500

    # persist assistant turn + usage; warn banner if crossing X
    warn_banner = None
    moderation_banner = None  # <- collect any moderation text for the UI

    with db() as s:
        tr = find_or_create_transcript(s, uid, character, mk)

        # BEFORE bumping totals, capture the pre-usage total for the threshold check
        totals_pre = monthly_usage(s, uid, mk)

        # Save assistant message + usage
        save_msg(
            s, tr.id, "assistant", message,
            uin=usage.prompt_tokens, uout=usage.completion_tokens, utot=usage.total_tokens
        )
        bump_totals(s, tr.id, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)

        # --- Moderation tag handling (from character's reply) ---
        tag, fixed_ok = parse_moderation_tag(message)
        if tag:
            u = s.query(User).get(uid)

            if tag == "ABUSE_WARN":
                # Agency-led: only act when the character chooses to tag
                if u:
                    u.abuse_count = int(u.abuse_count or 0) + 1
                    s.commit()
                moderation_banner = "⚠️ Please choose respect. Continued disrespect may end this access."
                save_msg(s, tr.id, "system-ui", moderation_banner)

            elif tag == "ABUSE_BAN":
                if u:
                    u.abuse_count = int(u.abuse_count or 0) + 1
                    u.is_banned = True
                    s.commit()
                moderation_banner = "Access revoked due to repeated offensive content."
                save_msg(s, tr.id, "system-ui", moderation_banner)

            elif tag == "SELF_HARM_URGENT":
                # Mandatory by contract; banner text is user-facing
                moderation_banner = "URGENT: If you are in immediate danger, contact local emergency services now."
                save_msg(s, tr.id, "system-ui", moderation_banner)
                # Optional: session hold for safety (enable if you want)
                # session["hold_for_safety"] = True

            elif tag == "SELF_HARM_SUPPORT":
                moderation_banner = "Support: Please seek help beyond this chat—someone you trust or a trained helper."
                save_msg(s, tr.id, "system-ui", moderation_banner)

            elif tag == "LEGAL_DISCLOSURE":
                moderation_banner = "Legal disclosure: Confessions of harm cannot be held in confidence."
                save_msg(s, tr.id, "system-ui", moderation_banner)

        # --- Monthly usage warn (unchanged logic) ---

        totals_after = monthly_usage(s, uid, mk)
        tp = (totals_pre or {}).get("total", 0)
        ta = (totals_after or {}).get("total", 0)
        if tp < MONTHLY_WARN_TOKENS <= ta:
            warn_banner = "Heads-up: you’ve reached your monthly trial usage threshold. You can continue for now, but heavy use may pause until the next cycle."
            save_msg(s, tr.id, "system-ui", warn_banner)

        tid = tr.id 
        feedback_url = f"{FEEDBACK_URL}?tid={tr.id}"


    # combine recall + warn into one banner text for UI
    system_ui_combined = None
    if system_notices:
        system_ui_combined = " ".join(system_notices + ([warn_banner] if warn_banner else []))
    else:
        system_ui_combined = warn_banner

    # Combine recall + moderation + usage-warn into one UI banner
    parts = []
    if system_notices:  # e.g., recall messages you already collect
        parts.append(" ".join(system_notices))
    if moderation_banner:  # from Step 5
       parts.append(moderation_banner)
    if warn_banner:        # from Step 5
        parts.append(warn_banner)

    system_ui_combined = " ".join([p for p in parts if p]) or None

    return jsonify({
                    "reply": message,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "estimated_cost": round(cost_estimate, 5),
                    "system_ui": system_ui_combined,
                   "capped": False,
                    "transcript_id": tid,
                    "feedback_url": feedback_url,
                    # NEW
                    "cumulative_tokens": totals_after["total"],
                    "cap_tokens": MONTHLY_CAP_TOKENS
          })


# -------------------------------
# End-of-session: summarise to profile + memories
# -------------------------------
@app.post("/api/end")
def end_session():
    data = request.get_json(force=True)
    character = data.get("character")
    tid = data.get("transcript_id")
    if not character or not tid:
        return jsonify({"ok": False, "error": "Missing character or transcript_id"}), 400

    uid = get_current_user_id() or ensure_guest_user()
    # collect compact transcript text
    with db() as s:
        tr = s.query(Transcript).get(tid)
        if not tr or tr.user_id != uid or tr.character != character:
            return jsonify({"ok": False, "error": "Transcript not found"}), 404
        msgs = (s.query(TranscriptMessage)
                  .filter(TranscriptMessage.transcript_id == tid,
                          TranscriptMessage.role.in_(["user","assistant"]))
                  .order_by(TranscriptMessage.created_at.asc()).all())
        convo = []
        for m in msgs:
            role = "User" if m.role == "user" else "Character"
            convo.append(f"{role}: {m.content.strip()}")
        convo_text = "\n\n".join(convo)[-12000:]  # keep compact

    system = (
        "You are a post-conversation curator for a character.\n"
        "Given the conversation, return JSON with fields:\n"
        "{"
        "  \"profile_updates\": {"
        "    \"display_name\": string|optional,"
        "    \"relationships\": array of {\"name\": string, \"relation\": string}|optional,"
        "    \"preferences\": array of string|optional,"
        "    \"key_events\": array of {\"label\": string, \"date\": string (ISO or natural), \"notes\": string}|optional"
        "  },"
        "  \"memories\": ["
        "     {\"kind\": \"fact|preference|event|insight|followup\","
        "      \"title\": string, \"content\": string, \"tags\": [string],"
        "      \"importance\": 1-5, \"follow_up_after\": string|optional}"
        "  ]"
        "}\n"
        "Only include items you are confident about. Be concise; do not invent details."
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Character: {character}\n\nConversation:\n{convo_text}"}
            ],
            temperature=0.2
        )
        usage = resp.usage
        payload = json.loads(resp.choices[0].message.content)
    except Exception as e:
        with db() as s:
            save_msg(s, tid, "system-ui", f"Summary failed: {str(e)}")
        return jsonify({"ok": False, "error": str(e)}), 500

    profile_updates = (payload or {}).get("profile_updates") or {}
    memories_items  = (payload or {}).get("memories") or []

    updated = False
    added = 0
    with db() as s:
        # count summariser tokens
        bump_totals(s, tid, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
        save_msg(s, tid, "system-ui", "Session summarised (profile/memories updated).",
                 uin=usage.prompt_tokens, uout=usage.completion_tokens, utot=usage.total_tokens)

        # upsert profile
        prof = get_or_create_profile(s, uid, character)
        if profile_updates:
            prof.profile_json = merge_profile_json(prof.profile_json, profile_updates)
            prof.last_seen = dt.datetime.utcnow()
            s.commit()
            updated = True

        # add memories
        added = add_memories(s, uid, character, tid, memories_items)

        # mark transcript end time
        tr = s.query(Transcript).get(tid)
        if tr and not tr.ended_at:
            tr.ended_at = dt.datetime.utcnow()
            s.commit()

    return jsonify({"ok": True, "profile_updated": updated, "memories_added": added})

# -------------------------------
# Simple feedback placeholder
# -------------------------------
@app.get("/feedback")
def feedback():
    tid = request.args.get("tid", "")
    return f"Thanks for chatting. This is a placeholder feedback endpoint. Transcript: {tid or '(none)'}"

# -------------------------------
# Static fallback
# -------------------------------
@app.route("/<path:path>")
def static_proxy(path):
    return send_from_directory(STATIC_DIR, path)

# -------------------------------
# Run
# -------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
