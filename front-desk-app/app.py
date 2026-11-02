"""
Mario Suites Ltd — Front Desk Management System
--------------------------------------------------
A local web application for managing guests, rooms, bookings, reservations
and payments at the front desk. Data is stored in a local SQLite database
file (hotel.db) that lives in the same folder as this script, so everything
stays on this computer. No internet connection is needed to run this app.

Run it with:  python app.py
Then open:    http://127.0.0.1:5000 in your browser.

See the project's README.md for full setup instructions, including how to
run this permanently in the background on Windows via NSSM.
"""

import io
import json
import os
import sqlite3
import threading
import time as time_module
import urllib.error
import urllib.request
from datetime import date, datetime
from datetime import time as dtime
from functools import wraps

from flask import Flask, Response, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

app = Flask(__name__)
# IMPORTANT: change this to a long random string before putting this app in
# real use (see README.md). Anyone who knows this value could forge login
# sessions.
app.secret_key = "mario-suites-ltd-change-this-secret-key"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hotel.db")

# Hotel details shown on receipts and around the app. Change these here if
# the hotel's contact details ever change.
HOTEL_NAME = "Mario Suites Ltd"
HOTEL_ADDRESS = "23/24 Danbare Layin Mobil, Opp. BUK New Site, Kano, Nigeria"
HOTEL_PHONES = ["+234 701 254 9345", "+234 814 708 5305"]

# Demo rooms, named in the hotel's own A/B-plus-floor convention. These are
# seeded automatically (any entry not already in the database, by room
# number, is added — so this also safely tops up a database that already
# exists from an earlier run) purely so there's something realistic to
# click through before the hotel goes live. Rename, reprice, add, or
# delete any of them freely from the Rooms page (Admin/Manager only) —
# none of this is hard-coded elsewhere.
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

# Forms of ID the front desk can record for a guest, matching what's
# commonly accepted for hotel guest registration in Nigeria.
ID_TYPES = [
    "National ID (NIN)",
    "International Passport",
    "Driver's License",
    "Voter's Card",
    "Other",
]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Open a connection to the SQLite database for this request."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they don't exist yet, and seed rooms + admin."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            address TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Hotel-standard guest registration fields, added after the app first
    # shipped with just name/phone/email/address — this tops up an older
    # hotel.db safely, the same way the bookings table's columns below do.
    # Every one of these is optional: Name and Phone stay the only required
    # fields, so front-desk staff can still check a guest in quickly even
    # when ID isn't on hand yet, and fill these in later via "Edit guest".
    existing_guest_cols = {row["name"] for row in cur.execute("PRAGMA table_info(guests)").fetchall()}
    new_guest_columns = {
        "nationality": "TEXT",         # defaults to 'Nigerian' in the app, not here, so old rows aren't silently relabeled
        "id_type": "TEXT",             # one of ID_TYPES above
        "id_number": "TEXT",
        "occupation": "TEXT",
        "next_of_kin_name": "TEXT",
        "next_of_kin_phone": "TEXT",
        "coming_from": "TEXT",          # where the guest arrived from (city/address)
        "going_to": "TEXT",             # where the guest is headed next
    }
    for col, col_type in new_guest_columns.items():
        if col not in existing_guest_cols:
            cur.execute(f"ALTER TABLE guests ADD COLUMN {col} {col_type}")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_number TEXT NOT NULL UNIQUE,
            floor TEXT NOT NULL DEFAULT 'Ground Floor',
            price_per_night REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'available'  -- available | occupied
        )
    """)
    # Add the floor column if this is an older database that doesn't have it.
    existing_room_cols = {row["name"] for row in cur.execute("PRAGMA table_info(rooms)").fetchall()}
    if "floor" not in existing_room_cols:
        cur.execute("ALTER TABLE rooms ADD COLUMN floor TEXT NOT NULL DEFAULT 'Ground Floor'")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_id INTEGER NOT NULL,
            room_id INTEGER NOT NULL,
            check_in TEXT NOT NULL,
            expected_check_out TEXT NOT NULL,
            actual_check_out TEXT,
            nights INTEGER NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'checked_in',  -- checked_in | checked_out
            created_at TEXT NOT NULL,
            FOREIGN KEY (guest_id) REFERENCES guests (id),
            FOREIGN KEY (room_id) REFERENCES rooms (id)
        )
    """)

    # Add newer columns to bookings if this is an older database that
    # doesn't have them yet (payment + early/late checkout settlement).
    # Note: there is no security-deposit column here — Mario Suites Ltd does
    # not use a security deposit, unlike the system this was adapted from.
    existing_cols = {row["name"] for row in cur.execute("PRAGMA table_info(bookings)").fetchall()}
    new_columns = {
        "checkout_time": "TEXT",            # full datetime of actual checkout
        "actual_nights": "INTEGER",         # nights actually charged for, set at checkout
        "actual_amount": "REAL",            # amount actually owed, set at checkout
        "refund_amount": "REAL DEFAULT 0",  # money owed back to the guest
        "balance_due": "REAL DEFAULT 0",    # extra money the guest still owes
        "payment_status": "TEXT DEFAULT 'pending'",  # pending | partial | paid
        "credit_issued": "REAL DEFAULT 0",  # refund converted to store credit at checkout
        "credit_applied": "REAL DEFAULT 0",  # store credit redeemed against this booking
        "checked_in_by": "TEXT",            # username who created the booking (checked the guest in)
        "checked_out_by": "TEXT",           # username who checked the guest out
    }
    for col, col_type in new_columns.items():
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE bookings ADD COLUMN {col} {col_type}")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,        -- cash | transfer | pos | credit
            kind TEXT NOT NULL DEFAULT 'payment',  -- payment | refund
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (booking_id) REFERENCES bookings (id)
        )
    """)

    # Store credit a guest is owed (e.g. a refund left unsettled on purpose
    # at checkout) that can be redeemed against a future booking.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guest_credits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_id INTEGER NOT NULL,
            source_booking_id INTEGER,
            amount REAL NOT NULL,
            remaining_amount REAL NOT NULL,
            created_at TEXT NOT NULL,
            note TEXT,
            FOREIGN KEY (guest_id) REFERENCES guests (id),
            FOREIGN KEY (source_booking_id) REFERENCES bookings (id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_id INTEGER NOT NULL,
            room_id INTEGER NOT NULL,
            check_in TEXT NOT NULL,
            expected_check_out TEXT NOT NULL,
            nights INTEGER NOT NULL,
            total_amount REAL NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | converted | cancelled
            converted_booking_id INTEGER,
            created_by TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (guest_id) REFERENCES guests (id),
            FOREIGN KEY (room_id) REFERENCES rooms (id),
            FOREIGN KEY (converted_booking_id) REFERENCES bookings (id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',  -- admin | manager | staff
            created_at TEXT NOT NULL
        )
    """)

    # Simple key/value store for app-wide settings — currently just the
    # website sync config (see "Website sync" section below). A plain
    # key/value table is overkill-proof: adding a new setting later never
    # needs a schema migration.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Seed demo rooms — insert any SEED_ROOMS entry not already present (by
    # room number), rather than only on a brand-new database. That way,
    # re-running this on a database from an earlier version of the app
    # (e.g. one that only had A1-A3) tops it up with the rest instead of
    # silently doing nothing.
    existing_room_numbers = {
        row["room_number"] for row in cur.execute("SELECT room_number FROM rooms").fetchall()
    }
    for room_number, floor, price in SEED_ROOMS:
        if room_number not in existing_room_numbers:
            cur.execute(
                "INSERT INTO rooms (room_number, floor, price_per_night, status) VALUES (?, ?, ?, 'available')",
                (room_number, floor, price),
            )

    # Seed a default admin account the very first time the app runs, so
    # there's always a way to log in. Change this password right away from
    # the Profile page (or Users page) once logged in.
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            ("admin", generate_password_hash("admin123"), datetime.now().isoformat(timespec="seconds")),
        )

    conn.commit()
    conn.close()


