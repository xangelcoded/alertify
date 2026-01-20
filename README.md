# Alertify – Complete Working Demo (Final / Presentation Ready)

This is a **fully working end-to-end prototype** of Alertify:

- **Two logins**
  - **Admin (LGU)** login: email + password
  - **Citizen/User** login: **name only** (no password) for urgent reporting
- **Citizen feed (Facebook-like)**: users can post and see all other users’ posts
- **LGU dashboard (real-time)**: posts appear instantly + map marker pin (Server-Sent Events)
- **Auto-classification (Demo Mode)**: disaster type + urgency + location + confidence (85–99%)
- **Typo tolerant**: fuzzy matching for English/Tagalog/Taglish + common “no-space” words
- **Operations workflow (LGU)**: acknowledge, validate, resolve (status tracked)
- **Export + analytics**: quick summary + CSV export for research appendix/demo

> The current prototype uses a **lightweight NLP engine (rules + fuzzy matching)** for fast, reliable demo classification. You can later swap in **mBERT** or another transformer model without changing the frontend.

---

## Run (Mac / VS Code)

### 1) Create virtual env + install
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2) Start server
```bash
python app.py
```

Open: http://127.0.0.1:5000/

---

## Demo accounts

### Admin (LGU)
- Email: `admin@lipa.gov.ph`
- Password: `admin123`

### Citizen/User
- **Name only** (e.g., `Juan Dela Cruz`)

---

## URLs

- Home: `/`
- Admin login: `/login.html`
- User login: `/user_login.html`
- User feed (post + view others): `/user_feed.html`
- Admin dashboard: `/dashboard.html`

---

## LGU dashboard tips

- New incidents appear in the right-side notifier list **and** create a **map pin**.
- If a post has no clear barangay, it still gets pinned using a **stable “Lipa City (unspecified)” marker**.
- Click an alert card to focus the marker and open the incident detail panel.
- Press **S** to toggle dashboard sound (toast + beep on new CRITICAL/HIGH).

---

## Quick API extras (optional for demo)

- **Analytics** (admin): `GET /api/analytics`
- **CSV export** (admin): `GET /api/export.csv`
- **Seed demo incidents** (admin): `POST /api/seed_demo`

---

## What the system classifies

- **Disaster Type**: Flood, Fire, Typhoon/Storm, Earthquake, Landslide, Volcano, Medical, Power, **Rescue**
- **Urgency**:
  - **CRITICAL**: trapped / rooftop / children / SOS / strong rescue wording
  - **HIGH**: urgent assistance, dangerous conditions, active fire, etc.
  - **MODERATE**: monitoring-level hazard posts
- **Location**: detects common Lipa barangays + maps them; otherwise pins to Lipa City fallback
- **Confidence**: deterministic **85–99%** based on post content

---

## Presentation setup

Open two windows:
1) **Admin dashboard**: login as admin
2) **Incognito user feed**: login with name only

Citizen posts → LGU dashboard updates instantly.
