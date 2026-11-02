# Mario Suites Ltd — Hotel Management Project

This project has **two separate applications**:

| Folder | What it is | Needs internet? | Runs where |
|---|---|---|---|
| `front-desk-app/` | The front-desk management system: guests, bookings, reservations, payments, receipts, rooms, reports | **No — 100% offline** | The Windows computer at the hotel's front desk |
| `public-website/` | A small public marketing website with WhatsApp/Call buttons, room prices and a map | **Yes — must be hosted on the internet** | A web host (not the front-desk PC) |

They are independent Flask apps with their own databases. They do **not** need to run on the same machine, and `front-desk-app` works with no internet connection at all — everything it needs lives in its own local `hotel.db` SQLite file.

---

## 1. Project structure

```
mario-suites-hotel-management/
├── README.md                   (this file)
├── front-desk-app/             OFFLINE — install on the hotel's Windows PC
│   ├── app.py
│   ├── requirements.txt
│   ├── static/
│   │   ├── logo.png
│   │   └── style.css
│   ├── templates/              (all the pages)
│   └── hotel.db                (created automatically on first run — not included)
└── public-website/             ONLINE — deploy to a web host
    ├── app.py
    ├── requirements.txt
    ├── static/
    │   ├── logo.png
    │   ├── style.css
    │   └── room_photos/         (uploaded via /admin)
    ├── templates/
    └── site.db                 (created automatically on first run — not included)
```

---

## 2. Front-desk app (`front-desk-app/`) — offline, local

This is the system front-desk staff use every day: register guests, create bookings, check guests in/out, record payments and print thermal receipts.

### 2.1 Ubuntu development setup

```bash
cd front-desk-app
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000** in your browser.

### 2.2 Windows setup (production, at the front desk)

1. Install **Python 3.12+** from [python.org](https://www.python.org/downloads/) (check "Add Python to PATH" during install).
2. Copy the `front-desk-app` folder onto the front-desk computer, e.g. `C:\MarioSuites\front-desk-app`.
3. Open Command Prompt in that folder and run:
   ```cmd
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
4. Open a browser to **http://127.0.0.1:5000**.
5. Log in with the seeded account **username `admin`, password `admin123`** — then immediately go to **Profile** and change the password (or create named Admin/Manager/Staff accounts from **Users** and retire the shared one).

### 2.3 Running permanently in the background (NSSM) — so staff never see a terminal

