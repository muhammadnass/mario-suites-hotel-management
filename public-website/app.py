"""
Mario Suites Ltd — Public Website
-----------------------------------
A small, free-to-run marketing site for Mario Suites Ltd. Its only job is
visibility and making it effortless for a visitor to reach out — it is NOT
an online booking or payment system. "Chat on WhatsApp" and "Call Us" use
plain, free wa.me / tel: links (no paid APIs, no API keys required
anywhere in this app).

Unlike front-desk-app (Part 1), THIS app is meant to be hosted on the
internet, not run on the hotel's front-desk computer. It keeps its own
small SQLite database (site.db) with its own copy of the room list, since
it runs on a different, disconnected machine from the front desk. See the
project's root README.md for how to keep room prices in sync between the
two apps (export/import a small JSON file).

Run it with:  python app.py
Then open:    http://127.0.0.1:5001 in your browser.
"""

import json
import os
import sqlite3
from datetime import date, datetime
from functools import wraps
from urllib.parse import quote, urlencode

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                    send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# Pillow is only used to draw placeholder room photos for the demo rooms
# seeded below. If it's ever missing (e.g. requirements.txt wasn't
# reinstalled yet), fall back gracefully instead of crashing the whole
# app — rooms just keep showing the CSS gradient placeholder card.
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

app = Flask(__name__)
app.secret_key = "mario-suites-ltd-public-site-change-this-secret-key"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "site.db")
ROOM_PHOTOS_DIR = os.path.join(BASE_DIR, "static", "room_photos")
os.makedirs(ROOM_PHOTOS_DIR, exist_ok=True)

HOTEL_NAME = "Mario Suites Ltd"

# Exact map coordinates for the hotel (from the owner's Google Maps pin),
# used for the embedded map and "Get Directions" link instead of geocoding
# the text address, so the pin lands precisely rather than approximately.
HOTEL_LAT = 11.963389
HOTEL_LNG = 8.432611
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

# Shared secret the front-desk app must send (as the X-Sync-Key header) to
# push room/availability data to this site — see /sync/rooms below. Set
# this in your hosting environment (Render's render.yaml generates it
# automatically); never hard-code a real key here. If it's unset, syncing
# is simply turned off (the endpoint always refuses) rather than left open.
SYNC_API_KEY = os.environ.get("SYNC_API_KEY")

# Demo rooms seeded for a first look at the site — same naming/floor/price
# convention as front-desk-app's SEED_ROOMS, so the two line up out of the
# box. Every one of these is a completely ordinary room row: rename it,
# reprice it, swap its photo, or delete it from /admin exactly like a room
# you added yourself — nothing here is special-cased or locked.
SEED_ROOMS = [
    ("A1", "Ground Floor", 20000),
    ("A2", "Ground Floor", 20000),
    ("A3", "Ground Floor", 20000),
    ("A4", "Ground Floor", 20000),
    ("A5", "Ground Floor", 20000),
    ("B1", "First Floor", 25000),
    ("B2", "First Floor", 25000),
    ("B3", "First Floor", 25000),
    ("B4", "First Floor", 25000),
    ("B5", "First Floor", 25000),
]


