"""
AI Job Hunter — Regression Test Suite
========================================
Targets the highest-risk areas where code changes most often break things:
  - Page rendering (500 errors)
  - Profile save/load cycle
  - Settings integrity
  - Admin functions
  - Sign out
  - Filter stability
  - Base template rendering

This is the FAST suite — run after every deployment.

Run:
  pytest test_regression.py -v --html=report_regression.html --tb=short
"""

import pytest
import re
import requests
from test_config import (
    BASE_URL, TEST_USER, TEST_PASS, ADMIN_USER, ADMIN_PASS,
    AUTHENTICATED_ROUTES, ADMIN_ONLY_ROUTES,
    PROFILE_TEST_DATA, REQUEST_TIMEOUT,
    get_session, get_admin_session, timed_get,
)


# ════════════════════════════════════════════════════════════════════
#  FIXTURES
# ════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def user_session():
    return get_session(TEST_USER, TEST_PASS)

@pytest.fixture(scope="module")
def admin_session():
    return get_admin_session()


# ════════════════════════════════════════════════════════════════════
#  R1. CRITICAL: NO 500 ERRORS ON ANY PAGE
# ════════════════════════════════════════════════════════════════════

class TestNo500Errors:
    """THE most important regression check. Any 500 = deployment is broken."""

    @pytest.mark.parametrize("route", AUTHENTICATED_ROUTES + ["/login"])
    def test_no_500_user_routes(self, user_session, route):
        if route == "/login":
            r = requests.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
        else:
            r = user_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
        assert r.status_code != 500, f"🔴 500 ERROR on {route}"

    @pytest.mark.parametrize("route", ADMIN_ONLY_ROUTES)
    def test_no_500_admin_routes(self, admin_session, route):
        r = admin_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
        assert r.status_code != 500, f"🔴 500 ERROR on {route}"


# ════════════════════════════════════════════════════════════════════
#  R2. CRITICAL: SIGN OUT EXISTS AND WORKS (Bug #2)
# ════════════════════════════════════════════════════════════════════