[NSSM](https://nssm.cc/) ("the Non-Sucking Service Manager") lets Windows run `app.py` as a background service that starts automatically on boot, with no console window for staff to accidentally close.

1. Download NSSM from https://nssm.cc/download and unzip it (e.g. to `C:\nssm`).
2. Open Command Prompt **as Administrator** and run:
   ```cmd
   C:\nssm\win64\nssm.exe install MarioSuitesFrontDesk
   ```
3. In the dialog that opens:
   - **Path:** `C:\MarioSuites\front-desk-app\venv\Scripts\python.exe`
   - **Startup directory:** `C:\MarioSuites\front-desk-app`
   - **Arguments:** `app.py`
4. On the **Details** tab, you can rename the display name to "Mario Suites Front Desk".
5. Click **Install service**.
6. Start it:
   ```cmd
   net start MarioSuitesFrontDesk
   ```
7. The app is now running in the background and will start automatically every time the computer boots. Staff just open a browser and go to **http://127.0.0.1:5000** — you can put a desktop shortcut to that address for convenience.
8. To stop or remove the service later:
   ```cmd
   net stop MarioSuitesFrontDesk
   C:\nssm\win64\nssm.exe remove MarioSuitesFrontDesk confirm
   ```

### 2.4 Backing up and restoring `hotel.db`

All data (guests, bookings, payments, rooms, users) lives in one file: `front-desk-app/hotel.db`.

**To back up:**
1. Stop the service first so nothing is writing to the file: `net stop MarioSuitesFrontDesk`.
2. Copy `hotel.db` somewhere safe (a USB drive, cloud folder, etc.). Date-stamp the filename, e.g. `hotel-2026-11-02.db`.
3. Restart the service: `net start MarioSuitesFrontDesk`.

**To restore:**
1. Stop the service.
2. Replace `front-desk-app\hotel.db` with your backup copy (same filename, `hotel.db`).
3. Restart the service.

Consider scheduling a nightly copy of `hotel.db` to another drive (Windows Task Scheduler running a simple `copy` command works well).

### 2.5 Changing the seeded rooms and prices

The app starts with three rooms: **A1, A2, A3**, all on the Ground Floor at ₦20,000/night. Change these from the app itself — **do not edit the database file by hand**:

1. Log in as an **Admin** or **Manager**.
2. Go to **Rooms**.
3. Each room card has an editable form (number/name, floor, price) — update and save.
4. To add more rooms, use the **"Add a new room"** form at the bottom of the Rooms page.

### 2.6 User roles

| Role | Can do |
|---|---|
| **Admin** | Everything: create/remove Manager and Staff accounts, reset passwords, edit payments after the fact, rooms/floors/prices, full revenue dashboard |
| **Manager** | Add/rename rooms and floors, change prices, view the revenue dashboard (all filters) — cannot manage user accounts |
| **Staff** | Register guests, create bookings/reservations, record payments, check guests in/out, print receipts — cannot see revenue figures, edit prices, or manage users |

The seeded default login (`admin` / `admin123`) is an **Admin** account. **Change this password immediately** — anyone with it has full access to the system and all guest/payment data.

### 2.7 Keeping the public website's prices in sync

The two apps run on different, disconnected computers and cannot share a database directly. When you change a room's price on the front-desk computer:

1. Go to **Rooms** → **Export rooms (for website)**. This downloads `rooms_export.json`.
2. On the public website, log in to **/admin** and use **"Import rooms from front-desk export"** to upload that file.

Room names, floors and prices are updated; uploaded photos on the website are left untouched.

---

## 3. Public website (`public-website/`) — online, internet-facing

A simple, free-to-run site whose only job is visibility: a home page, a rooms page with prices, and a location page with a map — each with one-tap **"Chat on WhatsApp"** and **"Call Us"** buttons. It is **not** a booking or payment system, and uses **no paid APIs or per-message services** anywhere:

- WhatsApp buttons are plain `wa.me` click-to-chat links (free, no WhatsApp Business API, no Twilio).
- The map is a bare `google.com/maps?...&output=embed` iframe and a `maps/dir/?api=1&destination=...` directions link — **no Google Maps API key required**.

### 3.1 Ubuntu development setup

```bash
cd public-website
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5001**.

### 3.2 Deploying to the internet

This app does **not** need to run on the front-desk PC. It ships ready for `gunicorn` (`requirements.txt` includes it, and `public-website/Procfile` declares `web: gunicorn app:app`), and creates/seeds `site.db` automatically the moment it's imported — not only when run directly — so a production server starting it via `app:app` works with no extra steps.

**Deploying to Render (free tier), step by step:**

1. **Put this project in a GitHub repo.** If it isn't one already:
   ```bash
   cd mario-suites-hotel-management
   git init
   git add .
   git commit -m "Mario Suites Ltd project"
   ```
   Create an empty repo on [github.com/new](https://github.com/new) (e.g. `mario-suites-hotel-management`), then:
   ```bash
   git remote add origin https://github.com/<your-username>/mario-suites-hotel-management.git
   git branch -M main
   git push -u origin main
   ```
   (`hotel.db` / `site.db` are created at runtime and shouldn't be committed — add a `.gitignore` with `*.db`, `venv/`, `__pycache__/` first if you don't already have one.)

2. **Sign up / log in at [render.com](https://render.com)** (free, no card required for the free tier) and connect your GitHub account when prompted.

3. **Create the service.** Two ways — pick either:
   - **Easiest — Blueprint:** In the Render dashboard, click **New +** → **Blueprint**, pick this repo. Render reads the `render.yaml` at the repo root and configures everything automatically (it already points at the `public-website/` subfolder, so `front-desk-app/` is never deployed). Click **Apply**.
   - **Manual:** Click **New +** → **Web Service**, pick this repo, then set:
     - **Root Directory:** `public-website`
     - **Runtime:** Python 3
     - **Build Command:** `pip install -r requirements.txt`
     - **Start Command:** `gunicorn app:app`
     - **Instance Type:** Free

4. **Deploy.** Render builds and starts the app, then gives you a live URL like `https://mario-suites-public-website.onrender.com`. Open it on your phone to test the mobile view for real.

5. **Log in at `/admin`** on that live URL (seeded `admin` / `admin123` — change it immediately) to edit content and rooms. Note this is a *separate* `site.db` from the one on your laptop, since Render runs its own copy — edits made locally won't appear on the live site until you edit them there too, or redeploy.

**Good to know about Render's free tier:** the free instance "spins down" after a period of no traffic and takes a few seconds to wake up on the next visit — fine for a demo, but for the hotel's real public site later, consider Render's cheapest paid tier (or another host) so it doesn't sleep. The free tier also doesn't guarantee the filesystem persists forever across redeploys, so treat `site.db` as something you can always regenerate (it reseeds automatically) rather than a permanent store — for lasting content, keep using the export/import JSON step to resync from the front-desk app, or ask about adding Render's free PostgreSQL add-on later if this needs to be more durable.

### 3.3 Editing the website

Log in at **/admin** with the seeded account **`admin` / `admin123`** (change this immediately, same as the front-desk app) to:

- Edit the home page overview text, address, phone numbers and WhatsApp number.
- Add/edit rooms, their floor, price, and upload a real photo for each (until a photo is uploaded, a room shows a simple placeholder card — no stock or fabricated photos are used).
- Import the `rooms_export.json` file from the front-desk app to keep prices in sync (see §2.7).

---

## 4. About the Google Maps listing

You shared this Google Maps link for the hotel: `https://maps.app.goo.gl/xM8kUuQAM8WzVrY99?g_st=ic`. **This could not be fetched automatically** — Google Maps short links aren't reachable by the tools used to build this project, so no business details or photos were pulled from it. The address and phone numbers you provided directly were used instead:

> 23/24 Danbare Layin Mobil, Opp. BUK New Site, Kano, Nigeria
> +234 701 254 9345 / +234 814 708 5305

**Update:** you later supplied the exact pin from Google Maps — `11°57'48.2"N 8°25'57.4"E`, i.e. `11.963389, 8.432611` in decimal degrees. The `/location` page's embedded map and "Get Directions" link now build from these coordinates (`HOTEL_LAT` / `HOTEL_LNG` near the top of `public-website/app.py`) instead of geocoding the address text, so the pin lands exactly on the building rather than an approximate match. If the hotel ever moves or the pin needs adjusting, just edit those two constants. Real room photos still don't exist yet; upload them via `/admin` → Rooms whenever you have them.

---

## 5. Troubleshooting

**"Port already in use" / app won't start**
Another program (or a previous copy of the app) is already using port 5000 (front-desk) or 5001 (website). Close it, or run on a different port: `python app.py` → edit the `app.run(..., port=5000)` line at the bottom of `app.py` to a free port.

**"database is locked"**
Two processes are writing to `hotel.db`/`site.db` at once — usually a leftover `python app.py` still running alongside the NSSM service. Stop the service (`net stop MarioSuitesFrontDesk`), make sure no stray `python.exe` processes remain (Task Manager), then restart.

**Can't log in**
Make sure Caps Lock isn't on and you're using the exact seeded username (`admin`). If the password was changed and forgotten, an existing Admin can reset any user's password from the **Users** page; if there is no working Admin left, stop the app, delete `hotel.db` (you will lose all data — restore from a backup instead if you have one), and restart so a fresh `admin`/`admin123` account is reseeded.

**Receipt doesn't fit the thermal printer**
The receipt is built for an 80mm roll by default. If your printer uses 58mm paper, edit `--ticket-width: 80mm;` near the top of `front-desk-app/static/style.css` to `58mm`, and the matching `width: 80mm;` under `@media print`.

**Public website prices don't match the front desk**
This is expected — the two apps don't talk to each other automatically. Follow §2.7 to export/import the rooms JSON after any price change.

---

**Security reminder:** both apps ship with the same default login, `admin` / `admin123`. Change both passwords (and the `app.secret_key` values in each `app.py`) before putting either system into real use.