def parse_flexible_datetime(value):
    """Parse a stored check-in / check-out value.

    Newer bookings store a date *and* time (e.g. "2026-06-20T14:30").
    Older bookings, created before exact times were tracked, only have a
    bare date (e.g. "2026-06-20"). This accepts either.
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date/time value: {value!r}")


def format_dt_display(value):
    """Jinja filter: render a stored check-in/check-out value, showing the
    exact time alongside the date whenever one was recorded."""
    if not value:
        return ""
    try:
        dt = parse_flexible_datetime(value)
    except ValueError:
        return value
    has_time = "T" in value or (":" in value)
    if has_time:
        return dt.strftime("%d %b %Y, %I:%M %p")
    return dt.strftime("%d %b %Y")


def format_time_ago(value):
    """Jinja filter: render an ISO timestamp (e.g. a sync's last_sync_at)
    as a short relative time like "3 minutes ago" or "2 hours ago"."""
    if not value:
        return "never"
    try:
        dt = parse_flexible_datetime(value)
    except ValueError:
        return value
    seconds = (datetime.now() - dt).total_seconds()
    if seconds < 0:
        return "just now"
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


app.jinja_env.filters["fmt_dt"] = format_dt_display
app.jinja_env.filters["time_ago"] = format_time_ago


# ---------------------------------------------------------------------------
# Authentication & roles
#
# Three roles: admin (full access), manager (rooms/prices + revenue
# reports, no user management), staff (day-to-day front-desk work only).
# ---------------------------------------------------------------------------

def current_user():
    if "user_id" not in session:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*allowed_roles):
    """Restrict a route to one or more roles, e.g. @roles_required('admin')
    or @roles_required('admin', 'manager')."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login", next=request.path))
            if session.get("role") not in allowed_roles:
                flash("That action is restricted to " + " / ".join(r.capitalize() for r in allowed_roles) + " profiles.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


# Kept as a thin alias so admin-only routes read clearly.
def admin_required(view):
    return roles_required("admin")(view)


@app.context_processor
def inject_user():
    return {
        "current_username": session.get("username"),
        "current_role": session.get("role"),
        "is_admin": session.get("role") == "admin",
        "is_manager_or_admin": session.get("role") in ("admin", "manager"),
        "session_user_id": session.get("user_id"),
        "hotel_name": HOTEL_NAME,
        "hotel_address": HOTEL_ADDRESS,
        "hotel_phones": HOTEL_PHONES,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            next_url = request.args.get("next") or url_for("dashboard")
            flash(f"Welcome back, {user['username']}.", "success")
            return redirect(next_url)

        flash("Incorrect username or password.", "error")
        return render_template("login.html", form=request.form)

    return render_template("login.html", form={})


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Users (admin-only: create Manager/Staff accounts, e.g. for shifts)
# ---------------------------------------------------------------------------

@app.route("/users")
@admin_required
def users_page():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END, username"
    ).fetchall()
    conn.close()
    return render_template("users.html", users=rows)


@app.route("/users/add", methods=["GET", "POST"])
@admin_required
def add_user():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff")
        if role not in ("admin", "manager", "staff"):
            role = "staff"

        error = None
        if not username or not password:
            error = "Username and password are required."
        elif len(password) < 4:
            error = "Password must be at least 4 characters."

        if not error:
            conn = get_db()
            existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                error = "That username is already taken."
            conn.close()

        if error:
            flash(error, "error")
            return render_template("add_user.html", form=request.form)

        conn = get_db()
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()
        flash(f"User '{username}' created as {role}.", "success")
        return redirect(url_for("users_page"))

    return render_template("add_user.html", form={})


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    if user_id == session.get("user_id"):
        flash("You can't delete the account you're currently logged in as.", "error")
        return redirect(url_for("users_page"))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user and user["role"] == "admin":
        remaining_admins = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0]
        if remaining_admins <= 1:
            conn.close()
            flash("At least one Admin account must remain.", "error")
            return redirect(url_for("users_page"))

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User removed.", "success")
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@admin_required
def reset_password(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if user is None:
        conn.close()
        flash("User not found.", "error")
        return redirect(url_for("users_page"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        error = None
        if len(new_password) < 4:
            error = "Password must be at least 4 characters."
        elif new_password != confirm_password:
            error = "The two passwords don't match."

        if error:
            conn.close()
            flash(error, "error")
            return render_template("reset_password.html", user=user)

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user_id),
        )
        conn.commit()
        conn.close()
        flash(f"Password updated for '{user['username']}'.", "success")
        return redirect(url_for("users_page"))

    conn.close()
    return render_template("reset_password.html", user=user)


# ---------------------------------------------------------------------------
# Profile (every logged-in user: change their own password)
# ---------------------------------------------------------------------------

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

        error = None
        if not user or not check_password_hash(user["password_hash"], current_password):
            error = "Current password is incorrect."
        elif len(new_password) < 4:
            error = "New password must be at least 4 characters."
        elif new_password != confirm_password:
            error = "The two new passwords don't match."

        if error:
            conn.close()
            flash(error, "error")
            return render_template("profile.html")

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), session["user_id"]),
        )
        conn.commit()
        conn.close()
        flash("Your password has been updated.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html")


@app.route("/admin/reset-demo-data", methods=["POST"])
@roles_required("admin")
def reset_demo_data():
    """Wipe every guest, booking, reservation, payment and store-credit
    record — meant to be run once, by the Admin, right before the hotel
    starts using the software for real, to clear out anything created
    while testing/demoing. Deliberately leaves the room catalog (so real
    room names/prices configured during testing aren't lost) and every
    user account (so nobody gets locked out) untouched — delete/edit
    rooms individually from the Rooms page if some were only ever demo
    rooms. Requires typing the exact confirmation phrase to reduce the
    chance of an accidental click wiping real guest data later."""
    confirmation = (request.form.get("confirmation") or "").strip()
    if confirmation != "WIPE ALL DATA":
        flash('Typed confirmation did not match — type exactly "WIPE ALL DATA" to proceed. Nothing was deleted.', "error")
        return redirect(url_for("profile"))

    conn = get_db()
    counts = {
        "payments": conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
        "guest_credits": conn.execute("SELECT COUNT(*) FROM guest_credits").fetchone()[0],
        "bookings": conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0],
        "reservations": conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
        "guests": conn.execute("SELECT COUNT(*) FROM guests").fetchone()[0],
    }
    conn.execute("DELETE FROM payments")
    conn.execute("DELETE FROM guest_credits")
    conn.execute("DELETE FROM bookings")
    conn.execute("DELETE FROM reservations")
    conn.execute("DELETE FROM guests")
    # Every room goes back to available, since any "occupied" status only
    # existed because of a booking that's now gone.
    conn.execute("UPDATE rooms SET status = 'available'")
    conn.commit()
    conn.close()

    flash(
        "All demo data wiped: {guests} guest(s), {bookings} booking(s), {reservations} reservation(s), "
        "{payments} payment(s) and {guest_credits} store-credit record(s) deleted. Rooms and user accounts "
        "were kept — all rooms are now marked available. The hotel is ready for real guests.".format(**counts),
        "success",
    )
    return redirect(url_for("profile"))


# ---------------------------------------------------------------------------
# Settings (admin-only: website sync configuration)
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings_page():
    conn = get_db()

    if request.method == "POST":
        website_sync_url = (request.form.get("website_sync_url") or "").strip().rstrip("/")
        website_sync_api_key = (request.form.get("website_sync_api_key") or "").strip()

        # Only overwrite each stored value if something was actually typed —
        # an empty submission means "leave it as it is", not "clear it", for
        # both fields (the API key field is blank-by-design every time it's
        # shown, since it's a password input; the URL field can also end up
        # submitted blank if the browser's placeholder example text is
        # mistaken for an already-filled value, so it gets the same
        # protection rather than silently wiping a good URL).
        if website_sync_url:
            set_setting(conn, "website_sync_url", website_sync_url)
        if website_sync_api_key:
            set_setting(conn, "website_sync_api_key", website_sync_api_key)

        if not website_sync_url and not get_setting(conn, "website_sync_url", ""):
            flash("Settings saved, but the Website sync URL is still empty — type it in and save again.", "error")
        else:
            flash("Settings saved.", "success")
        conn.commit()
        conn.close()
        return redirect(url_for("settings_page"))

    website_sync_url = get_setting(conn, "website_sync_url", "")
    has_api_key = bool(get_setting(conn, "website_sync_api_key", ""))
    last_sync_status = get_setting(conn, "last_sync_status")
    last_sync_at = get_setting(conn, "last_sync_at")
    conn.close()

    return render_template(
        "settings.html",
        website_sync_url=website_sync_url,
        has_api_key=has_api_key,
        last_sync_status=last_sync_status,
        last_sync_at=last_sync_at,
    )


@app.route("/settings/sync-now", methods=["POST"])
@roles_required("admin")
def sync_now():
    """Trigger an immediate sync attempt and wait for the result, so the
    Admin gets instant feedback (unlike the background loop/post-booking
    syncs, which are fire-and-forget). Uses a much longer timeout than
    those do, since a free-tier Render site that's been asleep can take
    up to ~50 seconds to wake up and answer — an admin clicking this
    button is deliberately waiting, so it's worth riding that out instead
    of giving up after a few seconds."""
    ok = sync_to_website(timeout=55)
    conn = get_db()
    status = get_setting(conn, "last_sync_status")
    conn.close()
    if ok:
        flash(f"Synced successfully: {status}.", "success")
    else:
        flash(f"Sync did not complete: {status}. If the website had been asleep, try Sync now once more — it should be awake now.", "error")
    return redirect(url_for("settings_page"))


# ---------------------------------------------------------------------------
# Shared helpers: nights, money, payments, store credit
# ---------------------------------------------------------------------------

# Standard hotel-industry checkout time. Checking out any time after this
# means the room was occupied into that day, so it is billed as a full
# extra night.
STANDARD_CHECKOUT_TIME = dtime(12, 0)


def calculate_nights(check_in_str, check_out_str):
    """Estimate nights for a booking/reservation total, the same way the
    final bill will be calculated: any expected checkout time after noon
    counts as a full extra night. If no checkout time was given (e.g. a
    reservation made before a precise time is known), this falls back to
    a plain calendar-day difference."""
    check_in = parse_flexible_datetime(check_in_str)
    check_out = parse_flexible_datetime(check_out_str)
    nights = (check_out.date() - check_in.date()).days
    checkout_has_time = "T" in check_out_str or ":" in check_out_str
    if checkout_has_time and check_out.time() > STANDARD_CHECKOUT_TIME:
        nights += 1
    return nights


def calculate_actual_nights(check_in_str, checkout_dt):
    """How many nights to actually bill for, given a real checkout datetime.
    Any checkout after 12:00pm (noon) on a given day counts as a stay for
    that whole day, per standard hospitality practice."""
    check_in_date = parse_flexible_datetime(check_in_str).date()
    nights = (checkout_dt.date() - check_in_date).days
    if checkout_dt.time() > STANDARD_CHECKOUT_TIME:
        nights += 1
    # A guest is always billed for at least one night.
    return max(nights, 1)


def parse_amount(value):
    """Parse a money amount typed with comma thousands separators (e.g.
    "20,000.50"). Raises ValueError if it isn't a valid number."""
    if value is None:
        raise ValueError("empty amount")
    cleaned = str(value).replace(",", "").strip()
    if cleaned == "":
        raise ValueError("empty amount")
    return float(cleaned)


PAYMENT_METHODS = ["cash", "transfer", "pos"]


def compute_status_amount_due(booking):
    """The amount due to compare against net payments for payment_status
    purposes. Used by add_payment and edit_payment so payment status, the
    receipt, and total revenue all agree after a post-checkout correction."""
    is_checked_out = booking["status"] == "checked_out"
    return booking["actual_amount"] if is_checked_out and booking["actual_amount"] else booking["total_amount"]


def get_payments(conn, booking_id):
    return conn.execute(
        "SELECT * FROM payments WHERE booking_id = ? ORDER BY created_at",
        (booking_id,),
    ).fetchall()


def get_amount_paid(conn, booking_id):
    """Net amount the guest has paid so far (payments minus refunds)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN kind = 'refund' THEN -amount ELSE amount END), 0)
           FROM payments WHERE booking_id = ?""",
        (booking_id,),
    ).fetchone()
    return row[0]


