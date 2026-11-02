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
from datetime import datetime
from functools import wraps
from urllib.parse import quote, urlencode

from flask import (Flask, flash, redirect, render_template, request,
                    send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

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

    cur.execute("SELECT COUNT(*) FROM rooms_display")
    if cur.fetchone()[0] == 0:
        for name, floor, price in [("A1", "Ground Floor", 20000), ("A2", "Ground Floor", 20000), ("A3", "Ground Floor", 20000)]:
            cur.execute(
                "INSERT INTO rooms_display (name, floor, price_per_night, photo_filename) VALUES (?, ?, ?, NULL)",
                (name, floor, price),
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


@app.context_processor
def inject_globals():
    content = get_site_content()
    return {
        "hotel_name": HOTEL_NAME,
        "site": content,
        "wa_link": wa_link,
        "tel_link": tel_link,
        "current_admin": session.get("admin_username"),
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
    conn.close()

    rooms_with_links = []
    for r in rows:
        message = f"Hi, I'd like to book Room {r['name']} at {HOTEL_NAME}."
        rooms_with_links.append({
            "room": r,
            "book_link": wa_link(content["whatsapp_number"], message),
        })
    return render_template("rooms.html", rooms_with_links=rooms_with_links)


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