class TestSignOutRegression:
    def test_signout_link_in_sidebar(self, user_session):
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        html_lower = r.text.lower()
        assert any(x in html_lower for x in ["sign out", "logout", "/logout"]), \
            "🔴 Bug #2: SIGN OUT MISSING from sidebar"

    def test_signout_ends_session(self):
        s = get_session(TEST_USER, TEST_PASS)
        # Try logout
        r = s.get(f"{BASE_URL}/logout", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        # Verify session ended
        r2 = s.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        assert "/login" in r2.url or 'name="username"' in r2.text, \
            "🔴 Logout didn't end session"


# ════════════════════════════════════════════════════════════════════
#  R3. PROFILE SAVE/LOAD CYCLE — most fragile area
# ════════════════════════════════════════════════════════════════════

class TestProfileRegression:
    def test_profile_loads_no_error(self, user_session):
        r = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200, f"Profile returned {r.status_code}"

    def test_profile_has_all_form_fields(self, user_session):
        """Verify all expected form fields exist after deployment."""
        r = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        required_fields = [
            'name="cv_summary"',
            'name="keywords"',
            'name="notification_pref"',
            'name="work_arrangement"',
            'name="full_name"',
        ]
        missing = [f for f in required_fields if f not in r.text]
        assert len(missing) == 0, f"Profile missing fields: {missing}"

    def test_profile_round_trip_save(self, user_session):
        """Save profile data, reload, verify it persisted."""
        test_marker = "REGRESSION_TEST_MARKER_12345"
        r = user_session.post(
            f"{BASE_URL}/profile",
            data={
                "full_name": PROFILE_TEST_DATA["full_name"],
                "recipient_email": PROFILE_TEST_DATA["recipient_email"],
                "cv_summary": test_marker,
                "keywords": "test keyword regression",
                "notification_pref": "web",
                "work_arrangement": ["Remote"],
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        assert r.status_code == 200, f"Profile POST returned {r.status_code}"

        # Reload
        r2 = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert test_marker in r2.text, \
            "🔴 Profile data did NOT persist after save"

        # Restore original
        user_session.post(
            f"{BASE_URL}/profile",
            data={
                "full_name": PROFILE_TEST_DATA["full_name"],
                "recipient_email": PROFILE_TEST_DATA["recipient_email"],
                "cv_summary": PROFILE_TEST_DATA["cv_summary"],
                "keywords": PROFILE_TEST_DATA["keywords"],
                "notification_pref": PROFILE_TEST_DATA["notification_pref"],
                "work_arrangement": PROFILE_TEST_DATA["work_arrangement"],
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

    def test_profile_work_arrangement_all_three(self, user_session):
        """Bug #16: All 3 work arrangements should be checkable and persist."""
        r = user_session.post(
            f"{BASE_URL}/profile",
            data={
                "cv_summary": PROFILE_TEST_DATA["cv_summary"],
                "keywords": PROFILE_TEST_DATA["keywords"],
                "notification_pref": "web",
                "work_arrangement": ["Remote", "Hybrid", "Onsite"],
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        assert r.status_code == 200

        r2 = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        for val in ["Remote", "Hybrid", "Onsite"]:
            assert re.search(rf'value="{val}"[^>]*checked', r2.text, re.IGNORECASE), \
                f"🔴 Bug #16: Work arrangement '{val}' not checked after save"

    def test_profile_job_sources_api(self, user_session):
        """Job sources API must respond with valid data."""
        r = user_session.get(f"{BASE_URL}/api/job-sources/countries", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert len(data.get("countries", [])) > 0, "Job sources countries API returned empty"


# ════════════════════════════════════════════════════════════════════
#  R4. SETTINGS INTEGRITY
# ════════════════════════════════════════════════════════════════════

class TestSettingsRegression:
    def test_settings_loads(self, user_session):
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200, f"Settings returned {r.status_code}"

    def test_settings_removed_features(self, user_session):
        """Bugs #8, #10, #11: Removed features should stay removed."""
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

        checks = {
            "Bug #10 color_theme": 'name="color_theme"',
            "Bug #11 schedule_time": 'name="scheduled_time"',
            "Bug #8 max_results_per_run": 'name="max_results_per_run"',
        }
        for bug, pattern in checks.items():
            if pattern in r.text:
                pytest.fail(f"🔴 {bug}: Field still present in settings")

    def test_settings_password_current_required(self, user_session):
        """Bug #12: Must require current password."""
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        has_current = (
            'current_password' in r.text or
            'old_password' in r.text or
            'current password' in r.text.lower()
        )
        assert has_current, "🔴 Bug #12: No current password field"


# ════════════════════════════════════════════════════════════════════
#  R5. BASE TEMPLATE — SIDEBAR & GLOBAL ELEMENTS
# ════════════════════════════════════════════════════════════════════

class TestBaseTemplateRegression:
    def test_sidebar_has_nav_links(self, user_session):
        """Sidebar should contain all major navigation links."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        expected_links = ["/profile", "/settings", "/results", "/tracker"]
        html = r.text
        missing = [link for link in expected_links if f'href="{link}"' not in html and f"href='{link}'" not in html]
        # Some links may use url_for which produces the same path
        missing2 = [link for link in missing if link not in html]
        assert len(missing2) == 0, f"Sidebar missing nav links: {missing2}"

    def test_sidebar_brand(self, user_session):
        """Sidebar shows app name and user name."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert "AI Job Hunter" in r.text, "App name missing from sidebar"

    def test_dark_mode_toggle_exists(self, user_session):
        """Dark mode toggle should be present."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert "toggleDarkMode" in r.text or "dark-mode" in r.text.lower() or "dark_mode" in r.text.lower(), \
            "Dark mode toggle missing"

    def test_mobile_topbar_exists(self, user_session):
        """Mobile hamburger menu should be present."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert "mobile-topbar" in r.text, "Mobile topbar missing"
        assert "toggleSidebar" in r.text, "Sidebar toggle function missing"

    def test_run_banner_present(self, user_session):
        """Bug #6: Global run banner element must exist."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert "globalRunBanner" in r.text, "🔴 Bug #6: Run progress banner missing"

    def test_run_status_polling(self, user_session):
        """Run status endpoint responds for background polling."""
        r = user_session.get(f"{BASE_URL}/run/status?since=0", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "running" in data


# ════════════════════════════════════════════════════════════════════
#  R6. FILTER STABILITY — Dashboard & All Results
# ════════════════════════════════════════════════════════════════════

class TestFilterRegression:
    """Filters are a common source of 500 errors when model changes break query params."""

    @pytest.mark.parametrize("page", ["/", "/results"])
    def test_filter_min_score_0(self, user_session, page):
        r = user_session.get(f"{BASE_URL}{page}?min_score=0", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200, f"Filter min_score=0 on {page} broke"

    @pytest.mark.parametrize("page", ["/", "/results"])
    def test_filter_min_score_100(self, user_session, page):
        r = user_session.get(f"{BASE_URL}{page}?min_score=100", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200, f"Filter min_score=100 on {page} broke"

    @pytest.mark.parametrize("page", ["/", "/results"])
    def test_filter_sort_score_desc(self, user_session, page):
        r = user_session.get(f"{BASE_URL}{page}?sort=score_desc", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200, f"Sort score_desc on {page} broke"

    @pytest.mark.parametrize("page", ["/", "/results"])
    def test_filter_invalid_param(self, user_session, page):
        """Invalid filter params should not cause 500."""
        r = user_session.get(
            f"{BASE_URL}{page}?min_score=abc&sort=invalid",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code != 500, f"Invalid filter params caused 500 on {page}"

    @pytest.mark.parametrize("page", ["/", "/results"])
    def test_filter_combined(self, user_session, page):
        r = user_session.get(
            f"{BASE_URL}{page}?min_score=50&sort=date_desc&source=adzuna",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200, f"Combined filters on {page} broke"


# ════════════════════════════════════════════════════════════════════
#  R7. ADMIN REGRESSION
# ════════════════════════════════════════════════════════════════════

class TestAdminRegression:
    def test_admin_loads(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/admin", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    def test_admin_has_delete_user(self, admin_session):
        """Bug #14: Admin must have user deletion."""
        r = admin_session.get(f"{BASE_URL}/admin", timeout=REQUEST_TIMEOUT)
        assert "delete" in r.text.lower(), "🔴 Bug #14: No delete user option"

    def test_admin_has_password_reset(self, admin_session):
        """Bug #14: Admin must have password reset for users."""
        r = admin_session.get(f"{BASE_URL}/admin", timeout=REQUEST_TIMEOUT)
        has_reset = "reset" in r.text.lower() and "password" in r.text.lower()
        assert has_reset, "🔴 Bug #14: No password reset option"

    def test_admin_blocked_for_normal_user(self, user_session):
        """Normal user must NOT access admin."""
        r = user_session.get(f"{BASE_URL}/admin", timeout=REQUEST_TIMEOUT, allow_redirects=False)
        assert r.status_code in (302, 303, 403), \
            f"Non-admin got {r.status_code} on /admin (expected redirect/403)"

    def test_usage_dashboard_loads(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/admin/usage", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200


# ════════════════════════════════════════════════════════════════════
#  R8. APPLICATION TRACKER REGRESSION
# ════════════════════════════════════════════════════════════════════

class TestTrackerRegression:
    def test_tracker_loads(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/tracker", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    def test_tracker_has_pipeline_columns(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/tracker", timeout=REQUEST_TIMEOUT)
        html_lower = r.text.lower()
        assert "watching" in html_lower or "pipeline" in html_lower, \
            "Tracker missing pipeline columns"

    def test_tracker_not_too_wide(self, admin_session):
        """Bug #5: Check for fixed widths that prevent responsive resize."""
        r = admin_session.get(f"{BASE_URL}/tracker", timeout=REQUEST_TIMEOUT)
        # Flag extremely large fixed widths
        wide_matches = re.findall(r'min-width:\s*(\d+)px', r.text)
        oversized = [int(w) for w in wide_matches if int(w) > 1400]
        assert len(oversized) == 0, \
            f"Bug #5: Tracker has oversized min-width: {oversized}px"
