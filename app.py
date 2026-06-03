"""
app.py — Job Hunter Web Application
Run with: python app.py
Then open: http://localhost:5000
"""
import json
import logging
import os
import threading
from datetime import datetime

from flask import (Flask, Response, flash, jsonify, redirect,
                   render_template, request, stream_with_context, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)

from models import JobResult, JobSource, User, UsageLog, db
from job_engine import ALL_LOCATIONS, build_email_html, job_fingerprint, run_for_user

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"]         = os.environ.get("SECRET_KEY", "change-me-in-production-please")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///job_hunter.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access Job Hunter."

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def get_effective_key(user, key_name):
    """Return user key if set, otherwise fall back to admin shared key."""
    user_val = getattr(user, key_name, "") or ""
    if user_val.strip():
        return user_val.strip()
    admin = User.query.filter_by(is_admin=True).first()
    if admin:
        return (getattr(admin, key_name, "") or "").strip()
    return ""


# Track running jobs per user
# _running: {user_id: threading.Thread}
# _run_log:  {user_id: [log lines]}
# _run_done: {user_id: bool}
import threading as _threading
import queue as _queue
_running  = {}
_run_log  = {}
_run_done = {}
_run_stop = {}   # {user_id: True} — signals background thread to stop


def _save_results(app_ctx, user_id, result):
    """Save job results to DB inside app context."""
    with app_ctx:
        from models import JobResult, User
        from sqlalchemy.orm import scoped_session
        user = User.query.get(user_id)
        for job in result.get("relevant_jobs", []):
            jr = JobResult(
                user_id             = user_id,
                title               = job.get("title",""),
                company             = job.get("company",""),
                location            = job.get("location",""),
                url                 = job.get("url",""),
                source              = job.get("source",""),
                listed              = job.get("listed",""),
                description         = job.get("description",""),
                search_location     = job.get("search_location",""),
                compatibility_score = job.get("compatibility_score",0),
                compatibility_label = job.get("compatibility_label",""),
                apply_priority      = job.get("apply_priority","Medium"),
                salary_estimate     = job.get("salary_estimate",""),
                hiring_manager_search = job.get("hiring_manager_search",""),
                linkedin_search     = job.get("linkedin_search",""),
                emailed             = result.get("emailed", False),
            )
            jr.match_reasons = job.get("match_reasons",[])
            jr.gaps          = job.get("gaps",[])
            jr.cv_tweaks     = job.get("cv_tweaks",[])
            db.session.add(jr)
        if user:
            user.last_run        = datetime.utcnow()
            user.last_run_status = (f"Found {len(result.get('relevant_jobs',[]))} relevant jobs "
                                    f"({'emailed' if result.get('emailed') else 'not emailed'})")
        db.session.commit()