def refresh_payment_status(conn, booking_id, amount_due):
    """Recompute and store pending/partial/paid based on payments recorded."""
    amount_paid = get_amount_paid(conn, booking_id)
    if amount_paid <= 0:
        status = "pending"
    elif amount_paid >= amount_due:
        status = "paid"
    else:
        status = "partial"
    conn.execute("UPDATE bookings SET payment_status = ? WHERE id = ?", (status, booking_id))
    return status, amount_paid


def record_payment_lines(conn, booking_id, form, method_field="payment_method[]", amount_field="payment_amount[]"):
    """Read repeated method[]/amount[] fields from a submitted form and
    insert one payment row per non-empty line. Returns the total recorded."""
    methods = form.getlist(method_field)
    amounts = form.getlist(amount_field)
    now = datetime.now().isoformat(timespec="seconds")
    total = 0.0
    for method, amount_str in zip(methods, amounts):
        amount_str = (amount_str or "").strip()
        if not amount_str:
            continue
        try:
            amount = parse_amount(amount_str)
        except ValueError:
            continue
        if amount <= 0 or method not in PAYMENT_METHODS:
            continue
        conn.execute(
            "INSERT INTO payments (booking_id, amount, method, kind, created_at) VALUES (?, ?, ?, 'payment', ?)",
            (booking_id, amount, method, now),
        )
        total += amount
    return total


# ---------------------------------------------------------------------------
# Guest store credit
#
# When a guest checks out with money owed back to them ("Refund Due to
# Guest") but the front-desk user chooses "No settlement right now"
# instead of paying it out, that amount becomes store credit on the
# guest's account, redeemable against a future booking.
# ---------------------------------------------------------------------------

def get_guest_credit_balance(conn, guest_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(remaining_amount), 0) FROM guest_credits WHERE guest_id = ?",
        (guest_id,),
    ).fetchone()
    return row[0]


def get_guest_credits(conn, guest_id):
    return conn.execute(
        "SELECT * FROM guest_credits WHERE guest_id = ? ORDER BY created_at DESC",
        (guest_id,),
    ).fetchall()


def get_guests_with_credit(conn, search=None):
    base = """
        SELECT c.*, COALESCE((SELECT SUM(remaining_amount) FROM guest_credits cc
                               WHERE cc.guest_id = c.id), 0) AS credit_balance
        FROM guests c
    """
    if search:
        return conn.execute(
            base + " WHERE c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ? ORDER BY c.name",
            (f"%{search}%", f"%{search}%", f"%{search}%"),
        ).fetchall()
    return conn.execute(base + " ORDER BY c.name").fetchall()