def generate_placeholder_photo(name, floor):
    """Draw a simple on-brand placeholder image for a demo room (gold-to-
    bronze gradient card with the room name/floor on it) and save it under
    static/room_photos/. Returns the filename to store in photo_filename,
    or None if Pillow isn't installed or drawing fails for any reason —
    the room card falls back to the plain CSS placeholder either way, so
    this is a nice-to-have, never something that should break a deploy.
    These are clearly placeholders, not real photos of the hotel; swap
    each one out via /admin once real room photos exist."""
    if not PIL_AVAILABLE:
        return None
    filename = f"demo-{secure_filename(name)}.jpg"
    path = os.path.join(ROOM_PHOTOS_DIR, filename)
    if os.path.exists(path):
        return filename  # already generated once; don't redo it every boot
    try:
        width, height = 900, 600
        top_color = (201, 162, 75)   # --color-accent
        bottom_color = (139, 105, 20)  # --color-primary-dark
        img = Image.new("RGB", (width, height), top_color)
        draw = ImageDraw.Draw(img)
        for y in range(height):
            t = y / height
            r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
            g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
            b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

        def load_font(size):
            for candidate in (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ):
                if os.path.exists(candidate):
                    return ImageFont.truetype(candidate, size)
            try:
                return ImageFont.load_default(size=size)
            except TypeError:
                return ImageFont.load_default()

        title_font = load_font(72)
        sub_font = load_font(28)

        title = f"Room {name}"
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((width - tw) / 2, height / 2 - th), title, font=title_font, fill="white")

        sub = f"{floor} · Demo photo — replace in /admin"
        bbox2 = draw.textbbox((0, 0), sub, font=sub_font)
        sw, sh = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
        draw.text(((width - sw) / 2, height / 2 + th - 10), sub, font=sub_font, fill=(255, 255, 255, 220))

        img.save(path, "JPEG", quality=85)
        return filename
    except Exception:
        return None


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS site_content (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            overview_text TEXT NOT NULL,
            address TEXT NOT NULL,
            phone1 TEXT NOT NULL,
            phone2 TEXT NOT NULL,
            whatsapp_number TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rooms_display (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            floor TEXT NOT NULL DEFAULT 'Ground Floor',
            price_per_night REAL NOT NULL,
            photo_filename TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin'  -- admin | manager
        )
    """)

    # Date ranges a room is NOT available for, pushed from the front-desk
    # app's /sync/rooms endpoint (see below). room_name is matched loosely
    # against rooms_display.name, the same join-by-name convention the
    # existing rooms_export/import feature already uses — there's no
    # foreign key, since the two apps' databases are entirely separate.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS availability_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_name TEXT NOT NULL,
            check_in TEXT NOT NULL,
            check_out TEXT NOT NULL
        )
    """)

    # A single-row table tracking when the last sync from the front-desk
    # app landed, so the Rooms page can tell a visitor how fresh (or
    # stale) the availability information is, rather than presenting it as
    # if it were guaranteed live.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_synced_at TEXT,
            rooms_count INTEGER,
            blocks_count INTEGER
        )
    """)
    cur.execute("SELECT COUNT(*) FROM sync_meta")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO sync_meta (id, last_synced_at, rooms_count, blocks_count) VALUES (1, NULL, 0, 0)")

    cur.execute("SELECT COUNT(*) FROM site_content")
    if cur.fetchone()[0] == 0:
        cur.execute(
            """INSERT INTO site_content (id, overview_text, address, phone1, phone2, whatsapp_number)
               VALUES (1, ?, ?, ?, ?, ?)""",
            (
                "Mario Suites Ltd is a comfortable, well-kept hotel in Kano, offering "
                "clean, simply furnished rooms and a warm welcome just opposite BUK New Site. "
                "Whether you're in town for business or visiting family, we aim to make your stay easy and pleasant.",
                "23/24 Danbare Layin Mobil, Opp. BUK New Site, Kano, Nigeria",
                "+234 701 254 9345",
                "+234 814 708 5305",
                "2347012549345",
            ),
        )

    # Insert any demo room not already present (by name), instead of only
    # seeding on a brand-new database — this tops up a site.db that was
    # created by an earlier version of the app (e.g. one that only had
    # A1-A3) instead of silently leaving it short. Existing rooms, and any
    # photo an admin has already uploaded, are left completely alone.
    existing_names = {row["name"] for row in cur.execute("SELECT name FROM rooms_display").fetchall()}
    for name, floor, price in SEED_ROOMS:
        if name not in existing_names:
            photo_filename = generate_placeholder_photo(name, floor)
            cur.execute(
                "INSERT INTO rooms_display (name, floor, price_per_night, photo_filename) VALUES (?, ?, ?, ?)",
                (name, floor, price, photo_filename),
            )

    cur.execute("SELECT COUNT(*) FROM admin_users")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, 'admin')",
            ("admin", generate_password_hash("admin123")),
        )

    conn.commit()
    conn.close()


def get_site_content(conn=None):
    close_after = conn is None
    conn = conn or get_db()
    row = conn.execute("SELECT * FROM site_content WHERE id = 1").fetchone()
    if close_after:
        conn.close()
    return row


def wa_link(phone_digits, text):
    """A free wa.me click-to-chat link — no WhatsApp Business API, no
    Twilio, no paid integration of any kind."""
    return f"https://wa.me/{phone_digits}?text={quote(text)}"


def tel_link(phone_display):
    digits = "".join(ch for ch in phone_display if ch.isdigit() or ch == "+")
    return f"tel:{digits}"


def maps_embed_url(address):
    """Embed the map centred on the hotel's exact coordinates (precise pin)
    rather than geocoding the text address (which can land nearby but not
    exactly on the building). Falls back to the address string if the
    coordinates are ever blanked out."""
    if HOTEL_LAT and HOTEL_LNG:
        query = f"{HOTEL_LAT},{HOTEL_LNG}"
    else:
        query = address
    return "https://www.google.com/maps?" + urlencode({"q": query, "output": "embed"})


def maps_directions_url(address):
    """Directions to the hotel's exact coordinates, no API key required."""
    if HOTEL_LAT and HOTEL_LNG:
        destination = f"{HOTEL_LAT},{HOTEL_LNG}"
    else:
        destination = address
    return "https://www.google.com/maps/dir/?" + urlencode({"api": "1", "destination": destination})