def _run_background(user_id, app_context):
    """Background thread — runs the full job search independent of HTTP connection."""
    log_lines = _run_log.setdefault(user_id, [])
    _run_done[user_id] = False
    _run_stop[user_id] = False

    def progress(msg):
        log.info(msg)
        log_lines.append(msg)

    try:
        with app_context:
            user = User.query.get(user_id)
            seen = {r.url for r in JobResult.query.filter_by(user_id=user_id).all() if r.url}

        progress("Starting job search...")
        with app_context:
            user = User.query.get(user_id)

        result = run_for_user(user, seen, progress_callback=progress,
                                   stop_check=lambda: _run_stop.get(user_id, False))

        # Save in a fresh context
        _save_results(app.app_context(), user_id, result)
        progress(f"✅ Done — {result.get('relevant', 0)} relevant jobs found.")
        progress("DONE")
    except Exception as e:
        progress(f"❌ Error: {e}")
        progress("DONE")
    finally:
        _run_done[user_id] = True


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/welcome")
def landing():
    """Public landing page — visible without login."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user     = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            # Check MFA if enabled
            if getattr(user, "mfa_enabled", False):
                from flask import session
                session["mfa_user_id"] = user.id
                return redirect(url_for("mfa_verify"))

            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user, remember=True)
            if getattr(user, "must_change_password", False):
                flash("Welcome! Please set a new password before continuing.", "success")
                return redirect(url_for("settings"))
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/mfa/verify", methods=["GET", "POST"])
def mfa_verify():
    """MFA verification page shown after password check."""
    from flask import session
    user_id = session.get("mfa_user_id")
    if not user_id:
        return redirect(url_for("login"))

    user = User.query.get(user_id)
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        token = request.form.get("token", "").strip()
        if _verify_totp(user.mfa_secret, token):
            session.pop("mfa_user_id", None)
            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user, remember=True)
            if getattr(user, "must_change_password", False):
                flash("Welcome! Please set a new password.", "success")
                return redirect(url_for("settings"))
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid verification code. Please try again.", "error")

    return render_template("mfa_verify.html")


# ── Forgot / Reset Password ───────────────────────────────────────────────────

import secrets
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText as _MIMEText
from email.mime.multipart import MIMEMultipart as _MIMEMultipart

# In-memory token store: {token: (user_id, expiry)}
_reset_tokens = {}


def _send_reset_email(to_email, reset_url, sender_email, smtp_password):
    msg = _MIMEMultipart("alternative")
    msg["Subject"] = "Job Hunter — Password Reset"
    msg["From"]    = sender_email
    msg["To"]      = to_email
    body = f"""
    <p>Hi,</p>
    <p>You requested a password reset for your Job Hunter account.</p>
    <p><a href="{reset_url}" style="background:#1d4ed8;color:#fff;padding:10px 20px;
       border-radius:8px;text-decoration:none;font-weight:600;">Reset My Password</a></p>
    <p>This link expires in <strong>1 hour</strong>.</p>
    <p>If you did not request this, ignore this email — your password will not change.</p>
    """
    msg.attach(_MIMEText(body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo(); s.starttls()
        s.login(sender_email, smtp_password)
        s.sendmail(sender_email, to_email, msg.as_string())


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = User.query.filter(
            db.func.lower(User.email) == email
        ).first() or User.query.filter(
            db.func.lower(User.recipient_email) == email
        ).first()

        # Always show the same message (prevents email enumeration)
        flash("If that email is registered, a reset link has been sent.", "success")

        if user:
            token  = secrets.token_urlsafe(32)
            expiry = datetime.utcnow() + timedelta(hours=1)
            _reset_tokens[token] = (user.id, expiry)

            reset_url = url_for("reset_password", token=token, _external=True)

            # Use admin Gmail to send, fall back to user sender
            admin = User.query.filter_by(is_admin=True).first()
            sender  = (admin.sender_email  if admin and admin.sender_email  else "")
            smtp_pw = (admin.smtp_password if admin and admin.smtp_password else "")

            if sender and smtp_pw:
                try:
                    _send_reset_email(email, reset_url, sender, smtp_pw)
                    log.info(f"Password reset email sent to {email}")
                except Exception as e:
                    log.warning(f"Reset email failed: {e}")
            else:
                log.warning("No sender email configured — cannot send reset email")

        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    entry = _reset_tokens.get(token)
    if not entry:
        flash("Invalid or expired reset link.", "error")
        return redirect(url_for("login"))

    user_id, expiry = entry
    if datetime.utcnow() > expiry:
        del _reset_tokens[token]
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    user = User.query.get(user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        pw1 = request.form.get("password",  "").strip()
        pw2 = request.form.get("password2", "").strip()
        if len(pw1) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif pw1 != pw2:
            flash("Passwords do not match.", "error")
        else:
            user.set_password(pw1)
            db.session.commit()
            del _reset_tokens[token]
            flash("Password updated successfully. Please log in.", "success")
            return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    recent_jobs = (JobResult.query
                   .filter_by(user_id=current_user.id, dismissed=False)
                   .order_by(JobResult.found_at.desc())
                   .limit(50).all())
    is_running  = current_user.id in _running and _running[current_user.id].is_alive()
    return render_template("dashboard.html", jobs=recent_jobs, is_running=is_running,
                           locations=ALL_LOCATIONS)


# ── Profile ───────────────────────────────────────────────────────────────────

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.full_name       = request.form.get("full_name", "")
        current_user.cv_summary      = request.form.get("cv_summary", "")
        current_user.recipient_email = request.form.get("recipient_email", "")
        # Only admin can change sender email and API keys
        if current_user.is_admin:
            current_user.sender_email   = request.form.get("sender_email", "")
            new_smtp = request.form.get("smtp_password", "")
            if new_smtp.strip():
                current_user.smtp_password = new_smtp.strip()
            current_user.anthropic_key  = request.form.get("anthropic_key", "")
            current_user.adzuna_app_id  = request.form.get("adzuna_app_id", "")
            current_user.adzuna_app_key = request.form.get("adzuna_app_key", "")

        # Keywords — one per line
        kws = [k.strip() for k in request.form.get("keywords","").splitlines() if k.strip()]
        current_user.keywords = kws

        # Locations — checkboxes
        selected_locs = request.form.getlist("locations")
        current_user.locations = selected_locs

        # Notification preference
        current_user.notification_pref = request.form.get("notification_pref", "both")

        # Work arrangement preference
        selected_arr = request.form.getlist("work_arrangement")
        current_user.work_arrangement = selected_arr

        db.session.commit()
        flash("Profile saved successfully.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html", locations=ALL_LOCATIONS)


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        try:
            current_user.score_threshold   = int(request.form.get("score_threshold", 20))
            current_user.max_jobs_to_score = int(request.form.get("max_jobs_to_score", 25))
            current_user.schedule_hour_utc = int(request.form.get("schedule_hour_utc", 21))

            # Change password
            new_pw = request.form.get("new_password","").strip()
            if new_pw:
                current_user.set_password(new_pw)
                current_user.must_change_password = False

            # Feature toggles
            current_user.enable_job_alerts = "enable_job_alerts" in request.form
            current_user.enable_weekly_summary = "enable_weekly_summary" in request.form
            current_user.dark_mode = "dark_mode" in request.form
            current_user.color_theme = request.form.get("color_theme", "default")

            db.session.commit()
            flash("Settings saved.", "success")
        except ValueError:
            flash("Invalid value — please enter numbers only.", "error")
        return redirect(url_for("settings"))
    return render_template("settings.html")


# ── Run Now (background thread — keeps running if user navigates away) ───────

@app.route("/run", methods=["POST"])
@login_required
def run_now():
    """Start job search in background thread. Returns immediately so user can navigate freely."""
    uid = current_user.id
    if uid in _running and _running[uid].is_alive():
        return jsonify({"error": "Already running", "running": True}), 409
    _run_log[uid]  = []
    _run_done[uid] = False
    t = _threading.Thread(target=_run_background, args=(uid, app.app_context()), daemon=True)
    _running[uid] = t
    t.start()
    return jsonify({"ok": True, "message": "Search started in background"})


@app.route("/run/status")
@login_required
def run_status():
    """Poll for background run status and new log lines."""
    uid        = current_user.id
    since      = request.args.get("since", 0, type=int)
    is_running = uid in _running and _running[uid].is_alive()
    done       = _run_done.get(uid, True)
    log_lines  = _run_log.get(uid, [])
    return jsonify({
        "running":     is_running,
        "done":        done or not is_running,
        "lines":       log_lines[since:],
        "total_lines": len(log_lines),
    })


@app.route("/run/stop", methods=["POST"])
@login_required
def run_stop():
    """Signal the background thread to stop."""
    uid = current_user.id
    _run_stop[uid] = True
    return jsonify({"ok": True, "message": "Stop signal sent"})


# ── Results / Job actions ──────────────────────────────────────────────────────

@app.route("/results")
@login_required
def results():
    page       = request.args.get("page", 1, type=int)
    min_score  = request.args.get("min_score", 0, type=int)
    source     = request.args.get("source", "")
    location   = request.args.get("location", "")
    sort       = request.args.get("sort", "date")
    priority   = request.args.get("priority", "")
    date_range = request.args.get("date_range", "")

    query = (JobResult.query
             .filter_by(user_id=current_user.id, dismissed=False)
             .filter(JobResult.compatibility_score >= min_score))
    if source:
        query = query.filter_by(source=source)
    if location:
        query = query.filter_by(search_location=location)
    if priority:
        query = query.filter_by(apply_priority=priority)
    if date_range:
        from datetime import timedelta
        now = datetime.utcnow()
        if date_range == "today":
            query = query.filter(JobResult.found_at >= now.replace(hour=0, minute=0, second=0))
        elif date_range == "7days":
            query = query.filter(JobResult.found_at >= now - timedelta(days=7))
        elif date_range == "30days":
            query = query.filter(JobResult.found_at >= now - timedelta(days=30))

    if sort == "score":
        query = query.order_by(JobResult.compatibility_score.desc())
    elif sort == "priority":
        from sqlalchemy import case
        query = query.order_by(
            case({"High": 0, "Medium": 1, "Low": 2}, value=JobResult.apply_priority),
            JobResult.compatibility_score.desc()
        )
    else:
        query = query.order_by(JobResult.found_at.desc())

    jobs = query.paginate(page=page, per_page=20)
    sources   = db.session.query(JobResult.source).filter_by(user_id=current_user.id).distinct().all()
    locations = db.session.query(JobResult.search_location).filter_by(user_id=current_user.id).distinct().all()
    return render_template("results.html", jobs=jobs, sources=sources, locations=locations,
                           min_score=min_score, source=source, location=location,
                           sort=sort, priority=priority, date_range=date_range)


@app.route("/job/<int:job_id>/save", methods=["POST"])
@login_required
def save_job(job_id):
    job = JobResult.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    job.saved = not job.saved
    db.session.commit()
    return jsonify({"saved": job.saved})


@app.route("/job/<int:job_id>/dismiss", methods=["POST"])
@login_required
def dismiss_job(job_id):
    job = JobResult.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    job.dismissed = True
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/saved")
@login_required
def saved_jobs():
    jobs = (JobResult.query
            .filter_by(user_id=current_user.id, saved=True, dismissed=False)
            .order_by(JobResult.compatibility_score.desc()).all())
    return render_template("saved.html", jobs=jobs)


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
def admin():
    if not current_user.is_admin:
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))
    users = User.query.all()
    return render_template("admin.html", users=users)


@app.route("/admin/user/<int:user_id>/toggle", methods=["POST"])
@login_required
def toggle_user(user_id):
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()
    return jsonify({"active": user.is_active})


@app.route("/admin/user/<int:user_id>/profile", methods=["GET", "POST"])
@login_required
def admin_user_profile(user_id):
    if not current_user.is_admin:
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))
    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        user.full_name       = request.form.get("full_name", "")
        user.cv_summary      = request.form.get("cv_summary", "")
        user.recipient_email = request.form.get("recipient_email", "")
        user.notification_pref = request.form.get("notification_pref", "both")

        kws = [k.strip() for k in request.form.get("keywords","").splitlines() if k.strip()]
        user.keywords = kws

        selected_locs = request.form.getlist("locations")
        user.locations = selected_locs

        selected_arr = request.form.getlist("work_arrangement")
        user.work_arrangement = selected_arr

        user.score_threshold   = int(request.form.get("score_threshold", 20))
        max_cap = 50 if current_user.is_admin else 25
        user.max_jobs_to_score = min(int(request.form.get("max_jobs_to_score", 25)), max_cap)

        db.session.commit()
        flash(f"Profile for {user.full_name or user.username} saved.", "success")
        return redirect(url_for("admin_user_profile", user_id=user_id))

    return render_template("admin_user_profile.html", target_user=user, locations=ALL_LOCATIONS)


# ── Invite User ──────────────────────────────────────────────────────────────

@app.route("/admin/invite", methods=["GET", "POST"])
@login_required
def invite_user():
    if not current_user.is_admin:
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email    = request.form.get("email",    "").strip().lower()
        fullname = request.form.get("full_name","").strip()

        if not email:
            flash("Email address is required.", "error")
            return redirect(url_for("invite_user"))

        # Check not already registered
        if User.query.filter(db.func.lower(User.email) == email).first():
            flash(f"User {email} already exists.", "error")
            return redirect(url_for("invite_user"))

        # Generate a secure 15-character default password
        import secrets, string
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        temp_password = "".join(secrets.choice(alphabet) for _ in range(15))

        # Username = email (feature #4)
        new_user = User(
            username        = email,
            email           = email,
            full_name       = fullname,
            recipient_email = email,
            is_admin        = False,
            must_change_password = True,
        )
        new_user.set_password(temp_password)
        db.session.add(new_user)
        db.session.commit()

        # Send invite email
        admin = User.query.filter_by(is_admin=True).first()
        sender  = admin.sender_email  if admin else ""
        smtp_pw = admin.smtp_password if admin else ""
        login_url = url_for("login", _external=True)

        if sender and smtp_pw:
            try:
                import smtplib as _smtp
                from email.mime.multipart import MIMEMultipart as _MM
                from email.mime.text import MIMEText as _MT
                msg = _MM("alternative")
                msg["Subject"] = "You've been invited to Job Hunter"
                msg["From"]    = sender
                msg["To"]      = email
                body = f"""
                <p>Hi {fullname or email},</p>
                <p>You've been invited to <strong>Job Hunter</strong> — an AI-powered job search platform.</p>
                <p><strong>Your login details:</strong></p>
                <ul>
                  <li>URL: <a href="{login_url}">{login_url}</a></li>
                  <li>Username: <strong>{email}</strong></li>
                  <li>Temporary Password: <strong>{temp_password}</strong></li>
                </ul>
                <p>You will be asked to change your password on first login.</p>
                <p style="font-size:12px;color:#64748b;">
                  &copy; 2026 Silver Fern Consulting Ltd. All Rights Reserved.
                </p>
                """
                msg.attach(_MT(body, "html"))
                with _smtp.SMTP("smtp.gmail.com", 587) as s:
                    s.ehlo(); s.starttls()
                    s.login(sender, smtp_pw)
                    s.sendmail(sender, email, msg.as_string())
                flash(f"Invite sent to {email}. Temporary password: {temp_password}", "success")
                log.info(f"Invite sent to {email}")
            except Exception as e:
                flash(f"User created but email failed: {e}. Temp password: {temp_password}", "error")
        else:
            flash(f"User created. No email configured. Temp password: {temp_password}", "success")

        return redirect(url_for("admin"))

    return render_template("invite_user.html")


# ── Preview last digest ───────────────────────────────────────────────────────

@app.route("/preview")
@login_required
def preview():
    """Wrapper page with sidebar — loads email HTML in an iframe."""
    jobs = (JobResult.query
            .filter_by(user_id=current_user.id)
            .order_by(JobResult.found_at.desc())
            .limit(25).all())
    if not jobs:
        return render_template("preview_empty.html")
    return render_template("preview.html", job_count=len(jobs))


@app.route("/preview/html")
@login_required
def preview_html():
    """Raw email HTML — loaded inside the iframe on /preview."""
    jobs = (JobResult.query
            .filter_by(user_id=current_user.id)
            .order_by(JobResult.found_at.desc())
            .limit(25).all())
    if not jobs:
        return "<p style='font-family:sans-serif;padding:40px;'>No jobs found yet.</p>"

    job_dicts = []
    for j in jobs:
        job_dicts.append({
            "title": j.title, "company": j.company, "location": j.location,
            "url": j.url, "source": j.source, "listed": j.listed,
            "description": j.description, "search_location": j.search_location,
            "compatibility_score": j.compatibility_score,
            "compatibility_label": j.compatibility_label,
            "apply_priority": j.apply_priority, "salary_estimate": j.salary_estimate,
            "match_reasons": j.match_reasons, "gaps": j.gaps, "cv_tweaks": j.cv_tweaks,
            "hiring_manager_search": j.hiring_manager_search,
            "linkedin_search": j.linkedin_search,
        })
    return build_email_html(job_dicts, current_user.full_name)


# ── Application Tracker ──────────────────────────────────────────────────────

APP_STATUSES = [
    {"id": "watching",     "label": "👀 Watching",      "color": "#6b7280", "bg": "#f3f4f6"},
    {"id": "applied",      "label": "📨 Applied",       "color": "#1d4ed8", "bg": "#dbeafe"},
    {"id": "interviewing", "label": "🎤 Interviewing",  "color": "#7c3aed", "bg": "#ede9fe"},
    {"id": "offered",      "label": "🎉 Offered",       "color": "#059669", "bg": "#d1fae5"},
    {"id": "rejected",     "label": "❌ Rejected",      "color": "#dc2626", "bg": "#fef2f2"},
    {"id": "withdrawn",    "label": "🚫 Withdrawn",     "color": "#9ca3af", "bg": "#f9fafb"},
]


@app.route("/tracker")
@login_required
def tracker():
    """Kanban-style application pipeline view."""
    view = request.args.get("view", "board")  # board or list
    jobs_by_status = {}
    for s in APP_STATUSES:
        jobs_by_status[s["id"]] = (
            JobResult.query
            .filter_by(user_id=current_user.id, app_status=s["id"], dismissed=False)
            .order_by(JobResult.status_updated.desc().nullsfirst(), JobResult.found_at.desc())
            .all()
        )
    total_active = sum(len(v) for k, v in jobs_by_status.items() if k not in ["rejected", "withdrawn"])
    return render_template("tracker.html", statuses=APP_STATUSES,
                           jobs_by_status=jobs_by_status, view=view, total_active=total_active)


@app.route("/job/<int:job_id>/status", methods=["POST"])
@login_required
def update_job_status(job_id):
    """Update application status for a job."""
    job = JobResult.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    new_status = request.form.get("status", "").strip()
    valid = [s["id"] for s in APP_STATUSES]
    if new_status not in valid:
        return jsonify({"error": "Invalid status"}), 400
    job.app_status = new_status
    job.status_updated = datetime.utcnow()
    if new_status == "applied" and not job.applied_date:
        job.applied_date = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "status": new_status})


@app.route("/job/<int:job_id>/notes", methods=["POST"])
@login_required
def update_job_notes(job_id):
    """Update notes and contact info for a job application."""
    job = JobResult.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    job.app_notes     = request.form.get("notes",         job.app_notes)
    job.contact_name  = request.form.get("contact_name",  job.contact_name)
    job.contact_email = request.form.get("contact_email", job.contact_email)
    interview = request.form.get("interview_date", "").strip()
    if interview:
        try:
            job.interview_date = datetime.strptime(interview, "%Y-%m-%d")
        except ValueError:
            pass
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/job/<int:job_id>/detail")
@login_required
def job_detail(job_id):
    """Return job details as JSON for the tracker modal."""
    job = JobResult.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    return jsonify({
        "id":              job.id,
        "title":           job.title,
        "company":         job.company,
        "location":        job.location,
        "url":             job.url,
        "source":          job.source,
        "salary_estimate": job.salary_estimate,
        "compatibility_score": job.compatibility_score,
        "compatibility_label": job.compatibility_label,
        "app_status":      job.app_status or "watching",
        "app_notes":       job.app_notes or "",
        "contact_name":    job.contact_name or "",
        "contact_email":   job.contact_email or "",
        "interview_date":  job.interview_date.strftime("%Y-%m-%d") if job.interview_date else "",
        "match_reasons":   job.match_reasons,
        "gaps":            job.gaps,
        "cv_tweaks":       job.cv_tweaks,
    })


# ── MFA ───────────────────────────────────────────────────────────────────────

@app.route("/mfa/setup", methods=["GET", "POST"])
@login_required
def mfa_setup():
    """Set up TOTP MFA for the current user."""
    import base64, hmac, struct, time as _time

    if request.method == "POST":
        token = request.form.get("token", "").strip()
        if not token or not current_user.mfa_secret:
            flash("Please scan the QR code first.", "error")
            return redirect(url_for("mfa_setup"))

        # Verify TOTP
        if _verify_totp(current_user.mfa_secret, token):
            current_user.mfa_enabled = True
            db.session.commit()
            flash("MFA enabled successfully!", "success")
            return redirect(url_for("settings"))
        else:
            flash("Invalid code. Please try again.", "error")
            return redirect(url_for("mfa_setup"))

    # Generate new secret
    import secrets as _sec
    secret = base64.b32encode(_sec.token_bytes(20)).decode("utf-8").rstrip("=")
    current_user.mfa_secret = secret
    db.session.commit()

    # Build provisioning URI for authenticator apps
    issuer = "AI Job Hunter"
    account = current_user.email or current_user.username
    uri = f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}&digits=6&period=30"
    qr_url = f"https://chart.googleapis.com/chart?chs=200x200&cht=qr&chl={uri}"

    return render_template("mfa_setup.html", secret=secret, qr_url=qr_url)


@app.route("/mfa/disable", methods=["POST"])
@login_required
def mfa_disable():
    current_user.mfa_enabled = False
    current_user.mfa_secret = ""
    db.session.commit()
    flash("MFA disabled.", "success")
    return redirect(url_for("settings"))


def _verify_totp(secret, token, window=1):
    """Verify a TOTP token against a base32 secret."""
    import base64, hmac, struct, hashlib, time as _time
    try:
        # Pad secret
        padded = secret + "=" * (8 - len(secret) % 8) if len(secret) % 8 else secret
        key = base64.b32decode(padded.upper())
        current_time = int(_time.time()) // 30

        for offset in range(-window, window + 1):
            counter = struct.pack(">Q", current_time + offset)
            h = hmac.new(key, counter, hashlib.sha1).digest()
            o = h[-1] & 0x0F
            code = str((struct.unpack(">I", h[o:o+4])[0] & 0x7FFFFFFF) % 1000000).zfill(6)
            if code == token.strip():
                return True
        return False
    except Exception:
        return False


# ── Usage Dashboard ──────────────────────────────────────────────────────────

@app.route("/admin/usage")
@login_required
def usage_dashboard():
    if not current_user.is_admin:
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    from datetime import timedelta
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    users = User.query.all()
    user_stats = []
    total_cost = 0.0
    total_scored = 0
    total_emails = 0

    for user in users:
        logs = UsageLog.query.filter_by(user_id=user.id).filter(UsageLog.date >= month_ago).all()
        scored = sum(l.jobs_scored for l in logs)
        cost = sum(l.est_cost_usd for l in logs)
        emails = sum(l.emails_sent for l in logs)
        searches = sum(l.jobs_searched for l in logs)
        total_cost += cost
        total_scored += scored
        total_emails += emails
        user_stats.append({
            "user": user,
            "month_scored": scored,
            "month_cost": round(cost, 4),
            "month_emails": emails,
            "month_searches": searches,
            "job_count": len(user.jobs),
        })

    return render_template("usage_dashboard.html", user_stats=user_stats,
                           total_cost=round(total_cost, 4), total_scored=total_scored,
                           total_emails=total_emails)


# ── Dark Mode Toggle ─────────────────────────────────────────────────────────

@app.route("/toggle-dark-mode", methods=["POST"])
@login_required
def toggle_dark_mode():
    current_user.dark_mode = not current_user.dark_mode
    db.session.commit()
    return jsonify({"dark_mode": current_user.dark_mode})


# ── Weekly Summary ────────────────────────────────────────────────────────────

@app.route("/weekly-summary")
@login_required
def weekly_summary():
    from datetime import timedelta
    today = datetime.utcnow().date()

    weeks = []
    for i in range(4):
        week_start = today - timedelta(days=today.weekday() + 7 * i)
        week_end = week_start + timedelta(days=6)
        jobs = (JobResult.query
                .filter_by(user_id=current_user.id)
                .filter(db.func.date(JobResult.found_at) >= week_start)
                .filter(db.func.date(JobResult.found_at) <= week_end)
                .all())
        weeks.append({
            "label": f"{week_start.strftime('%d %b')} - {week_end.strftime('%d %b')}",
            "total": len(jobs),
            "high": sum(1 for j in jobs if j.apply_priority == "High"),
            "excellent": sum(1 for j in jobs if "Excellent" in (j.compatibility_label or "")),
            "saved": sum(1 for j in jobs if j.saved),
            "applied": sum(1 for j in jobs if j.app_status == "applied"),
        })

    return render_template("weekly_summary.html", weeks=weeks)


# ── Interview Prep ────────────────────────────────────────────────────────────

@app.route("/job/<int:job_id>/interview-prep")
@login_required
def interview_prep(job_id):
    """Generate interview questions using Claude AI based on job + CV."""
    job = JobResult.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()

    # Get effective API key
    anthropic_key = get_effective_key(current_user, "anthropic_key")
    if not anthropic_key:
        flash("Anthropic API key not configured.", "error")
        return redirect(url_for("results"))

    from anthropic import Anthropic
    client = Anthropic(api_key=anthropic_key)

    prompt = f"""You are a senior interview coach. Based on the job description and candidate CV below,