def issue_guest_credit(conn, guest_id, amount, source_booking_id=None, note=None):
    if amount <= 0:
        return
    conn.execute(
        """INSERT INTO guest_credits
           (guest_id, source_booking_id, amount, remaining_amount, created_at, note)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (guest_id, source_booking_id, amount, amount,
         datetime.now().isoformat(timespec="seconds"), note),
    )


def apply_guest_credit(conn, guest_id, amount):
    """Spend up to `amount` of the guest's available store credit, oldest
    grants first. Returns the amount actually applied."""
    if amount <= 0:
        return 0.0
    remaining_to_apply = amount
    applied_total = 0.0
    credits = conn.execute(
        """SELECT id, remaining_amount FROM guest_credits
           WHERE guest_id = ? AND remaining_amount > 0
           ORDER BY created_at""",
        (guest_id,),
    ).fetchall()
    for c in credits:
        if remaining_to_apply <= 0:
            break
        take = min(c["remaining_amount"], remaining_to_apply)
        conn.execute(
            "UPDATE guest_credits SET remaining_amount = remaining_amount - ? WHERE id = ?",
            (take, c["id"]),
        )
        remaining_to_apply -= take
        applied_total += take
    return applied_total


# ---------------------------------------------------------------------------
# Website sync
#
# Pushes the room list and current occupied date-ranges to the public
# website (public-website/app.py, hosted separately on Render) whenever
# this PC has internet, so a visitor can see a same-day availability
# calendar. This is a convenience signal, not a live database replica —
# see the Settings page and the README for the honest limitations (a few
# minutes of lag, and the website still can't take a real booking; a
# guest's final booking is always confirmed by staff via WhatsApp/phone).
# ---------------------------------------------------------------------------

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(conn, key, value):
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )


def build_sync_payload(conn):
    """Build the JSON payload describing the current room list and every
    date-range a room is NOT available for: every currently checked-in
    booking (room physically occupied through its expected check-out) plus
    every pending/confirmed reservation (room already spoken for, even
    though no guest has arrived yet). Dates are plain YYYY-MM-DD — the
    website only needs day-level granularity for its availability search,
    not exact check-in/out times."""
    rooms = conn.execute("SELECT room_number, floor, price_per_night FROM rooms ORDER BY floor, room_number").fetchall()

    blocks = []

    checked_in_bookings = conn.execute(
        """SELECT b.check_in, b.expected_check_out, r.room_number
           FROM bookings b JOIN rooms r ON r.id = b.room_id
           WHERE b.status = 'checked_in'"""
    ).fetchall()
    for b in checked_in_bookings:
        try:
            check_in_date = parse_flexible_datetime(b["check_in"]).date().isoformat()
            check_out_date = parse_flexible_datetime(b["expected_check_out"]).date().isoformat()
        except ValueError:
            continue
        blocks.append({"room_name": b["room_number"], "check_in": check_in_date, "check_out": check_out_date})

    active_reservations = conn.execute(
        """SELECT rv.check_in, rv.expected_check_out, r.room_number
           FROM reservations rv JOIN rooms r ON r.id = rv.room_id
           WHERE rv.status IN ('pending', 'confirmed')"""
    ).fetchall()
    for rv in active_reservations:
        try:
            check_in_date = parse_flexible_datetime(rv["check_in"]).date().isoformat()
            check_out_date = parse_flexible_datetime(rv["expected_check_out"]).date().isoformat()
        except ValueError:
            continue
        blocks.append({"room_name": rv["room_number"], "check_in": check_in_date, "check_out": check_out_date})

    return {
        "rooms": [
            {"name": r["room_number"], "floor": r["floor"], "price_per_night": r["price_per_night"]}
            for r in rooms
        ],
        "blocks": blocks,
    }


def sync_to_website(conn=None, timeout=10):
    """Attempt one sync push to the public website. Safe to call anytime —
    a hotel PC being offline is the NORMAL case here, not an error, so
    every failure is caught and recorded rather than raised. Returns True
    on a confirmed successful sync, False otherwise (including "not
    configured yet", which is not treated as an error state).

    `timeout` defaults to a short 10s for the background loop and the
    fire-and-forget post-booking syncs, so a slow/offline website never
    holds up a booking/checkout request. The "Sync now" button on the
    Settings page passes a much longer timeout instead, since an admin
    who clicks it is deliberately waiting for the result — this matters
    because a free-tier Render site that's been asleep can take up to
    ~50 seconds just to wake up and answer its first request."""
    close_after = conn is None
    conn = conn or get_db()
    try:
        url = (get_setting(conn, "website_sync_url") or "").strip().rstrip("/")
        api_key = (get_setting(conn, "website_sync_api_key") or "").strip()

        if not url or not api_key:
            set_setting(conn, "last_sync_status", "Not configured")
            set_setting(conn, "last_sync_at", datetime.now().isoformat(timespec="seconds"))
            conn.commit()
            return False

        payload = build_sync_payload(conn)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/sync/rooms",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "X-Sync-Key": api_key},
        )

        status = "error"
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if resp.status == 200 and body.get("status") == "ok":
                    status = "ok"
                else:
                    status = f"Website returned an unexpected response (HTTP {resp.status})"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                status = "Website rejected the request (check the API key)"
            elif e.code == 503:
                status = "Website hasn't configured its sync key yet"
            else:
                status = f"Website returned an error (HTTP {e.code})"
        except urllib.error.URLError:
            # Covers "no internet", DNS failure, connection refused, the
            # website being asleep with no response, etc. — the normal
            # case most of the time this PC isn't online.
            status = "No internet connection (will retry automatically)"
        except TimeoutError:
            status = "Website took too long to respond (will retry automatically)"
        except (ValueError, json.JSONDecodeError):
            status = "Website sent back something unexpected"

        set_setting(conn, "last_sync_status", status)
        set_setting(conn, "last_sync_at", datetime.now().isoformat(timespec="seconds"))
        conn.commit()
        return status == "ok"
    except Exception as e:  # noqa: BLE001 - a sync attempt must never crash the caller
        try:
            set_setting(conn, "last_sync_status", f"Unexpected error: {e}")
            set_setting(conn, "last_sync_at", datetime.now().isoformat(timespec="seconds"))
            conn.commit()
        except Exception:
            pass
        return False
    finally:
        if close_after:
            conn.close()


def trigger_async_sync():
    """Fire a sync attempt in the background without making the current
    request wait on a network call that might time out. Called right
    after any booking/reservation/room change that affects availability."""
    threading.Thread(target=sync_to_website, daemon=True).start()


def background_sync_loop():
    """Runs for the lifetime of the app: retries a sync every 5 minutes so
    that even without any booking activity, the website eventually catches
    up once this PC regains internet."""
    while True:
        time_module.sleep(300)
        sync_to_website()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.before_request
def require_login():
    allowed_endpoints = {"login", "static"}
    if request.endpoint in allowed_endpoints or request.endpoint is None:
        return None
    if "user_id" not in session:
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/")
def dashboard():
    conn = get_db()
    rooms = conn.execute("SELECT * FROM rooms ORDER BY room_number").fetchall()

    total_rooms = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    total_guests = conn.execute("SELECT COUNT(*) FROM guests").fetchone()[0]
    active_bookings = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE status = 'checked_in'"
    ).fetchone()[0]

    # Revenue reflects actual cash the hotel has collected: every payment
    # recorded counts immediately, and any cash refund paid back at checkout
    # brings it back down. Store-credit redemptions (method = 'credit') are
    # excluded so redeeming a credit on a later booking doesn't get counted
    # as new cash on top of the original payment that created that credit.
    # Visible to Admin and Manager only — Staff do not see revenue figures.
    total_revenue = 0.0
    today_revenue = 0.0
    if session.get("role") in ("admin", "manager"):
        total_revenue = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN kind = 'refund' THEN -amount ELSE amount END), 0)
               FROM payments WHERE method != 'credit'"""
        ).fetchone()[0]

        today_revenue = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN kind = 'refund' THEN -amount ELSE amount END), 0)
               FROM payments WHERE method != 'credit'
                 AND date(created_at) = date('now', 'localtime')"""
        ).fetchone()[0]

    # Pull guest + room info for currently occupied rooms.
    current_stays = conn.execute("""
        SELECT b.id, g.name AS guest_name, r.room_number, r.floor, b.check_in,
               b.expected_check_out
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE b.status = 'checked_in'
        ORDER BY b.check_in
    """).fetchall()

    conn.close()
    return render_template(
        "dashboard.html",
        rooms=rooms,
        total_guests=total_guests,
        active_bookings=active_bookings,
        total_revenue=total_revenue,
        today_revenue=today_revenue,
        available_rooms=total_rooms - active_bookings,
        current_stays=current_stays,
    )


# ---------------------------------------------------------------------------
# Guests
# ---------------------------------------------------------------------------

@app.route("/guests")
def guests():
    conn = get_db()
    search = request.args.get("q", "").strip()
    rows = get_guests_with_credit(conn, search=search or None)
    conn.close()
    return render_template("guests.html", guests=rows, search=search)


def _guest_registration_fields(form):
    """Pull the eight optional hotel-registration fields out of a submitted
    form, trimmed, with nationality defaulting to 'Nigerian' when left
    blank. Shared by add_guest and edit_guest so the two stay consistent."""
    nationality = (form.get("nationality") or "").strip() or "Nigerian"
    id_type = (form.get("id_type") or "").strip()
    if id_type not in ID_TYPES:
        id_type = ""
    return {
        "nationality": nationality,
        "id_type": id_type,
        "id_number": (form.get("id_number") or "").strip(),
        "occupation": (form.get("occupation") or "").strip(),
        "next_of_kin_name": (form.get("next_of_kin_name") or "").strip(),
        "next_of_kin_phone": (form.get("next_of_kin_phone") or "").strip(),
        "coming_from": (form.get("coming_from") or "").strip(),
        "going_to": (form.get("going_to") or "").strip(),
    }


@app.route("/guests/add", methods=["GET", "POST"])
def add_guest():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address = request.form.get("address", "").strip()
        reg = _guest_registration_fields(request.form)

        if not name or not phone:
            flash("Name and phone number are required.", "error")
            return render_template("add_guest.html", form=request.form, id_types=ID_TYPES)

        conn = get_db()
        conn.execute(
            """INSERT INTO guests
               (name, phone, email, address, nationality, id_type, id_number,
                occupation, next_of_kin_name, next_of_kin_phone,
                coming_from, going_to, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, phone, email, address,
                reg["nationality"], reg["id_type"], reg["id_number"],
                reg["occupation"], reg["next_of_kin_name"], reg["next_of_kin_phone"],
                reg["coming_from"], reg["going_to"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()
        flash(f"Guest '{name}' added successfully.", "success")
        return redirect(url_for("guests"))

    return render_template("add_guest.html", form={}, id_types=ID_TYPES)


@app.route("/guests/<int:guest_id>/edit", methods=["GET", "POST"])
def edit_guest(guest_id):
    """Correct or fill in a returning guest's details later — e.g. ID
    details weren't on hand at check-in — without creating a duplicate
    guest record. Open to every logged-in role, since Staff already
    handles guest registration day-to-day."""
    conn = get_db()
    guest = conn.execute("SELECT * FROM guests WHERE id = ?", (guest_id,)).fetchone()
    if guest is None:
        conn.close()
        flash("Guest not found.", "error")
        return redirect(url_for("guests"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address = request.form.get("address", "").strip()
        reg = _guest_registration_fields(request.form)

        if not name or not phone:
            conn.close()
            flash("Name and phone number are required.", "error")
            return render_template("edit_guest.html", guest=guest, form=request.form, id_types=ID_TYPES)

        conn.execute(
            """UPDATE guests
               SET name = ?, phone = ?, email = ?, address = ?, nationality = ?,
                   id_type = ?, id_number = ?, occupation = ?,
                   next_of_kin_name = ?, next_of_kin_phone = ?,
                   coming_from = ?, going_to = ?
               WHERE id = ?""",
            (
                name, phone, email, address,
                reg["nationality"], reg["id_type"], reg["id_number"],
                reg["occupation"], reg["next_of_kin_name"], reg["next_of_kin_phone"],
                reg["coming_from"], reg["going_to"],
                guest_id,
            ),
        )
        conn.commit()
        conn.close()
        flash(f"Guest '{name}' updated.", "success")
        return redirect(url_for("guest_detail", guest_id=guest_id))

    conn.close()
    return render_template("edit_guest.html", guest=guest, form=dict(guest), id_types=ID_TYPES)


@app.route("/guests/report")
@roles_required("admin", "manager")
def guests_report():
    period = (request.args.get("period") or "").strip()
    from_date = (request.args.get("from") or "").strip()
    to_date = (request.args.get("to") or "").strip()

    if period in ("today", "week", "month", "year"):
        today = date.today()
        to_date = today.isoformat()
        days_back = {"today": 0, "week": 7, "month": 30, "year": 365}[period]
        from_date = (today - __import__("datetime").timedelta(days=days_back)).isoformat()

    if not (from_date and to_date):
        today = date.today().isoformat()
        return render_template(
            "guests_report_setup.html",
            default_from=today,
            default_to=today,
        )

    conn = get_db()
    raw_rows = conn.execute("""
        SELECT b.*, g.name AS guest_name, g.phone AS guest_phone,
               g.email AS guest_email, g.address AS guest_address,
               r.room_number,
               COALESCE((SELECT SUM(CASE WHEN p.kind = 'refund' THEN -p.amount ELSE p.amount END)
                         FROM payments p WHERE p.booking_id = b.id), 0) AS amount_paid,
               (SELECT GROUP_CONCAT(DISTINCT p.method) FROM payments p
                WHERE p.booking_id = b.id AND p.kind = 'payment') AS payment_methods,
               COALESCE((SELECT SUM(p.amount) FROM payments p
                         WHERE p.booking_id = b.id AND p.kind = 'refund'), 0) AS refund_paid,
               (SELECT GROUP_CONCAT(DISTINCT p.method) FROM payments p
                WHERE p.booking_id = b.id AND p.kind = 'refund') AS refund_methods
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE substr(b.check_in, 1, 10) BETWEEN ? AND ?
        ORDER BY b.check_in
    """, (from_date, to_date)).fetchall()

    rows = []
    for r in raw_rows:
        b = dict(r)
        b["billed_amount"] = b["actual_amount"] if b["actual_amount"] is not None else b["total_amount"]
        b["payment_methods_display"] = (
            ", ".join(m.capitalize() for m in b["payment_methods"].split(",")) if b["payment_methods"] else "—"
        )
        b["refund_total"] = b["refund_paid"] or 0
        refund_method_parts = [m.capitalize() for m in b["refund_methods"].split(",")] if b["refund_methods"] else []
        b["refund_methods_display"] = ", ".join(refund_method_parts) if refund_method_parts else "—"
        rows.append(b)

    # Group bookings by guest so each guest's full record for the
    # period appears together.
    guests_map = {}
    order = []
    for b in rows:
        gid = b["guest_id"]
        if gid not in guests_map:
            guests_map[gid] = {
                "name": b["guest_name"],
                "phone": b["guest_phone"],
                "email": b["guest_email"],
                "address": b["guest_address"],
                "bookings": [],
                "total_spent": 0.0,
                "total_paid": 0.0,
                "total_refunded": 0.0,
            }
            order.append(gid)
        guests_map[gid]["bookings"].append(b)
        guests_map[gid]["total_spent"] += b["billed_amount"] or 0
        guests_map[gid]["total_paid"] += b["amount_paid"] or 0
        guests_map[gid]["total_refunded"] += b["refund_total"] or 0

    report_guests = [guests_map[gid] for gid in order]
    total_bookings = len(rows)
    total_revenue = sum(b["billed_amount"] or 0 for b in rows)
    total_collected = sum(b["amount_paid"] or 0 for b in rows)
    total_refunded = sum(b["refund_total"] or 0 for b in rows)

    conn.close()
    return render_template(
        "guests_report.html",
        from_date=from_date,
        to_date=to_date,
        period=period or None,
        report_guests=report_guests,
        total_guests=len(report_guests),
        total_bookings=total_bookings,
        total_revenue=total_revenue,
        total_collected=total_collected,
        total_refunded=total_refunded,
        generated_at=datetime.now().strftime("%d %b %Y, %I:%M %p"),
    )


@app.route("/reports/revenue")
@roles_required("admin", "manager")
def revenue_report():
    period = (request.args.get("period") or "month").strip()
    today = date.today()
    custom_from = (request.args.get("from") or "").strip()
    custom_to = (request.args.get("to") or "").strip()

    if period == "custom" and custom_from and custom_to:
        from_date, to_date = custom_from, custom_to
    else:
        period = period if period in ("today", "week", "month", "year") else "month"
        to_date = today.isoformat()
        days_back = {"today": 0, "week": 7, "month": 30, "year": 365}[period]
        from_date = (today - __import__("datetime").timedelta(days=days_back)).isoformat()

    conn = get_db()

    # Total revenue: net cash payments recorded in the date range.
    total_revenue = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN kind = 'refund' THEN -amount ELSE amount END), 0)
           FROM payments
           WHERE method != 'credit'
             AND date(created_at) BETWEEN ? AND ?""",
        (from_date, to_date),
    ).fetchone()[0]

    # Revenue broken down by staff member (whoever checked the guest in),
    # based on bookings whose check-in date falls in the selected range.
    by_staff = conn.execute(
        """SELECT COALESCE(checked_in_by, 'Unknown') AS staff_name,
                  COUNT(*) AS bookings_count,
                  SUM(COALESCE(actual_amount, total_amount)) AS revenue
           FROM bookings
           WHERE substr(check_in, 1, 10) BETWEEN ? AND ?
           GROUP BY COALESCE(checked_in_by, 'Unknown')
           ORDER BY revenue DESC""",
        (from_date, to_date),
    ).fetchall()

    # Revenue broken down by customer (reuses the same billed-amount logic
    # as the guests report, grouped here instead of per-booking).
    by_customer = conn.execute(
        """SELECT g.id, g.name, g.phone,
                  COUNT(b.id) AS bookings_count,
                  SUM(COALESCE(b.actual_amount, b.total_amount)) AS revenue
           FROM bookings b
           JOIN guests g ON g.id = b.guest_id
           WHERE substr(b.check_in, 1, 10) BETWEEN ? AND ?
           GROUP BY g.id, g.name, g.phone
           ORDER BY revenue DESC""",
        (from_date, to_date),
    ).fetchall()

    conn.close()
    return render_template(
        "reports_revenue.html",
        period=period,
        from_date=from_date,
        to_date=to_date,
        total_revenue=total_revenue,
        by_staff=by_staff,
        by_customer=by_customer,
        generated_at=datetime.now().strftime("%d %b %Y, %I:%M %p"),
    )


# ---------------------------------------------------------------------------
# Guest manifest (Admin/Manager only): a compliance-style register of every
# guest checked in during a chosen date range, with who checked them in/out
# and when — separate from the financial guests_report above, and
# downloadable as a real PDF rather than relying on a browser's print
# dialog.
# ---------------------------------------------------------------------------

def _manifest_bookings(conn, from_date, to_date):
    """Every booking whose check-in date falls within [from_date, to_date],
    joined to the guest's registration details and the room, oldest first."""
    return conn.execute(
        """SELECT b.*, g.name AS guest_name, g.phone AS guest_phone,
                  g.nationality, g.id_type, g.id_number,
                  g.coming_from, g.going_to,
                  r.room_number, r.floor
           FROM bookings b
           JOIN guests g ON g.id = b.guest_id
           JOIN rooms r ON r.id = b.room_id
           WHERE substr(b.check_in, 1, 10) BETWEEN ? AND ?
           ORDER BY b.check_in""",
        (from_date, to_date),
    ).fetchall()


@app.route("/reports/manifest")
@roles_required("admin", "manager")
def guest_manifest():
    from_date = (request.args.get("from") or "").strip()
    to_date = (request.args.get("to") or "").strip()

    if not (from_date and to_date):
        today = date.today().isoformat()
        return render_template(
            "manifest_setup.html",
            default_from=today,
            default_to=today,
        )

    conn = get_db()
    rows = _manifest_bookings(conn, from_date, to_date)
    conn.close()

    return render_template(
        "manifest.html",
        rows=rows,
        from_date=from_date,
        to_date=to_date,
        generated_at=datetime.now().strftime("%d %b %Y, %I:%M %p"),
    )


@app.route("/reports/manifest.pdf")
@roles_required("admin", "manager")
def guest_manifest_pdf():
    from_date = (request.args.get("from") or "").strip()
    to_date = (request.args.get("to") or "").strip()
    if not (from_date and to_date):
        flash("Choose a date range first.", "error")
        return redirect(url_for("guest_manifest"))

    conn = get_db()
    rows = _manifest_bookings(conn, from_date, to_date)
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"Mario Suites Ltd - Guest Manifest {from_date} to {to_date}",
    )

    gold = colors.HexColor("#A9802E")
    brown = colors.HexColor("#5C4420")
    # spaceAfter=12 (not the old 2) gives the title real breathing room above
    # the "Period:"/"Generated:" lines — at 2pt the two blocks sat almost
    # flush against each other, and the gap between the title's baseline
    # and the next line's ascenders could read as a stray underline beneath
    # the title. This is what was reported as "not properly aligned".
    title_style = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=16, textColor=brown, spaceAfter=12)
    meta_style = ParagraphStyle("Meta", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#6b5a3a"), spaceAfter=3)

    elements = [
        Paragraph(f"{HOTEL_NAME} &mdash; Guest Manifest", title_style),
        Paragraph(f"Period: {from_date} to {to_date}", meta_style),
        Paragraph(f"Generated: {datetime.now().strftime('%d %b %Y, %I:%M %p')}", meta_style),
        Spacer(1, 8 * mm),
    ]

    if not rows:
        elements.append(Paragraph("No guests checked in during this period.", meta_style))
    else:
        header = ["Guest", "Phone", "ID Type / No.", "Nationality", "Coming from", "Going to",
                   "Room", "Check-in", "Checked in by", "Check-out", "Checked out by"]
        cell_style = ParagraphStyle("Cell", fontName="Helvetica", fontSize=8, leading=10)
        # Header font is deliberately a touch smaller than the body text —
        # single-word headers like "Nationality" have no space to wrap at,
        # so without enough room they get hard-broken mid-word (e.g.
        # "Nationalit" / "y"), which is the real cause of the misaligned
        # look that was reported. A slightly smaller, still-bold header
        # font plus the rebalanced column widths below keep every header
        # on one or two clean lines instead.
        header_style = ParagraphStyle("Head", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=colors.white)

        table_data = [[Paragraph(h, header_style) for h in header]]
        for b in rows:
            id_display = " / ".join(x for x in [b["id_type"], b["id_number"]] if x) or "—"
            check_out_display = format_dt_display(b["actual_check_out"] or "") if b["status"] == "checked_out" and b["actual_check_out"] else (
                "Still checked in" if b["status"] != "checked_out" else "—"
            )
            table_data.append([
                Paragraph(b["guest_name"] or "—", cell_style),
                Paragraph(b["guest_phone"] or "—", cell_style),
                Paragraph(id_display, cell_style),
                Paragraph(b["nationality"] or "—", cell_style),
                Paragraph(b["coming_from"] or "—", cell_style),
                Paragraph(b["going_to"] or "—", cell_style),
                Paragraph(f"{b['room_number']} ({b['floor']})", cell_style),
                Paragraph(format_dt_display(b["check_in"]), cell_style),
                Paragraph(b["checked_in_by"] or "—", cell_style),
                Paragraph(check_out_display, cell_style),
                Paragraph(b["checked_out_by"] or "—", cell_style),
            ])

        # Widths sum to 242mm, comfortably inside the ~269mm usable width
        # on a landscape A4 page with 14mm margins, so the table never
        # runs into or past the page edge regardless of font-rendering
        # variance between systems.
        col_widths = [30*mm, 18*mm, 24*mm, 22*mm, 18*mm, 14*mm,
                      20*mm, 24*mm, 22*mm, 24*mm, 26*mm]
        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), gold),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E4D5B7")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FBF3E3")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(table)

    doc.build(elements)
    buffer.seek(0)
    filename = f"guest_manifest_{from_date}_to_{to_date}.pdf"
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.route("/guests/<int:guest_id>")
def guest_detail(guest_id):
    conn = get_db()
    guest = conn.execute(
        "SELECT * FROM guests WHERE id = ?", (guest_id,)
    ).fetchone()
    bookings = conn.execute("""
        SELECT b.*, r.room_number, r.floor
        FROM bookings b
        JOIN rooms r ON r.id = b.room_id
        WHERE b.guest_id = ?
        ORDER BY b.check_in DESC
    """, (guest_id,)).fetchall()
    credit_balance = get_guest_credit_balance(conn, guest_id)
    credits = get_guest_credits(conn, guest_id)
    conn.close()

    if guest is None:
        flash("Guest not found.", "error")
        return redirect(url_for("guests"))

    return render_template(
        "guest_detail.html",
        guest=guest,
        bookings=bookings,
        credit_balance=credit_balance,
        credits=credits,
    )


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

