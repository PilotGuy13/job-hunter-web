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

from models import JobResult, User, db
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

# Track running jobs per user (user_id -> thread)
_running = {}


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user     = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user, remember=True)
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


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
        current_user.sender_email    = request.form.get("sender_email", "")

        # Only update password if provided
        new_smtp = request.form.get("smtp_password", "")
        if new_smtp.strip():
            current_user.smtp_password = new_smtp.strip()

        current_user.anthropic_key   = request.form.get("anthropic_key", "")
        current_user.adzuna_app_id   = request.form.get("adzuna_app_id", "")
        current_user.adzuna_app_key  = request.form.get("adzuna_app_key", "")

        # Keywords — one per line
        kws = [k.strip() for k in request.form.get("keywords","").splitlines() if k.strip()]
        current_user.keywords = kws

        # Locations — checkboxes
        selected_locs = request.form.getlist("locations")
        current_user.locations = selected_locs

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

            db.session.commit()
            flash("Settings saved.", "success")
        except ValueError:
            flash("Invalid value — please enter numbers only.", "error")
        return redirect(url_for("settings"))
    return render_template("settings.html")


# ── Run Now (streaming) ───────────────────────────────────────────────────────

@app.route("/run")
@login_required
def run_now():
    """Trigger a job search run and stream progress to the browser."""
    if current_user.id in _running and _running[current_user.id].is_alive():
        return jsonify({"error": "Already running"}), 409

    def generate():
        def progress(msg):
            yield f"data: {msg}\n\n"

        # Get seen fingerprints for this user
        with app.app_context():
            user = User.query.get(current_user.id)
            seen = {r.url for r in JobResult.query.filter_by(user_id=user.id).all() if r.url}

            yield "data: Starting job search...\n\n"
            try:
                result = run_for_user(user, seen, progress_callback=None)

                # Save results to DB
                for job in result.get("relevant_jobs", []):
                    jr = JobResult(
                        user_id             = user.id,
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

                user.last_run        = datetime.utcnow()
                user.last_run_status = (f"Found {result['relevant_jobs'].__len__()} relevant jobs "
                                        f"({'emailed' if result['emailed'] else 'not emailed'})")
                db.session.commit()

                yield f"data: ✅ Done — {result['relevant']} relevant jobs found.\n\n"
                yield "data: DONE\n\n"
            except Exception as e:
                yield f"data: ❌ Error: {e}\n\n"
                yield "data: DONE\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ── Results / Job actions ──────────────────────────────────────────────────────

@app.route("/results")
@login_required
def results():
    page     = request.args.get("page", 1, type=int)
    min_score= request.args.get("min_score", 0, type=int)
    source   = request.args.get("source", "")
    location = request.args.get("location", "")

    query = (JobResult.query
             .filter_by(user_id=current_user.id, dismissed=False)
             .filter(JobResult.compatibility_score >= min_score))
    if source:
        query = query.filter_by(source=source)
    if location:
        query = query.filter_by(search_location=location)

    jobs = query.order_by(JobResult.found_at.desc()).paginate(page=page, per_page=20)
    sources   = db.session.query(JobResult.source).filter_by(user_id=current_user.id).distinct().all()
    locations = db.session.query(JobResult.search_location).filter_by(user_id=current_user.id).distinct().all()
    return render_template("results.html", jobs=jobs, sources=sources, locations=locations,
                           min_score=min_score, source=source, location=location)


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


# ── Preview last digest ───────────────────────────────────────────────────────

@app.route("/preview")
@login_required
def preview():
    jobs = (JobResult.query
            .filter_by(user_id=current_user.id)
            .order_by(JobResult.found_at.desc())
            .limit(25).all())
    if not jobs:
        return "<p style='font-family:sans-serif;padding:40px;'>No jobs found yet. Run a search first.</p>"

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
            sender_email   = "jobhunterget@gmail.com",
            smtp_password  = "zlxy vcqv awim bsfx",
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
            sender_email   = "jobhunterget@gmail.com",
            smtp_password  = "zlxy vcqv awim bsfx",
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


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        create_default_users()
    app.run(debug=True, host="0.0.0.0", port=5000)