generate a comprehensive interview preparation guide.

JOB:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {job.description or 'Not available'}

CANDIDATE CV SUMMARY:
{current_user.cv_summary or 'Not provided'}

CANDIDATE MATCH REASONS: {', '.join(job.match_reasons) if job.match_reasons else 'N/A'}
CANDIDATE GAPS: {', '.join(job.gaps) if job.gaps else 'N/A'}

Please provide:
1. **5 likely technical/domain questions** they will ask, with suggested answers
2. **3 behavioural questions** (STAR format suggestions)
3. **3 questions the candidate should ask** the interviewer
4. **Key talking points** — what to emphasise from your CV for THIS specific role
5. **Potential red flags** — gaps to prepare explanations for
6. **Salary negotiation tips** for this role/market

Format as clear sections with bullet points. Be specific to this exact role and company."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        prep_content = response.content[0].text

        # Log usage
        try:
            from models import UsageLog
            from datetime import date as _date
            today = _date.today()
            usage = UsageLog.query.filter_by(user_id=current_user.id, date=today).first()
            if not usage:
                usage = UsageLog(user_id=current_user.id, date=today)
            usage.api_calls += 1
            usage.est_cost_usd += 0.003
            db.session.add(usage)
            db.session.commit()
        except Exception:
            pass

    except Exception as e:
        prep_content = f"Error generating interview prep: {e}"

    import re
    html = prep_content
    html = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'^- (.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
    html = re.sub(r'^(\d+)\. (.+)$', r'<li>\2</li>', html, flags=re.MULTILINE)
    html = re.sub(r'((?:<li>.*</li>\n?)+)', r'<ul>\1</ul>', html)
    html = html.replace('---', '<hr>')
    html = re.sub(r'\n\n', '</p><p>', html)
    html = '<p>' + html + '</p>'
    html = html.replace('<p><h', '<h').replace('</h1></p>', '</h1>').replace('</h2></p>', '</h2>').replace('</h3></p>', '</h3>')
    html = html.replace('<p><hr></p>', '<hr>').replace('<p><ul>', '<ul>').replace('</ul></p>', '</ul>')

    return render_template("interview_prep.html", job=job, prep_content=html)