@app.route("/bookings")
def bookings():
    conn = get_db()
    rows = conn.execute("""
        SELECT b.*, g.name AS guest_name, g.phone AS guest_phone,
               r.room_number, r.floor, r.price_per_night,
               COALESCE((SELECT SUM(CASE WHEN p.kind = 'refund' THEN -p.amount ELSE p.amount END)
                         FROM payments p WHERE p.booking_id = b.id), 0) AS amount_paid
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        ORDER BY b.status ASC, b.check_in DESC
    """).fetchall()
    conn.close()
    return render_template("bookings.html", bookings=rows)


@app.route("/bookings/new", methods=["GET", "POST"])
def new_booking():
    conn = get_db()

    if request.method == "POST":
        guest_id = request.form.get("guest_id")
        room_id = request.form.get("room_id")
        check_in_date = request.form.get("check_in")
        check_in_time = (request.form.get("check_in_time") or "").strip()
        expected_check_out_date = request.form.get("expected_check_out")
        expected_check_out_time = (request.form.get("expected_check_out_time") or "").strip()

        check_in = f"{check_in_date}T{check_in_time}" if check_in_date and check_in_time else check_in_date
        expected_check_out = (
            f"{expected_check_out_date}T{expected_check_out_time}"
            if expected_check_out_date and expected_check_out_time
            else expected_check_out_date
        )

        error = None
        nights = 0
        if not (guest_id and room_id and check_in_date and expected_check_out_date):
            error = "Please fill in every field."
        else:
            try:
                nights = calculate_nights(check_in, expected_check_out)
            except ValueError:
                error = "Dates are not valid."
            if nights is not None and nights <= 0:
                error = "Check-out date must be after the check-in date."

        if error:
            flash(error, "error")
            guests_list = get_guests_with_credit(conn)
            available_rooms = conn.execute(
                "SELECT * FROM rooms WHERE status = 'available' ORDER BY room_number"
            ).fetchall()
            conn.close()
            return render_template(
                "new_booking.html",
                guests=guests_list,
                rooms=available_rooms,
                form=request.form,
            )

        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        total_amount = nights * room["price_per_night"]

        cur = conn.execute(
            """INSERT INTO bookings
               (guest_id, room_id, check_in, expected_check_out, nights,
                total_amount, status, created_at, checked_in_by)
               VALUES (?, ?, ?, ?, ?, ?, 'checked_in', ?, ?)""",
            (
                guest_id,
                room_id,
                check_in,
                expected_check_out,
                nights,
                total_amount,
                datetime.now().isoformat(timespec="seconds"),
                session.get("username"),
            ),
        )
        booking_id = cur.lastrowid
        conn.execute("UPDATE rooms SET status = 'occupied' WHERE id = ?", (room_id,))

        # Redeem any store credit the guest has on their account first.
        credit_to_apply_str = (request.form.get("credit_to_apply") or "").strip()
        try:
            credit_to_apply = parse_amount(credit_to_apply_str) if credit_to_apply_str else 0.0
        except ValueError:
            credit_to_apply = 0.0
        available_credit = get_guest_credit_balance(conn, guest_id)
        credit_to_apply = max(0.0, min(credit_to_apply, available_credit, total_amount))

        applied_credit = 0.0
        if credit_to_apply > 0:
            applied_credit = apply_guest_credit(conn, guest_id, credit_to_apply)
            if applied_credit > 0:
                conn.execute(
                    """INSERT INTO payments (booking_id, amount, method, kind, note, created_at)
                       VALUES (?, ?, 'credit', 'payment', ?, ?)""",
                    (booking_id, applied_credit, "Store credit applied at booking",
                     datetime.now().isoformat(timespec="seconds")),
                )
                conn.execute("UPDATE bookings SET credit_applied = ? WHERE id = ?", (applied_credit, booking_id))

        amount_recorded = record_payment_lines(conn, booking_id, request.form)
        status, _ = refresh_payment_status(conn, booking_id, total_amount)

        conn.commit()
        conn.close()
        trigger_async_sync()  # room just went from available to occupied

        credit_note = f" ₦{applied_credit:,.0f} store credit applied." if applied_credit > 0 else ""
        if status == "paid":
            flash("Booking created. Payment received in full." + credit_note, "success")
        elif status == "partial":
            flash(f"Booking created. Deposit of ₦{amount_recorded:,.0f} recorded; balance still due.{credit_note}", "success")
        else:
            flash("Booking created and room marked as occupied. Payment is pending." + credit_note, "success")
        return redirect(url_for("bookings"))

    guests_list = get_guests_with_credit(conn)
    available_rooms = conn.execute(
        "SELECT * FROM rooms WHERE status = 'available' ORDER BY room_number"
    ).fetchall()
    conn.close()

    if not guests_list:
        flash("Add a guest first before creating a booking.", "error")
        return redirect(url_for("add_guest"))

    now = datetime.now()
    preselect_guest_id = request.args.get("guest_id", "")
    return render_template(
        "new_booking.html",
        guests=guests_list,
        rooms=available_rooms,
        form={"guest_id": preselect_guest_id} if preselect_guest_id else {},
        today=now.date().isoformat(),
        now_time=now.strftime("%H:%M"),
    )


