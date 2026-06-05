"""
Database models for Job Hunter Web
"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """User account with profile and settings."""
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80),  unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    last_login    = db.Column(db.DateTime)

    # Profile
    full_name     = db.Column(db.String(120), default="")
    cv_summary    = db.Column(db.Text, default="")
    recipient_email = db.Column(db.String(120), default="")

    # Search settings (stored as JSON strings)
    _keywords     = db.Column("keywords",  db.Text, default="[]")
    _locations    = db.Column("locations", db.Text, default="[]")

    # Email settings
    sender_email  = db.Column(db.String(120), default="")
    smtp_password = db.Column(db.String(256), default="")

    # API keys
    anthropic_key = db.Column(db.String(256), default="")
    adzuna_app_id = db.Column(db.String(64),  default="")
    adzuna_app_key= db.Column(db.String(64),  default="")

    # Work arrangement preference (stored as JSON list)
    # Options: ["Remote", "Hybrid", "Onsite"] or [] = no preference
    _work_arrangement = db.Column("work_arrangement", db.Text, default="[]")

    # Run settings
    score_threshold  = db.Column(db.Integer, default=20)
    max_jobs_to_score= db.Column(db.Integer, default=25)
    schedule_hour_utc= db.Column(db.Integer, default=21)  # 9am NZT
    is_active        = db.Column(db.Boolean, default=True)
    last_run         = db.Column(db.DateTime)
    must_change_password = db.Column(db.Boolean, default=False)  # force pw change on first login
    notification_pref   = db.Column(db.String(16), default="both")  # email | web | both

    # MFA
    mfa_secret          = db.Column(db.String(64),  default="")
    mfa_enabled         = db.Column(db.Boolean,     default=False)

    # Feature toggles
    enable_job_alerts   = db.Column(db.Boolean, default=True)   # instant email for excellent matches
    enable_weekly_summary = db.Column(db.Boolean, default=True) # weekly trends email

    # UI preferences
    dark_mode           = db.Column(db.Boolean, default=False)
    color_theme         = db.Column(db.String(32), default="default")  # default|ocean|forest|sunset|berry|slate
    selected_sources    = db.Column(db.Text, default="[]")  # JSON list of source IDs user has enabled

    # Subscription
    subscription_plan   = db.Column(db.String(16), default="free")  # free|lite|standard|pro|trial
    trial_end_date      = db.Column(db.DateTime)
    plan_activated_at   = db.Column(db.DateTime)
    mfa_dismissed       = db.Column(db.Boolean, default=False)   # user dismissed MFA prompt
    require_mfa         = db.Column(db.Boolean, default=False)   # admin: force MFA for this user
    mfa_grace_until     = db.Column(db.DateTime)                 # deadline to enable MFA
    last_run_status  = db.Column(db.String(200), default="Never run")

    # Relationships
    jobs = db.relationship("JobResult", backref="user", lazy=True,
                           cascade="all, delete-orphan")

    @property
    def work_arrangement(self):
        try:
            return json.loads(self._work_arrangement)
        except Exception:
            return []

    @work_arrangement.setter
    def work_arrangement(self, value):
        self._work_arrangement = json.dumps(value)

    @property
    def work_arrangement_label(self):
        arr = self.work_arrangement
        if not arr:
            return "No preference"
        return ", ".join(arr)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def keywords(self):
        try:
            return json.loads(self._keywords)
        except Exception:
            return []

    @keywords.setter
    def keywords(self, value):
        self._keywords = json.dumps(value)

    @property
    def locations(self):
        try:
            return json.loads(self._locations)
        except Exception:
            return []

    @locations.setter
    def locations(self, value):
        self._locations = json.dumps(value)


class JobResult(db.Model):
    """A scored job result for a user."""
    __tablename__ = "job_results"

    id                  = db.Column(db.Integer, primary_key=True)
    user_id             = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    found_at            = db.Column(db.DateTime, default=datetime.utcnow)

    # Job details
    title               = db.Column(db.String(256), default="")
    company             = db.Column(db.String(256), default="")
    location            = db.Column(db.String(256), default="")
    url                 = db.Column(db.String(1024), default="")
    source              = db.Column(db.String(64),  default="")
    listed              = db.Column(db.String(64),  default="")
    description         = db.Column(db.Text,        default="")
    search_location     = db.Column(db.String(128), default="")

    # Claude scoring
    compatibility_score = db.Column(db.Integer, default=0)
    compatibility_label = db.Column(db.String(64),  default="")
    apply_priority      = db.Column(db.String(16),  default="Medium")
    salary_estimate     = db.Column(db.String(128), default="")
    _match_reasons      = db.Column("match_reasons", db.Text, default="[]")
    _gaps               = db.Column("gaps",          db.Text, default="[]")
    _cv_tweaks          = db.Column("cv_tweaks",     db.Text, default="[]")
    hiring_manager_search = db.Column(db.String(512), default="")
    linkedin_search     = db.Column(db.String(512),  default="")

    # Status
    emailed             = db.Column(db.Boolean, default=False)
    saved               = db.Column(db.Boolean, default=False)   # user bookmarked
    dismissed           = db.Column(db.Boolean, default=False)  # user dismissed

    # Application tracking
    app_status          = db.Column(db.String(32), default="watching")  # watching|applied|interviewing|offered|rejected|withdrawn
    app_notes           = db.Column(db.Text, default="")
    applied_date        = db.Column(db.DateTime)
    status_updated      = db.Column(db.DateTime)
    interview_date      = db.Column(db.DateTime)
    contact_name        = db.Column(db.String(128), default="")
    contact_email       = db.Column(db.String(128), default="")

    @property
    def match_reasons(self):
        try:
            return json.loads(self._match_reasons)
        except Exception:
            return []

    @match_reasons.setter
    def match_reasons(self, value):
        self._match_reasons = json.dumps(value)

    @property
    def gaps(self):
        try:
            return json.loads(self._gaps)
        except Exception:
            return []

    @gaps.setter
    def gaps(self, value):
        self._gaps = json.dumps(value)

    @property
    def cv_tweaks(self):
        try:
            return json.loads(self._cv_tweaks)
        except Exception:
            return []

    @cv_tweaks.setter
    def cv_tweaks(self, value):
        self._cv_tweaks = json.dumps(value)


class UsageLog(db.Model):
    """Tracks API usage per user per day."""
    __tablename__ = "usage_logs"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date       = db.Column(db.Date,    nullable=False)
    jobs_searched = db.Column(db.Integer, default=0)
    jobs_scored   = db.Column(db.Integer, default=0)
    api_calls     = db.Column(db.Integer, default=0)
    est_cost_usd  = db.Column(db.Float,   default=0.0)
    emails_sent   = db.Column(db.Integer, default=0)


class JobSource(db.Model):
    """A job board source — built-in or custom RSS feed."""
    __tablename__ = "job_sources"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(64),   nullable=False)   # e.g. "Monster.com"
    source_type = db.Column(db.String(16),   default="rss")    # "rss" | "builtin"
    rss_url     = db.Column(db.String(512),  default="")       # URL with {keyword} and {location} placeholders
    is_active   = db.Column(db.Boolean,      default=True)
    is_builtin  = db.Column(db.Boolean,      default=False)    # built-in sources can be toggled but not deleted
    color       = db.Column(db.String(16),   default="#6b7280")
    created_at  = db.Column(db.DateTime,     default=datetime.utcnow)
    added_by    = db.Column(db.Integer,      db.ForeignKey("users.id"), nullable=True)

    def build_url(self, keyword, location=""):
        """Replace {keyword} and {location} placeholders in the RSS URL."""
        import urllib.parse
        url = self.rss_url
        url = url.replace("{keyword}",  urllib.parse.quote(keyword))
        url = url.replace("{location}", urllib.parse.quote(location))
        return url
