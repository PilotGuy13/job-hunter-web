# 🔒 Job Hunter Web

A multi-user web application for automated GRC/Security job discovery with Claude AI scoring.

## Features

- **Multi-user** — each user has their own profile, CV, keywords, and job digest
- **Password protected** — login page per user, admin panel for management
- **Run Now button** — trigger a search instantly from the browser
- **Live progress** — streaming log shows search progress in real time
- **Results browser** — filter, save, and dismiss jobs from the web UI
- **Email digest** — sends a formatted HTML email when jobs are found
- **Dashboard** — stats, recent jobs, and quick actions

## Setup

### 1. Install dependencies

```bash
cd job_hunter_web
pip3 install -r requirements.txt
```

### 2. Run the app

```bash
python3 app.py
```

Open your browser at: **http://localhost:5000**

### 3. Default login credentials

| Username | Password         | Role  |
|----------|-----------------|-------|
| john     | JobHunter2026!  | Admin |
| partner  | JobHunter2026!  | User  |

**Change these immediately after first login** via Settings → Change Password.

### 4. Configure your profile

After logging in:
1. Go to **My Profile**
2. Fill in your CV summary, recipient email, Gmail sender, API keys
3. Select your search locations and keywords
4. Click **Save Profile**

### 5. Run your first search

Click **▶ Run Now** on the dashboard. Watch the live progress log.
Results appear in the dashboard and an email is sent to your digest address.

---

## API Keys needed

| Key | Where to get | Cost |
|-----|-------------|------|
| Anthropic | https://console.anthropic.com | ~$0.05/run |
| Adzuna App ID + Key | https://developer.adzuna.com | Free (250 calls/month) |
| Gmail App Password | https://myaccount.google.com/apppasswords | Free |

---

## File structure

```
job_hunter_web/
  app.py          — Flask web application
  job_engine.py   — Search + scoring engine
  models.py       — Database models
  requirements.txt
  templates/
    base.html     — Shared layout + sidebar
    login.html    — Login page
    dashboard.html— Main dashboard
    profile.html  — CV and settings form
    settings.html — Score threshold, schedule, password
    results.html  — All results with filters
    saved.html    — Bookmarked jobs
    admin.html    — User management (admin only)
  instance/
    job_hunter.db — SQLite database (auto-created)
```

---

## Moving to hosted (Option B later)

When ready to host online:
1. Push to a **private GitHub repo**
2. Deploy to **Railway** or **Render** (both have free tiers)
3. Set environment variables for API keys
4. Access from any device including phone

---

*Built for John Klitgaard · Wellington, NZ · 2026*