@app.route("/bookings/<int:booking_id>/extend", methods=["GET", "POST"])
def extend_booking(booking_id):
    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, g.name AS guest_name, r.room_number, r.floor, r.price_per_night
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE b.id = ?
    """, (booking_id,)).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("bookings"))

    if booking["status"] != "checked_in":
        conn.close()
        flash("Only guests who are currently checked in can have their stay extended.", "error")
        return redirect(url_for("bookings"))

    amount_paid_so_far = get_amount_paid(conn, booking_id)

    if request.method == "POST":
        new_date = request.form.get("new_check_out")
        new_time = (request.form.get("new_check_out_time") or "12:00").strip()
        if not new_date:
            conn.close()
            flash("Please choose a new check-out date.", "error")
            return redirect(url_for("extend_booking", booking_id=booking_id))

        new_expected_check_out = f"{new_date}T{new_time}"

        try:
            new_nights = calculate_nights(booking["check_in"], new_expected_check_out)
        except ValueError:
            conn.close()
            flash("That date is not valid.", "error")
            return redirect(url_for("extend_booking", booking_id=booking_id))

        if new_nights <= booking["nights"]:
            conn.close()
            flash("The new check-out must be later than the current expected check-out to extend the stay.", "error")
            return redirect(url_for("extend_booking", booking_id=booking_id))

        added_nights = new_nights - booking["nights"]
        new_total = new_nights * booking["price_per_night"]
        added_amount = new_total - booking["total_amount"]

        conn.execute(
            "UPDATE bookings SET expected_check_out = ?, nights = ?, total_amount = ? WHERE id = ?",
            (new_expected_check_out, new_nights, new_total, booking_id),
        )
        refresh_payment_status(conn, booking_id, new_total)
        conn.commit()
        conn.close()
        trigger_async_sync()  # expected check-out date changed

        flash(
            f"Stay extended by {added_nights} night(s). New total: ₦{new_total:,.0f} "
            f"(₦{added_amount:,.0f} added).",
            "success",
        )
        return redirect(url_for("booking_receipt", booking_id=booking_id))

    conn.close()
    return render_template(
        "extend_booking.html",
        booking=booking,
        amount_paid_so_far=amount_paid_so_far,
    )


@app.route("/bookings/<int:booking_id>/receipt")
def booking_receipt(booking_id):
    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, g.name AS guest_name, g.phone AS guest_phone,
               g.email AS guest_email, g.address AS guest_address,
               r.room_number, r.floor, r.price_per_night
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE b.id = ?
    """, (booking_id,)).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("bookings"))

    payments = get_payments(conn, booking_id)
    amount_paid = get_amount_paid(conn, booking_id)

    payments_by_method = {m: 0.0 for m in PAYMENT_METHODS + ["credit"]}
    for p in payments:
        if p["method"] in payments_by_method:
            payments_by_method[p["method"]] += -p["amount"] if p["kind"] == "refund" else p["amount"]

    is_checked_out = booking["status"] == "checked_out"
    billed_nights = booking["actual_nights"] if is_checked_out and booking["actual_nights"] else booking["nights"]
    billed_amount = booking["actual_amount"] if is_checked_out and booking["actual_amount"] else booking["total_amount"]

    if amount_paid >= billed_amount:
        balance_due = 0.0
        refund_amount = max(0.0, amount_paid - billed_amount)
    else:
        balance_due = max(0.0, billed_amount - amount_paid)
        refund_amount = 0.0

    conn.close()

    return render_template(
        "receipt.html",
        booking=booking,
        payments=payments,
        payments_by_method=payments_by_method,
        amount_paid=amount_paid,
        billed_nights=billed_nights,
        billed_amount=billed_amount,
        refund_amount=refund_amount,
        balance_due=balance_due,
        is_checked_out=is_checked_out,
        receipt_number=f"MSL-{booking_id:06d}",
        generated_at=datetime.now().strftime("%d %b %Y, %I:%M %p"),
    )


@app.route("/bookings/<int:booking_id>/edit-payment", methods=["GET", "POST"])
@admin_required
def edit_payment(booking_id):
    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, g.name AS guest_name, r.room_number, r.floor, r.price_per_night
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE b.id = ?
    """, (booking_id,)).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("bookings"))

    amount_due = compute_status_amount_due(booking)

    if request.method == "POST":
        payment_ids = request.form.getlist("payment_id[]")
        methods = request.form.getlist("payment_method[]")
        amounts = request.form.getlist("payment_amount[]")
        kinds = request.form.getlist("payment_kind[]")
        delete_ids = set(request.form.getlist("delete_payment_id[]"))

        for pid, method, amount_str, kind in zip(payment_ids, methods, amounts, kinds):
            if pid in delete_ids:
                conn.execute("DELETE FROM payments WHERE id = ? AND booking_id = ?", (pid, booking_id))
                continue
            amount_str = (amount_str or "").strip()
            try:
                amount = parse_amount(amount_str)
            except ValueError:
                amount = 0
            if amount <= 0 or method not in PAYMENT_METHODS + ["credit"] or kind not in ("payment", "refund"):
                flash(f"Skipped an invalid edit on payment #{pid}.", "error")
                continue
            conn.execute(
                "UPDATE payments SET amount = ?, method = ?, kind = ? WHERE id = ? AND booking_id = ?",
                (amount, method, kind, pid, booking_id),
            )

        new_amount_recorded = record_payment_lines(
            conn, booking_id, request.form,
            method_field="new_payment_method[]", amount_field="new_payment_amount[]",
        )

        status, _ = refresh_payment_status(conn, booking_id, amount_due)
        conn.commit()
        conn.close()
        if new_amount_recorded > 0:
            flash(
                f"Payment records updated and ₦{new_amount_recorded:,.0f} new payment added. "
                f"Payment status: {status}.",
                "success",
            )
        else:
            flash(f"Payment records updated. Payment status: {status}.", "success")
        return redirect(url_for("booking_receipt", booking_id=booking_id))

    payments = get_payments(conn, booking_id)
    amount_paid_so_far = get_amount_paid(conn, booking_id)
    conn.close()
    return render_template(
        "edit_payment.html",
        booking=booking,
        payments=payments,
        amount_due=amount_due,
        amount_paid_so_far=amount_paid_so_far,
    )


@app.route("/bookings/<int:booking_id>/add-payment", methods=["GET", "POST"])
def add_payment(booking_id):
    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, g.name AS guest_name, r.room_number, r.floor, r.price_per_night
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE b.id = ?
    """, (booking_id,)).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("bookings"))

    # Only admins may add a payment to an already checked-out booking.
    if booking["status"] == "checked_out" and session.get("role") != "admin":
        conn.close()
        flash("Adding payment to a checked-out booking is restricted to the Admin profile.", "error")
        return redirect(url_for("bookings"))

    amount_paid_so_far = get_amount_paid(conn, booking_id)
    amount_due = compute_status_amount_due(booking)

    if request.method == "POST":
        amount_recorded = record_payment_lines(conn, booking_id, request.form)
        if amount_recorded <= 0:
            conn.close()
            flash("Enter at least one payment amount before saving.", "error")
            return redirect(url_for("add_payment", booking_id=booking_id))

        status, _ = refresh_payment_status(conn, booking_id, amount_due)
        conn.commit()
        conn.close()
        flash(f"Payment of ₦{amount_recorded:,.0f} recorded. Payment status: {status}.", "success")
        return redirect(url_for("booking_receipt", booking_id=booking_id))

    conn.close()
    balance_remaining = max(0.0, amount_due - amount_paid_so_far)
    return render_template(
        "add_payment.html",
        booking=booking,
        amount_paid_so_far=amount_paid_so_far,
        amount_due=amount_due,
        balance_remaining=balance_remaining,
    )