# ── Contact Us ───────────────────────────────────────────────────────────────

@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name    = request.form.get("name",    "").strip()
        email   = request.form.get("email",   "").strip()
        message = request.form.get("message", "").strip()

        if not name or not email or not message:
            flash("Please fill in all fields.", "error")
            return redirect(url_for("contact"))

        # Send via admin Gmail
        admin = User.query.filter_by(is_admin=True).first()
        sender  = admin.sender_email  if admin else ""
        smtp_pw = admin.smtp_password if admin else ""

        if sender and smtp_pw:
            try:
                import smtplib as _smtp
                from email.mime.multipart import MIMEMultipart as _MM
                from email.mime.text import MIMEText as _MT
                msg = _MM("alternative")
                msg["Subject"] = f"Job Hunter Contact Form — {name}"
                msg["From"]    = sender
                msg["To"]      = "johnbklitgaard@outlook.com"
                msg["Reply-To"]= email
                body = f"""
                <p><strong>Name:</strong> {name}</p>
                <p><strong>Email:</strong> {email}</p>
                <p><strong>Message:</strong></p>
                <p>{message}</p>
                """
                msg.attach(_MT(body, "html"))
                with _smtp.SMTP("smtp.gmail.com", 587) as s:
                    s.ehlo(); s.starttls()
                    s.login(sender, smtp_pw)
                    s.sendmail(sender, "johnbklitgaard@outlook.com", msg.as_string())
                flash("Message sent! We'll be in touch shortly.", "success")
            except Exception as e:
                flash(f"Could not send message: {e}", "error")
        else:
            flash("Contact form is not configured yet. Please try again later.", "error")
        return redirect(url_for("contact"))

    return render_template("contact.html")


