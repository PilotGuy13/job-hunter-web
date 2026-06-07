"""
AI Job Hunter — Test Configuration
====================================
Shared config for both Full E2E and Regression test suites.

Usage:
  1. pip install requests pytest pytest-html
  2. Edit BASE_URL and credentials below
  3. Run:  pytest test_full_e2e.py -v --html=report_e2e.html
          pytest test_regression.py -v --html=report_regression.html
"""

import requests
import re
import time

# ── Target Environment ──────────────────────────────────────────────
# Switch between local and live by commenting/uncommenting
BASE_URL = "https://jobhunterweb.pythonanywhere.com"
# BASE_URL = "http://localhost:5001"

# ── Test Account (non-admin) ────────────────────────────────────────
TEST_USER = "bsacks1975@gmail.com"
TEST_PASS = "VhoSnaIJdaLbYd!@"  # <-- FILL IN before running

# ── Admin Account ───────────────────────────────────────────────────
ADMIN_USER = "johnbklitgaard@outlook.com"
ADMIN_PASS = "MyJ0bHunt3r2026@!@"  # <-- FILL IN before running

# ── Timeouts ────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 30  # seconds per request
SLOW_THRESHOLD = 5    # flag pages slower than this (seconds)

# ── All known routes (GET) ──────────────────────────────────────────
# These are tested for 200 OK and no 500 errors
PUBLIC_ROUTES = [
    "/login",
]

AUTHENTICATED_ROUTES = [
    "/",                   # dashboard
    "/profile",
    "/settings",
    "/results",            # all results
    "/tracker",            # application tracker
    "/weekly-summary",
    "/saved",              # saved jobs
]

ADMIN_ONLY_ROUTES = [
    "/admin",
    "/admin/usage",
]

API_ROUTES = [
    "/api/job-sources/countries",
]

# ── Profile Test Data ───────────────────────────────────────────────
# Used to verify profile edits save correctly
PROFILE_TEST_DATA = {
    "full_name": "Test Automation User",
    "recipient_email": "test-automation@example.com",
    "cv_summary": (
        "Key Capabilities:\n"
        "• Certification & Accreditation (C&A) – NZISM-aligned\n"
        "• GRC & Continuous Assurance\n"
        "• Security Risk Assessment & Threat Modelling\n"
        "• Secure by Design Advisory\n"
        "• Security Architecture Review\n"
        "• NZISM & PSR\n"
        "• Policy Development & Capability Uplift\n"
        "• Cloud Security (Azure / M365)\n"
        "• Supply Chain & Third-Party Risk"
    ),
    "keywords": "GRC Security\nSecurity Architect\nCyber Security GRC\nSecurity Analyst GRC",
    "notification_pref": "web",
    "work_arrangement": ["Remote", "Hybrid", "Onsite"],
}

# ── Filter Test Matrix ──────────────────────────────────────────────
# Filters to test on dashboard and all results pages
FILTER_OPTIONS = {
    "min_score": [0, 50, 70, 90],
    "sort": ["score_desc", "score_asc", "date_desc", "date_asc"],
}


# ── Helper: create a logged-in session ──────────────────────────────
def get_session(username=None, password=None):
    """Return a requests.Session logged in to the app."""
    s = requests.Session()
    s.headers.update({"User-Agent": "JobHunterTestHarness/1.0"})
    u = username or TEST_USER
    p = password or TEST_PASS
    if not p:
        raise ValueError(f"Password not set for {u} — edit test_config.py")
    r = s.post(
        f"{BASE_URL}/login",
        data={"username": u, "password": p},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    # After successful login, we should be redirected to dashboard
    if r.status_code >= 400:
        raise ConnectionError(f"Login failed for {u}: HTTP {r.status_code}")
    if "/login" in r.url and "error" in r.text.lower():
        raise ConnectionError(f"Login failed for {u}: still on login page")
    return s


def get_admin_session():
    """Return a requests.Session logged in as admin."""
    return get_session(ADMIN_USER, ADMIN_PASS)


def timed_get(session, url, **kwargs):
    """GET with timing info. Returns (response, elapsed_seconds)."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    start = time.time()
    r = session.get(url, **kwargs)
    elapsed = time.time() - start
    return r, elapsed


def extract_links(html, base_url):
    """Pull all href values from HTML, return as absolute URLs."""
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    links = set()
    for h in hrefs:
        if h.startswith("mailto:") or h.startswith("javascript:") or h.startswith("#"):
            continue
        if h.startswith("http"):
            links.add(h)
        elif h.startswith("/"):
            links.add(base_url + h)
    return links


def check_text_contrast(html):
    """
    Basic check for white-on-light-grey readability issues.
    Returns list of potential problems found.
    """
    issues = []
    # Check for white text with light backgrounds
    white_on_light = re.findall(
        r'color:\s*#?(?:fff|ffffff|white).*?background[^;]*:\s*#?(?:f[0-9a-f]{5}|e[0-9a-f]{5}|d[0-9a-f]{5}|light)',
        html, re.IGNORECASE | re.DOTALL
    )
    if white_on_light:
        issues.append(f"Potential white-on-light text found ({len(white_on_light)} instances)")
    return issues