@app.route("/bookings/<int:booking_id>/checkout", methods=["GET", "POST"])
def checkout_booking(booking_id):
    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, g.name AS guest_name, r.room_number, r.floor, r.price_per_night
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE b.id = ?
    """, (booking_id,)).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("bookings"))

    if booking["status"] == "checked_out":
        conn.close()
        flash("This guest has already been checked out.", "error")
        return redirect(url_for("bookings"))

    amount_paid_so_far = get_amount_paid(conn, booking_id)

    if request.method == "POST":
        checkout_date_str = request.form.get("checkout_date") or date.today().isoformat()
        checkout_time_str = request.form.get("checkout_time") or datetime.now().strftime("%H:%M")
        try:
            checkout_dt = datetime.strptime(f"{checkout_date_str} {checkout_time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            checkout_dt = datetime.now()

        actual_nights = calculate_actual_nights(booking["check_in"], checkout_dt)
        actual_amount = actual_nights * booking["price_per_night"]

        # Record any extra settlement payment/refund made at checkout time.
        settle_kind = request.form.get("settle_kind")  # 'refund' or 'extra_payment' or '' (none)
        settle_method = request.form.get("settle_method")
        settle_amount_str = (request.form.get("settle_amount") or "").strip()
        credit_issued = 0.0

        if settle_kind in ("refund", "extra_payment") and settle_method in PAYMENT_METHODS and settle_amount_str:
            try:
                settle_amount = parse_amount(settle_amount_str)
            except ValueError:
                settle_amount = 0
            if settle_amount > 0:
                kind = "refund" if settle_kind == "refund" else "payment"
                conn.execute(
                    "INSERT INTO payments (booking_id, amount, method, kind, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (booking_id, settle_amount, settle_method,
                     kind, "Settled at checkout", datetime.now().isoformat(timespec="seconds")),
                )
        elif settle_kind == "" or not settle_kind:
            # "No settlement right now": if a refund is owed, convert it to
            # store credit instead of losing track of it.
            amount_paid_now = get_amount_paid(conn, booking_id)
            owed = amount_paid_now - actual_amount
            if owed > 0:
                issue_guest_credit(
                    conn, booking["guest_id"], owed, booking_id,
                    note=f"Unsettled refund from booking #{booking_id} at checkout",
                )
                credit_issued = owed

        # Recompute refund/balance after all payments.
        amount_paid = get_amount_paid(conn, booking_id)
        refund_amount = max(0.0, amount_paid - actual_amount) if credit_issued == 0 else 0.0
        balance_due = max(0.0, actual_amount - amount_paid)

        conn.execute(
            """UPDATE bookings
               SET status = 'checked_out',
                   actual_check_out = ?,
                   checkout_time = ?,
                   actual_nights = ?,
                   actual_amount = ?,
                   refund_amount = ?,
                   balance_due = ?,
                   credit_issued = ?,
                   checked_out_by = ?
               WHERE id = ?""",
            (
                checkout_dt.date().isoformat(),
                checkout_dt.isoformat(timespec="minutes"),
                actual_nights,
                actual_amount,
                refund_amount,
                balance_due,
                credit_issued,
                session.get("username"),
                booking_id,
            ),
        )
        refresh_payment_status(conn, booking_id, actual_amount)
        conn.execute("UPDATE rooms SET status = 'available' WHERE id = ?", (booking["room_id"],))
        conn.commit()
        conn.close()
        trigger_async_sync()  # room just became available again

        if refund_amount > 0:
            flash(f"Guest checked out. Refund due: ₦{refund_amount:,.0f}.", "success")
        elif credit_issued > 0:
            flash(f"Guest checked out. ₦{credit_issued:,.0f} issued as store credit.", "success")
        elif balance_due > 0:
            flash(f"Guest checked out. Balance still owed: ₦{balance_due:,.0f}.", "success")
        else:
            flash("Guest checked out. Room is now available.", "success")
        return redirect(url_for("booking_receipt", booking_id=booking_id))

    now = datetime.now()
    conn.close()
    return render_template(
        "checkout.html",
        booking=booking,
        amount_paid_so_far=amount_paid_so_far,
        default_date=now.date().isoformat(),
        default_time=now.strftime("%H:%M"),
    )


# ---------------------------------------------------------------------------
# Cancel booking (admin-only: cancels a checked-in booking, frees the room,
# and refunds any payments the guest has already made)
# ---------------------------------------------------------------------------

@app.route("/bookings/<int:booking_id>/cancel", methods=["GET", "POST"])
@admin_required
def cancel_booking(booking_id):
    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, g.name AS guest_name, r.room_number, r.floor, r.price_per_night
        FROM bookings b
        JOIN guests g ON g.id = b.guest_id
        JOIN rooms r ON r.id = b.room_id
        WHERE b.id = ?
    """, (booking_id,)).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("bookings"))

    if booking["status"] != "checked_in":
        conn.close()
        flash("Only bookings with a checked-in guest can be cancelled.", "error")
        return redirect(url_for("bookings"))

    amount_paid_so_far = get_amount_paid(conn, booking_id)

    if request.method == "POST":
        cancel_reason = (request.form.get("cancel_reason") or "").strip()
        now = datetime.now()

        if amount_paid_so_far > 0:
            conn.execute(
                """INSERT INTO payments (booking_id, amount, method, kind, note, created_at)
                   VALUES (?, ?, 'credit', 'refund', ?, ?)""",
                (
                    booking_id,
                    amount_paid_so_far,
                    f"Refund on cancellation by {session.get('username')}"
                    + (f": {cancel_reason}" if cancel_reason else ""),
                    now.isoformat(timespec="seconds"),
                ),
            )
            issue_guest_credit(
                conn,
                booking["guest_id"],
                amount_paid_so_far,
                booking_id,
                note=f"Refund from cancelled booking #{booking_id}"
                + (f": {cancel_reason}" if cancel_reason else ""),
            )

        conn.execute(
            """UPDATE bookings
               SET status = 'checked_out',
                   actual_check_out = ?,
                   checkout_time = ?,
                   actual_nights = 0,
                   actual_amount = 0,
                   refund_amount = ?,
                   balance_due = 0,
                   credit_issued = ?,
                   checked_out_by = ?
               WHERE id = ?""",
            (
                now.date().isoformat(),
                now.isoformat(timespec="minutes"),
                amount_paid_so_far,
                amount_paid_so_far,
                session.get("username"),
                booking_id,
            ),
        )
        if amount_paid_so_far > 0:
            conn.execute("UPDATE bookings SET payment_status = 'paid' WHERE id = ?", (booking_id,))
        else:
            conn.execute("UPDATE bookings SET payment_status = 'pending' WHERE id = ?", (booking_id,))

        conn.execute("UPDATE rooms SET status = 'available' WHERE id = ?", (booking["room_id"],))
        conn.commit()
        conn.close()
        trigger_async_sync()  # room just became available again

        if amount_paid_so_far > 0:
            flash(
                f"Booking for {booking['guest_name']} cancelled. "
                f"₦{amount_paid_so_far:,.0f} refunded as store credit.",
                "success",
            )
        else:
            flash(f"Booking for {booking['guest_name']} cancelled. Room is now available.", "success")
        return redirect(url_for("bookings"))

    conn.close()
    return render_template(
        "cancel_booking.html",
        booking=booking,
        amount_paid_so_far=amount_paid_so_far,
    )


# ---------------------------------------------------------------------------
# Reservations (booking a room ahead of arrival, for a guest who isn't
# checking in yet). No money is collected at reservation time — payment
# happens at check-in / booking, same as a walk-in.
# ---------------------------------------------------------------------------

def get_reservation_or_404(conn, reservation_id):
    return conn.execute(
        """SELECT rv.*, g.name AS guest_name, g.phone AS guest_phone,
                  g.email AS guest_email, g.address AS guest_address,
                  r.room_number, r.floor, r.price_per_night
           FROM reservations rv
           JOIN guests g ON g.id = rv.guest_id
           JOIN rooms r ON r.id = rv.room_id
           WHERE rv.id = ?""",
        (reservation_id,),
    ).fetchone()


@app.route("/reservations")
def reservations_page():
    conn = get_db()
    status_filter = request.args.get("status", "").strip()
    query = """
        SELECT rv.*, g.name AS guest_name, g.phone AS guest_phone,
               r.room_number, r.floor
        FROM reservations rv
        JOIN guests g ON g.id = rv.guest_id
        JOIN rooms r ON r.id = rv.room_id
    """
    params = ()
    if status_filter in ("pending", "confirmed", "converted", "cancelled"):
        query += " WHERE rv.status = ?"
        params = (status_filter,)
    query += " ORDER BY rv.status ASC, rv.check_in ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template(
        "reservations.html",
        reservations=rows,
        status_filter=status_filter or None,
    )