def time_ago(timestamp_str):
    """Render an ISO timestamp (e.g. sync_meta.last_synced_at) as a short
    relative time like "3 minutes ago" or "2 hours ago", for the honest
    "availability last confirmed ..." note on the Rooms page."""
    if not timestamp_str:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_str)
    except ValueError:
        return None
    seconds = (datetime.now() - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


app.jinja_env.filters["time_ago"] = time_ago


@app.context_processor
def inject_globals():
    content = get_site_content()
    conn = get_db()
    sync_row = conn.execute("SELECT * FROM sync_meta WHERE id = 1").fetchone()
    conn.close()
    return {
        "hotel_name": HOTEL_NAME,
        "site": content,
        "wa_link": wa_link,
        "tel_link": tel_link,
        "current_admin": session.get("admin_username"),
        "sync_meta": sync_row,
        "sync_configured": bool(SYNC_API_KEY),
    }


# ---------------------------------------------------------------------------
# Admin auth (separate login from the front-desk app, same admin/manager
# permission model: both roles may edit the site's content)
# ---------------------------------------------------------------------------

def admin_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "admin_id" not in session:
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if "admin_id" in session:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        user = conn.execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["admin_id"] = user["id"]
            session["admin_username"] = user["username"]
            flash(f"Welcome back, {user['username']}.", "success")
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        flash("Incorrect username or password.", "error")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Sync from the front-desk app
#
# The front-desk app (a separate, normally-offline program on the hotel's
# own computer) pushes its room list and currently-occupied date ranges
# here whenever it has internet, so the Rooms page can answer "is Room X
# free from date A to date B". This is a one-way, pull-free push: this
# site never reaches out to the front desk (it has no way to — that PC
# isn't reachable from the internet), it only accepts what gets sent here.
# ---------------------------------------------------------------------------

@app.route("/sync/rooms", methods=["POST"])
def sync_rooms():
    if not SYNC_API_KEY:
        return jsonify({"status": "error", "error": "Sync is not configured on this site (SYNC_API_KEY is unset)."}), 503

    if request.headers.get("X-Sync-Key") != SYNC_API_KEY:
        return jsonify({"status": "error", "error": "Invalid sync key."}), 401

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        payload = None
    if not isinstance(payload, dict) or "rooms" not in payload or "blocks" not in payload:
        return jsonify({"status": "error", "error": "Expected JSON body with 'rooms' and 'blocks'."}), 400

    rooms = payload.get("rooms") or []
    blocks = payload.get("blocks") or []
    if not isinstance(rooms, list) or not isinstance(blocks, list):
        return jsonify({"status": "error", "error": "'rooms' and 'blocks' must both be lists."}), 400

    conn = get_db()

    # Upsert each room by name — same behavior as the existing rooms_export
    # JSON import: update floor/price for a name already here, leave its
    # photo untouched; insert new names with a fresh placeholder photo,
    # same as the initial demo seeding does. Rooms NOT present in this
    # payload are left alone (never deleted) — an admin may have added a
    # website-only room that the front desk doesn't know about.
    existing_names = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM rooms_display").fetchall()}
    for entry in rooms:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        floor = (entry.get("floor") or "Ground Floor").strip()
        price = entry.get("price_per_night")
        if not name or price is None:
            continue
        if name in existing_names:
            conn.execute(
                "UPDATE rooms_display SET floor = ?, price_per_night = ? WHERE id = ?",
                (floor, price, existing_names[name]),
            )
        else:
            photo_filename = generate_placeholder_photo(name, floor)
            conn.execute(
                "INSERT INTO rooms_display (name, floor, price_per_night, photo_filename) VALUES (?, ?, ?, ?)",
                (name, floor, price, photo_filename),
            )

    # Availability blocks are a full snapshot each time (simplest correct
    # approach — the front desk always sends its complete current picture,
    # not a diff), so replace the whole table rather than trying to merge.
    conn.execute("DELETE FROM availability_blocks")
    blocks_inserted = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        room_name = (b.get("room_name") or "").strip()
        check_in = (b.get("check_in") or "").strip()
        check_out = (b.get("check_out") or "").strip()
        if not (room_name and check_in and check_out):
            continue
        conn.execute(
            "INSERT INTO availability_blocks (room_name, check_in, check_out) VALUES (?, ?, ?)",
            (room_name, check_in, check_out),
        )
        blocks_inserted += 1

    conn.execute(
        "UPDATE sync_meta SET last_synced_at = ?, rooms_count = ?, blocks_count = ? WHERE id = 1",
        (datetime.now().isoformat(timespec="seconds"), len(rooms), blocks_inserted),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "rooms_synced": len(rooms), "blocks_synced": blocks_inserted})


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    content = get_site_content()
    intro_message = f"Hi, I'd like to enquire about a room at {HOTEL_NAME}."
    return render_template(
        "home.html",
        intro_wa_link=wa_link(content["whatsapp_number"], intro_message),
        intro_tel_link=tel_link(content["phone1"]),
    )