# ── Job Sources ──────────────────────────────────────────────────────────────

@app.route("/admin/sources")
@login_required
def admin_sources():
    if not current_user.is_admin:
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))
    sources = JobSource.query.order_by(JobSource.is_builtin.desc(), JobSource.name).all()
    return render_template("sources.html", sources=sources)


@app.route("/admin/sources/add", methods=["POST"])
@login_required
def add_source():
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    name  = request.form.get("name",  "").strip()
    url   = request.form.get("rss_url", "").strip()
    color = request.form.get("color", "#6b7280").strip()
    if not name or not url:
        flash("Name and RSS URL are required.", "error")
        return redirect(url_for("admin_sources"))
    source = JobSource(name=name, rss_url=url, color=color,
                       source_type="rss", is_builtin=False,
                       added_by=current_user.id)
    db.session.add(source)
    db.session.commit()
    flash(f"Source '{name}' added.", "success")
    return redirect(url_for("admin_sources"))


@app.route("/admin/sources/<int:source_id>/toggle", methods=["POST"])
@login_required
def toggle_source(source_id):
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    source = JobSource.query.get_or_404(source_id)
    source.is_active = not source.is_active
    db.session.commit()
    return jsonify({"active": source.is_active})


@app.route("/admin/sources/<int:source_id>/delete", methods=["POST"])
@login_required
def delete_source(source_id):
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    source = JobSource.query.get_or_404(source_id)
    if source.is_builtin:
        return jsonify({"error": "Cannot delete built-in sources"}), 400
    db.session.delete(source)
    db.session.commit()
    return jsonify({"ok": True})