@app.route("/reservations/new", methods=["GET", "POST"])
def new_reservation():
    conn = get_db()

    if request.method == "POST":
        guest_id = request.form.get("guest_id")
        room_id = request.form.get("room_id")
        check_in = request.form.get("check_in")
        expected_check_out = request.form.get("expected_check_out")
        notes = (request.form.get("notes") or "").strip()

        error = None
        nights = 0
        if not (guest_id and room_id and check_in and expected_check_out):
            error = "Please fill in every required field."
        else:
            try:
                nights = calculate_nights(check_in, expected_check_out)
            except ValueError:
                error = "Dates are not valid."
            if not error and nights <= 0:
                error = "Check-out date must be after the check-in date."

        if error:
            flash(error, "error")
            guests_list = get_guests_with_credit(conn)
            all_rooms = conn.execute("SELECT * FROM rooms ORDER BY room_number").fetchall()
            conn.close()
            return render_template(
                "new_reservation.html",
                guests=guests_list,
                rooms=all_rooms,
                form=request.form,
                today=date.today().isoformat(),
            )

        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        total_amount = nights * room["price_per_night"]

        cur = conn.execute(
            """INSERT INTO reservations
               (guest_id, room_id, check_in, expected_check_out, nights,
                total_amount, notes, status, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                guest_id,
                room_id,
                check_in,
                expected_check_out,
                nights,
                total_amount,
                notes or None,
                session.get("username"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        reservation_id = cur.lastrowid
        conn.commit()
        conn.close()
        trigger_async_sync()  # room just got reserved for these dates

        flash(f"Reservation created for {room['room_number']}.", "success")
        return redirect(url_for("reservation_detail", reservation_id=reservation_id))

    guests_list = get_guests_with_credit(conn)
    all_rooms = conn.execute("SELECT * FROM rooms ORDER BY room_number").fetchall()
    conn.close()

    if not guests_list:
        flash("Add a guest first before creating a reservation.", "error")
        return redirect(url_for("add_guest"))

    return render_template(
        "new_reservation.html",
        guests=guests_list,
        rooms=all_rooms,
        form={},
        today=date.today().isoformat(),
    )


@app.route("/reservations/<int:reservation_id>")
def reservation_detail(reservation_id):
    conn = get_db()
    rv = get_reservation_or_404(conn, reservation_id)
    conn.close()
    if rv is None:
        flash("Reservation not found.", "error")
        return redirect(url_for("reservations_page"))
    return render_template(
        "reservation_detail.html",
        rv=rv,
        generated_at=datetime.now().strftime("%d %b %Y, %I:%M %p"),
    )


@app.route("/reservations/<int:reservation_id>/confirm", methods=["POST"])
def confirm_reservation(reservation_id):
    conn = get_db()
    rv = get_reservation_or_404(conn, reservation_id)
    if rv is None:
        conn.close()
        flash("Reservation not found.", "error")
        return redirect(url_for("reservations_page"))
    if rv["status"] != "pending":
        conn.close()
        flash("Only a pending reservation can be confirmed.", "error")
        return redirect(url_for("reservation_detail", reservation_id=reservation_id))

    conn.execute("UPDATE reservations SET status = 'confirmed' WHERE id = ?", (reservation_id,))
    conn.commit()
    conn.close()
    flash("Reservation confirmed.", "success")
    return redirect(url_for("reservation_detail", reservation_id=reservation_id))


@app.route("/reservations/<int:reservation_id>/cancel", methods=["POST"])
def cancel_reservation(reservation_id):
    conn = get_db()
    rv = get_reservation_or_404(conn, reservation_id)
    if rv is None:
        conn.close()
        flash("Reservation not found.", "error")
        return redirect(url_for("reservations_page"))
    if rv["status"] not in ("pending", "confirmed"):
        conn.close()
        flash("This reservation can no longer be cancelled.", "error")
        return redirect(url_for("reservation_detail", reservation_id=reservation_id))

    conn.execute("UPDATE reservations SET status = 'cancelled' WHERE id = ?", (reservation_id,))
    conn.commit()
    conn.close()
    trigger_async_sync()  # those dates just freed up
    flash("Reservation cancelled.", "success")
    return redirect(url_for("reservations_page"))


@app.route("/reservations/<int:reservation_id>/checkin", methods=["GET", "POST"])
def checkin_from_reservation(reservation_id):
    conn = get_db()
    rv = get_reservation_or_404(conn, reservation_id)
    if rv is None:
        conn.close()
        flash("Reservation not found.", "error")
        return redirect(url_for("reservations_page"))
    if rv["status"] not in ("pending", "confirmed"):
        conn.close()
        flash("This reservation has already been checked in or cancelled.", "error")
        return redirect(url_for("reservation_detail", reservation_id=reservation_id))

    if request.method == "POST":
        check_in_date = request.form.get("check_in_date")
        check_in_time = (request.form.get("check_in_time") or "").strip()
        check_in = f"{check_in_date}T{check_in_time}" if check_in_date and check_in_time else check_in_date

        if not check_in_date:
            flash("Please provide the actual check-in date.", "error")
            conn.close()
            return redirect(url_for("checkin_from_reservation", reservation_id=reservation_id))

        expected_check_out = rv["expected_check_out"]
        try:
            nights = calculate_nights(check_in, expected_check_out)
        except ValueError:
            nights = rv["nights"]
        if nights <= 0:
            nights = rv["nights"]
        total_amount = nights * rv["price_per_night"]

        cur = conn.execute(
            """INSERT INTO bookings
               (guest_id, room_id, check_in, expected_check_out, nights,
                total_amount, status, created_at, checked_in_by)
               VALUES (?, ?, ?, ?, ?, ?, 'checked_in', ?, ?)""",
            (
                rv["guest_id"],
                rv["room_id"],
                check_in,
                expected_check_out,
                nights,
                total_amount,
                datetime.now().isoformat(timespec="seconds"),
                session.get("username"),
            ),
        )
        booking_id = cur.lastrowid
        conn.execute("UPDATE rooms SET status = 'occupied' WHERE id = ?", (rv["room_id"],))

        amount_recorded = record_payment_lines(conn, booking_id, request.form)
        status, _ = refresh_payment_status(conn, booking_id, total_amount)

        conn.execute(
            "UPDATE reservations SET status = 'converted', converted_booking_id = ? WHERE id = ?",
            (booking_id, reservation_id),
        )

        conn.commit()
        conn.close()
        trigger_async_sync()  # reservation converted into an active booking

        if status == "paid":
            flash("Guest checked in. Payment received in full.", "success")
        elif status == "partial":
            flash(f"Guest checked in. ₦{amount_recorded:,.0f} recorded; balance still due.", "success")
        else:
            flash("Guest checked in. Room marked as occupied. Payment is pending.", "success")
        return redirect(url_for("booking_receipt", booking_id=booking_id))

    conn.close()
    now = datetime.now()
    return render_template(
        "checkin_from_reservation.html",
        rv=rv,
        today=now.date().isoformat(),
        now_time=now.strftime("%H:%M"),
    )


# ---------------------------------------------------------------------------
# Rooms (add/rename/edit rooms, floors and prices — Admin and Manager only)
# ---------------------------------------------------------------------------

@app.route("/rooms")
def rooms():
    conn = get_db()
    rows = conn.execute("SELECT * FROM rooms ORDER BY floor, room_number").fetchall()
    conn.close()
    return render_template("rooms.html", rooms=rows)


@app.route("/rooms/add", methods=["POST"])
@roles_required("admin", "manager")
def add_room():
    room_number = (request.form.get("room_number") or "").strip()
    floor = (request.form.get("floor") or "").strip() or "Ground Floor"
    price_raw = request.form.get("price_per_night")

    if not room_number:
        flash("Room number/name is required.", "error")
        return redirect(url_for("rooms"))

    try:
        price = parse_amount(price_raw)
    except ValueError:
        flash("Please enter a valid price.", "error")
        return redirect(url_for("rooms"))

    conn = get_db()
    existing = conn.execute("SELECT id FROM rooms WHERE room_number = ?", (room_number,)).fetchone()
    if existing:
        conn.close()
        flash(f"Room '{room_number}' already exists.", "error")
        return redirect(url_for("rooms"))

    conn.execute(
        "INSERT INTO rooms (room_number, floor, price_per_night, status) VALUES (?, ?, ?, 'available')",
        (room_number, floor, price),
    )
    conn.commit()
    conn.close()
    trigger_async_sync()
    flash(f"Room '{room_number}' added.", "success")
    return redirect(url_for("rooms"))


@app.route("/rooms/<int:room_id>/edit", methods=["POST"])
@roles_required("admin", "manager")
def edit_room(room_id):
    room_number = (request.form.get("room_number") or "").strip()
    floor = (request.form.get("floor") or "").strip() or "Ground Floor"
    price_raw = request.form.get("price_per_night")

    conn = get_db()
    room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        flash("Room not found.", "error")
        return redirect(url_for("rooms"))

    if not room_number:
        conn.close()
        flash("Room number/name is required.", "error")
        return redirect(url_for("rooms"))

    try:
        price = parse_amount(price_raw)
    except ValueError:
        conn.close()
        flash("Please enter a valid price.", "error")
        return redirect(url_for("rooms"))

    duplicate = conn.execute(
        "SELECT id FROM rooms WHERE room_number = ? AND id != ?", (room_number, room_id)
    ).fetchone()
    if duplicate:
        conn.close()
        flash(f"Another room is already named '{room_number}'.", "error")
        return redirect(url_for("rooms"))

    conn.execute(
        "UPDATE rooms SET room_number = ?, floor = ?, price_per_night = ? WHERE id = ?",
        (room_number, floor, price, room_id),
    )
    conn.commit()
    conn.close()
    trigger_async_sync()
    flash("Room updated.", "success")
    return redirect(url_for("rooms"))


@app.route("/rooms/<int:room_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_room(room_id):
    """Remove a room — e.g. a demo room that isn't a real room at the
    hotel. Refuses if any booking or reservation (past or present) still
    references it, since deleting would orphan that history; edit the room
    instead (rename/reprice) if it needs to stay for record-keeping."""
    conn = get_db()
    room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        flash("Room not found.", "error")
        return redirect(url_for("rooms"))

    booking_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE room_id = ?", (room_id,)
    ).fetchone()[0]
    reservation_count = conn.execute(
        "SELECT COUNT(*) FROM reservations WHERE room_id = ?", (room_id,)
    ).fetchone()[0]

    if booking_count or reservation_count:
        conn.close()
        flash(
            f"Can't delete '{room['room_number']}' — it has {booking_count} booking(s) and "
            f"{reservation_count} reservation(s) on record. Rename or reprice it instead, or use "
            "'Reset Demo Data' on your Profile page first if this is still demo data.",
            "error",
        )
        return redirect(url_for("rooms"))

    conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
    conn.commit()
    conn.close()
    trigger_async_sync()
    flash(f"Room '{room['room_number']}' deleted.", "success")
    return redirect(url_for("rooms"))


@app.route("/rooms/export.json")
@roles_required("admin", "manager")
def export_rooms_json():
    """Download the current room list as JSON, to upload into the public
    website's /admin page and keep the publicly shown prices in sync. The
    two apps run on different, disconnected machines, so this is a
    deliberate manual step (see README.md)."""
    conn = get_db()
    rows = conn.execute("SELECT room_number, floor, price_per_night FROM rooms ORDER BY floor, room_number").fetchall()
    conn.close()
    payload = [
        {"name": r["room_number"], "floor": r["floor"], "price_per_night": r["price_per_night"]}
        for r in rows
    ]
    response = Response(json.dumps(payload, indent=2), mimetype="application/json")
    response.headers["Content-Disposition"] = "attachment; filename=rooms_export.json"
    return response


if __name__ == "__main__":
    init_db()
    # Start the background sync loop once — not twice. Flask's debug-mode
    # reloader restarts this script in a child process, so without this
    # guard the thread would start once in the parent process (which never
    # actually serves requests) and again in the reloaded child, doubling
    # up every sync attempt. WERKZEUG_RUN_MAIN is only set in that child.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(target=background_sync_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=True)