@app.route("/rooms")
def rooms_page():
    conn = get_db()
    rows = conn.execute("SELECT * FROM rooms_display ORDER BY floor, name").fetchall()
    content = get_site_content(conn)

    check_in = (request.args.get("check_in") or "").strip()
    check_out = (request.args.get("check_out") or "").strip()
    searching = False
    date_error = None

    if check_in and check_out:
        try:
            ci = date.fromisoformat(check_in)
            co = date.fromisoformat(check_out)
            if co <= ci:
                date_error = "Check-out must be after check-in."
            else:
                searching = True
        except ValueError:
            date_error = "Please enter valid dates."

    available_rooms = []
    unavailable_rooms = []

    if searching:
        # Pull every block once and test each room against it in Python —
        # simpler and plenty fast for a hotel-sized room list, rather than
        # a fiddly date-range SQL join. A room is unavailable for the
        # requested range if ANY block for it overlaps that range (the
        # standard interval-overlap test: block starts before the
        # requested check-out AND ends after the requested check-in).
        blocks = conn.execute("SELECT room_name, check_in, check_out FROM availability_blocks").fetchall()
        blocked_names = set()
        for b in blocks:
            try:
                b_in = date.fromisoformat(b["check_in"])
                b_out = date.fromisoformat(b["check_out"])
            except ValueError:
                continue
            if b_in < co and b_out > ci:
                blocked_names.add(b["room_name"])

        date_label = f"{ci.strftime('%d %b')} to {co.strftime('%d %b %Y')}"
        for r in rows:
            if r["name"] in blocked_names:
                unavailable_rooms.append({"room": r})
            else:
                message = f"Hi, I'd like to book Room {r['name']} at {HOTEL_NAME} from {date_label}."
                available_rooms.append({
                    "room": r,
                    "book_link": wa_link(content["whatsapp_number"], message),
                })
    else:
        # No date range given (or it was invalid) — just show every room,
        # the same plain list the page always showed before this feature.
        for r in rows:
            message = f"Hi, I'd like to book Room {r['name']} at {HOTEL_NAME}."
            available_rooms.append({
                "room": r,
                "book_link": wa_link(content["whatsapp_number"], message),
            })

    sync_row = conn.execute("SELECT * FROM sync_meta WHERE id = 1").fetchone()
    conn.close()

    # Flag the availability info as possibly stale if it's never synced, or
    # hasn't synced in the last 6 hours — the front-desk PC may simply have
    # been offline that whole time, so this isn't necessarily broken, just
    # worth a visitor double-checking by WhatsApp before counting on it.
    stale = True
    if sync_row and sync_row["last_synced_at"]:
        try:
            age_hours = (datetime.now() - datetime.fromisoformat(sync_row["last_synced_at"])).total_seconds() / 3600
            stale = age_hours > 6
        except ValueError:
            stale = True

    return render_template(
        "rooms.html",
        rooms_with_links=available_rooms,  # unfiltered view keeps using this name, unchanged from before
        searching=searching,
        unavailable_rooms=unavailable_rooms,
        check_in=check_in,
        check_out=check_out,
        date_error=date_error,
        sync_last_synced_at=sync_row["last_synced_at"] if sync_row else None,
        sync_stale=stale,
    )