# ── DB init & seed ────────────────────────────────────────────────────────────

def create_default_users():
    """Create default users on first run."""
    if User.query.count() == 0:
        # Admin / John
        john = User(
            username       = "john",
            email          = "johnbklitgaard@outlook.com",
            full_name      = "John Klitgaard",
            recipient_email= "johnbklitgaard@outlook.com",
            sender_email   = "bsacks1975@gmail.com",
            smtp_password  = "idmb wkut mmxf djig",
            anthropic_key  = os.environ.get("ANTHROPIC_API_KEY", ""),
            adzuna_app_id  = os.environ.get("ADZUNA_APP_ID", "aaa7540c"),
            adzuna_app_key = os.environ.get("ADZUNA_APP_KEY", "37ca0dd379156d7cbdda1ad8b283be37"),
            is_admin       = True,
            score_threshold= 20,
            max_jobs_to_score= 25,
        )
        john.set_password("JobHunter2026!")
        john.keywords  = [
            "GRC Security", "Security Architect", "Cyber Security GRC",
            "Security Analyst GRC", "Information Security Analyst",
            "Security Risk Advisor", "Cloud Security Architect", "Security Consultant",
        ]
        john.locations = [l["name"] for l in ALL_LOCATIONS]
        db.session.add(john)

        # Partner placeholder
        partner = User(
            username       = "partner",
            email          = "partner@example.com",
            full_name      = "Partner",
            recipient_email= "",
            sender_email   = "bsacks1975@gmail.com",
            smtp_password  = "idmb wkut mmxf djig",
            anthropic_key  = os.environ.get("ANTHROPIC_API_KEY", ""),
            is_admin       = False,
            score_threshold= 20,
            max_jobs_to_score= 25,
        )
        partner.set_password("JobHunter2026!")
        partner.keywords  = ["Data Scientist", "ML Engineer", "Data Analyst", "Analytics Engineer"]
        partner.locations = ["Wellington, NZ", "Auckland, NZ", "Remote"]
        db.session.add(partner)
        db.session.commit()
        log.info("Default users created — john / JobHunter2026! and partner / JobHunter2026!")

    # Seed built-in sources if not already present
    if JobSource.query.count() == 0:
        builtin_sources = [
            {"name": "Seek NZ/AU",  "color": "#0ea5e9", "rss_url": ""},
            {"name": "Adzuna",      "color": "#059669", "rss_url": ""},
            {"name": "LinkedIn",    "color": "#0a66c2", "rss_url": ""},
            {"name": "Finn.no",     "color": "#dc2626", "rss_url": "https://www.finn.no/rss/job/fulltime/result.rss?q={keyword}&occupation=20001"},
            {"name": "Jobindex",    "color": "#2563eb", "rss_url": "https://www.jobindex.dk/jobsoegning.rss?q={keyword}&lang=en"},
            {"name": "Jobicy",      "color": "#db2777", "rss_url": "https://jobicy.com/?feed=job_feed&job_categories=it-security&search_keywords={keyword}"},
            {"name": "Monster.com", "color": "#7c3aed", "rss_url": "https://www.monster.com/rss/search?q={keyword}&where={location}"},
            {"name": "Monster.ca",  "color": "#7c3aed", "rss_url": "https://www.monster.ca/rss/search?q={keyword}&where={location}"},
        ]
        for s in builtin_sources:
            js = JobSource(name=s["name"], rss_url=s["rss_url"],
                           color=s["color"], is_builtin=True,
                           is_active=(s["name"] not in ["Monster.com", "Monster.ca"]))
            db.session.add(js)
        db.session.commit()
        log.info("Default job sources seeded")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        # Migrate any missing columns for older DBs
        try:
            import sqlite3 as _sq, os as _os
            _db = _os.path.join(app.instance_path, "job_hunter.db")
            _c = _sq.connect(_db)
            for col, typedef in [
                ("work_arrangement",     "TEXT DEFAULT '[]'"),
                ("must_change_password", "INTEGER DEFAULT 0"),
                ("notification_pref",   "TEXT DEFAULT 'both'"),
                ("mfa_secret",          "TEXT DEFAULT ''"),
                ("mfa_enabled",         "INTEGER DEFAULT 0"),
                ("enable_job_alerts",   "INTEGER DEFAULT 1"),
                ("enable_weekly_summary","INTEGER DEFAULT 1"),
                ("dark_mode",           "INTEGER DEFAULT 0"),
                ("color_theme",         "TEXT DEFAULT 'default'"),
            ]:
                try: _c.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
                except: pass
            for col, typedef in [
                ("app_status",     "TEXT DEFAULT 'watching'"),
                ("app_notes",      "TEXT DEFAULT ''"),
                ("applied_date",   "DATETIME"),
                ("status_updated", "DATETIME"),
                ("interview_date", "DATETIME"),
                ("contact_name",   "TEXT DEFAULT ''"),
                ("contact_email",  "TEXT DEFAULT ''"),
            ]:
                try: _c.execute(f"ALTER TABLE job_results ADD COLUMN {col} {typedef}")
                except: pass
            _c.commit(); _c.close()
        except: pass
        create_default_users()
    app.run(debug=True, host="0.0.0.0", port=5001)