@app.route("/location")
def location_page():
    content = get_site_content()
    message = f"Hi, I'd like to enquire about a room at {HOTEL_NAME}."
    return render_template(
        "location.html",
        map_embed_url=maps_embed_url(content["address"]),
        directions_url=maps_directions_url(content["address"]),
        wa_url=wa_link(content["whatsapp_number"], message),
        tel_url=tel_link(content["phone1"]),
    )


@app.route("/static/room_photos/<path:filename>")
def room_photo(filename):
    return send_from_directory(ROOM_PHOTOS_DIR, filename)


# ---------------------------------------------------------------------------
# Admin dashboard: edit site content, rooms, photos; import rooms JSON
# exported from the front-desk app's Rooms page.
# ---------------------------------------------------------------------------

@app.route("/admin")
@admin_login_required
def admin_dashboard():
    conn = get_db()
    content = get_site_content(conn)
    rooms = conn.execute("SELECT * FROM rooms_display ORDER BY floor, name").fetchall()
    conn.close()
    return render_template("admin_dashboard.html", content=content, rooms=rooms)


@app.route("/admin/content", methods=["POST"])
@admin_login_required
def admin_update_content():
    overview_text = (request.form.get("overview_text") or "").strip()
    address = (request.form.get("address") or "").strip()
    phone1 = (request.form.get("phone1") or "").strip()
    phone2 = (request.form.get("phone2") or "").strip()
    whatsapp_number = (request.form.get("whatsapp_number") or "").strip()

    if not (overview_text and address and phone1 and whatsapp_number):
        flash("Overview, address, phone 1 and WhatsApp number are required.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_db()
    conn.execute(
        """UPDATE site_content SET overview_text = ?, address = ?, phone1 = ?, phone2 = ?, whatsapp_number = ?
           WHERE id = 1""",
        (overview_text, address, phone1, phone2, whatsapp_number),
    )
    conn.commit()
    conn.close()
    flash("Site content updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/<int:room_id>/edit", methods=["POST"])
@admin_login_required
def admin_edit_room(room_id):
    name = (request.form.get("name") or "").strip()
    floor = (request.form.get("floor") or "").strip() or "Ground Floor"
    try:
        price = float(str(request.form.get("price_per_night") or "0").replace(",", ""))
    except ValueError:
        flash("Please enter a valid price.", "error")
        return redirect(url_for("admin_dashboard"))

    if not name:
        flash("Room name is required.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_db()
    conn.execute(
        "UPDATE rooms_display SET name = ?, floor = ?, price_per_night = ? WHERE id = ?",
        (name, floor, price, room_id),
    )

    photo = request.files.get("photo")
    if photo and photo.filename:
        ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
        if ext in ALLOWED_PHOTO_EXTENSIONS:
            filename = secure_filename(f"room_{room_id}.{ext}")
            photo.save(os.path.join(ROOM_PHOTOS_DIR, filename))
            conn.execute("UPDATE rooms_display SET photo_filename = ? WHERE id = ?", (filename, room_id))
        else:
            flash("Photo must be a PNG, JPG or WEBP file.", "error")

    conn.commit()
    conn.close()
    flash("Room updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/add", methods=["POST"])
@admin_login_required
def admin_add_room():
    name = (request.form.get("name") or "").strip()
    floor = (request.form.get("floor") or "").strip() or "Ground Floor"
    try:
        price = float(str(request.form.get("price_per_night") or "0").replace(",", ""))
    except ValueError:
        flash("Please enter a valid price.", "error")
        return redirect(url_for("admin_dashboard"))

    if not name:
        flash("Room name is required.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_db()
    conn.execute(
        "INSERT INTO rooms_display (name, floor, price_per_night, photo_filename) VALUES (?, ?, ?, NULL)",
        (name, floor, price),
    )
    conn.commit()
    conn.close()
    flash(f"Room '{name}' added.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/<int:room_id>/delete", methods=["POST"])
@admin_login_required
def admin_delete_room(room_id):
    """Remove a room from the public site — e.g. one of the seeded demo
    rooms you don't actually have. This only affects this site's own
    rooms_display table; it has no effect on the front-desk app's room
    catalog. Also deletes its photo file from disk, if it has one."""
    conn = get_db()
    room = conn.execute("SELECT * FROM rooms_display WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        flash("Room not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if room["photo_filename"]:
        photo_path = os.path.join(ROOM_PHOTOS_DIR, room["photo_filename"])
        if os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except OSError:
                pass

    conn.execute("DELETE FROM rooms_display WHERE id = ?", (room_id,))
    conn.commit()
    conn.close()
    flash(f"Room '{room['name']}' deleted from the website.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/<int:room_id>/remove-photo", methods=["POST"])
@admin_login_required
def admin_remove_room_photo(room_id):
    """Clear a room's photo (e.g. a seeded demo placeholder) so the card
    falls back to the plain CSS placeholder, without deleting the room
    itself. Deletes the old file from disk too."""
    conn = get_db()
    room = conn.execute("SELECT * FROM rooms_display WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        flash("Room not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if room["photo_filename"]:
        photo_path = os.path.join(ROOM_PHOTOS_DIR, room["photo_filename"])
        if os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except OSError:
                pass
        conn.execute("UPDATE rooms_display SET photo_filename = NULL WHERE id = ?", (room_id,))
        conn.commit()
        flash(f"Photo removed from '{room['name']}'.", "success")
    else:
        flash(f"'{room['name']}' has no photo to remove.", "error")
    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/import", methods=["POST"])
@admin_login_required
def admin_import_rooms():
    """Import the rooms_export.json file downloaded from the front-desk
    app's Rooms page (Admin/Manager only there too). Overwrites each
    matching room's name/floor/price by name; leaves photos untouched.
    New room names not seen before are added. This is a deliberate manual
    step since the two apps run on different, disconnected machines."""
    upload = request.files.get("rooms_file")
    if not upload or not upload.filename:
        flash("Choose a rooms_export.json file first.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        data = json.loads(upload.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        flash("That file isn't valid JSON.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_db()
    updated, added = 0, 0
    for entry in data:
        name = (entry.get("name") or "").strip()
        floor = (entry.get("floor") or "Ground Floor").strip()
        price = entry.get("price_per_night")
        if not name or price is None:
            continue
        existing = conn.execute("SELECT id FROM rooms_display WHERE name = ?", (name,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE rooms_display SET floor = ?, price_per_night = ? WHERE id = ?",
                (floor, price, existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO rooms_display (name, floor, price_per_night, photo_filename) VALUES (?, ?, ?, NULL)",
                (name, floor, price),
            )
            added += 1
    conn.commit()
    conn.close()
    flash(f"Rooms imported: {updated} updated, {added} added.", "success")
    return redirect(url_for("admin_dashboard"))


# Create the tables (and seed the default content/admin/rooms) as soon as
# this module is imported, not only when run directly. A production server
# like gunicorn imports this file as `app:app` and never executes the
# `__main__` block below, so if init_db() only ran there, site.db would
# never get created on a host like Render.
init_db()

# A real secret key should be set via the SECRET_KEY environment variable
# in production (e.g. in Render's dashboard); this is only a safe-enough
# fallback for local development.
app.secret_key = os.environ.get("SECRET_KEY", app.secret_key)

if __name__ == "__main__":
    # Local development only. In production, a WSGI server such as
    # gunicorn runs this app instead (see Procfile / README) and binds to
    # the host's own $PORT, so this block is skipped entirely there.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5001)), debug=True)
