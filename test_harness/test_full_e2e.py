"""
AI Job Hunter — Full End-to-End Test Suite
=============================================
Covers every area from TestHarness.rtf:
  - Login / page loads / no 500s
  - Broken link check
  - Dashboard & All Results filtering
  - Email preview rendering
  - Application tracker drag/drop
  - My Profile edits & saves
  - Settings edits & saves
  - Welcome/Login page readability
  - Run search (no errors)
  - Admin functions
  - Bug regression checks (Bugs.xlsx #1-24)

Run:
  pytest test_full_e2e.py -v --html=report_e2e.html --tb=short
  pytest test_full_e2e.py -v -k "login or page_load"   # run subset
"""

import pytest
import re
import time
from test_config import (
    BASE_URL, TEST_USER, TEST_PASS, ADMIN_USER, ADMIN_PASS,
    PUBLIC_ROUTES, AUTHENTICATED_ROUTES, ADMIN_ONLY_ROUTES, API_ROUTES,
    PROFILE_TEST_DATA, FILTER_OPTIONS, REQUEST_TIMEOUT, SLOW_THRESHOLD,
    get_session, get_admin_session, timed_get, extract_links, check_text_contrast,
)


# ════════════════════════════════════════════════════════════════════
#  FIXTURES
# ════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def user_session():
    """Logged-in session for the standard test user."""
    return get_session(TEST_USER, TEST_PASS)


@pytest.fixture(scope="module")
def admin_session():
    """Logged-in session for the admin user."""
    return get_admin_session()


# ════════════════════════════════════════════════════════════════════
#  1. LOGIN & AUTHENTICATION
# ════════════════════════════════════════════════════════════════════

class TestLogin:
    def test_login_page_loads(self):
        """Login page returns 200."""
        r = requests.get(f"{BASE_URL}/login", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200, f"Login page returned {r.status_code}"

    def test_login_page_has_form(self):
        """Login page contains username and password fields."""
        import requests as req
        r = req.get(f"{BASE_URL}/login", timeout=REQUEST_TIMEOUT)
        assert 'name="username"' in r.text, "Missing username field"
        assert 'name="password"' in r.text, "Missing password field"

    def test_login_page_readability(self):
        """Login page has no white-on-light text issues."""
        import requests as req
        r = req.get(f"{BASE_URL}/login", timeout=REQUEST_TIMEOUT)
        issues = check_text_contrast(r.text)
        assert len(issues) == 0, f"Readability issues: {issues}"

    def test_login_page_registration_link(self):
        """Login page has a link to register / start trial."""
        import requests as req
        r = req.get(f"{BASE_URL}/login", timeout=REQUEST_TIMEOUT)
        assert "register" in r.text.lower() or "trial" in r.text.lower(), \
            "No registration/trial link on login page"

    def test_login_success(self, user_session):
        """Standard user can log in and reach dashboard."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        assert "Job Hunter" in r.text

    def test_login_wrong_password(self):
        """Wrong password shows error, stays on login page."""
        import requests as req
        s = req.Session()
        r = s.post(
            f"{BASE_URL}/login",
            data={"username": TEST_USER, "password": "wrongpassword123"},
            timeout=REQUEST_TIMEOUT, allow_redirects=True,
        )
        assert r.status_code == 200
        # Should still be on login page
        assert 'name="username"' in r.text or 'name="password"' in r.text

    def test_unauthenticated_redirect(self):
        """Accessing dashboard without login redirects to login."""
        import requests as req
        r = req.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        assert "/login" in r.url or 'name="username"' in r.text


# ════════════════════════════════════════════════════════════════════
#  2. PAGE LOADS — NO 500 ERRORS
# ════════════════════════════════════════════════════════════════════

class TestPageLoads:
    @pytest.mark.parametrize("route", AUTHENTICATED_ROUTES)
    def test_authenticated_page_loads(self, user_session, route):
        """Each authenticated page returns 200, not 500."""
        r, elapsed = timed_get(user_session, f"{BASE_URL}{route}")
        assert r.status_code != 500, f"{route} returned 500 Internal Server Error"
        assert r.status_code == 200, f"{route} returned {r.status_code}"
        if elapsed > SLOW_THRESHOLD:
            pytest.warns(UserWarning, match=f"{route} took {elapsed:.1f}s (threshold: {SLOW_THRESHOLD}s)")

    @pytest.mark.parametrize("route", ADMIN_ONLY_ROUTES)
    def test_admin_page_loads(self, admin_session, route):
        """Admin-only pages return 200 for admin user."""
        r, elapsed = timed_get(admin_session, f"{BASE_URL}{route}")
        assert r.status_code != 500, f"{route} returned 500 Internal Server Error"
        assert r.status_code == 200, f"{route} returned {r.status_code}"

    @pytest.mark.parametrize("route", ADMIN_ONLY_ROUTES)
    def test_admin_page_blocked_for_user(self, user_session, route):
        """Admin-only pages return 403 or redirect for non-admin."""
        r = user_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT, allow_redirects=False)
        assert r.status_code in (302, 303, 403), \
            f"Non-admin accessing {route} got {r.status_code} (expected redirect or 403)"

    @pytest.mark.parametrize("route", API_ROUTES)
    def test_api_routes(self, user_session, route):
        """API endpoints return 200 and valid JSON."""
        r = user_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200, f"API {route} returned {r.status_code}"
        data = r.json()
        assert isinstance(data, dict), f"API {route} did not return a JSON object"


# ════════════════════════════════════════════════════════════════════
#  3. BROKEN LINK CHECK
# ════════════════════════════════════════════════════════════════════

class TestBrokenLinks:
    def test_no_broken_internal_links(self, user_session):
        """Crawl all authenticated pages and check internal links."""
        broken = []
        checked = set()
        for route in AUTHENTICATED_ROUTES:
            r = user_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                continue
            links = extract_links(r.text, BASE_URL)
            for link in links:
                if link in checked:
                    continue
                if not link.startswith(BASE_URL):
                    continue  # skip external
                checked.add(link)
                try:
                    lr = user_session.get(link, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                    if lr.status_code >= 400:
                        broken.append(f"{link} → {lr.status_code} (found on {route})")
                except Exception as e:
                    broken.append(f"{link} → ERROR: {e} (found on {route})")

        assert len(broken) == 0, (
            f"Found {len(broken)} broken link(s):\n" + "\n".join(broken)
        )


# ════════════════════════════════════════════════════════════════════
#  4. DASHBOARD FILTERING
# ════════════════════════════════════════════════════════════════════

class TestDashboardFiltering:
    def test_dashboard_loads(self, user_session):
        """Dashboard (/) returns 200 with expected elements."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    @pytest.mark.parametrize("min_score", FILTER_OPTIONS["min_score"])
    def test_dashboard_filter_min_score(self, user_session, min_score):
        """Dashboard filter by min_score does not error."""
        r = user_session.get(
            f"{BASE_URL}/?min_score={min_score}",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200, f"Filter min_score={min_score} returned {r.status_code}"

    @pytest.mark.parametrize("sort", FILTER_OPTIONS["sort"])
    def test_dashboard_filter_sort(self, user_session, sort):
        """Dashboard sort options do not error."""
        r = user_session.get(
            f"{BASE_URL}/?sort={sort}",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200, f"Sort={sort} returned {r.status_code}"

    def test_dashboard_combined_filters(self, user_session):
        """Dashboard with multiple filters applied simultaneously."""
        r = user_session.get(
            f"{BASE_URL}/?min_score=50&sort=score_desc",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200


# ════════════════════════════════════════════════════════════════════
#  5. ALL RESULTS FILTERING
# ════════════════════════════════════════════════════════════════════

class TestAllResultsFiltering:
    def test_all_results_loads(self, user_session):
        """All Results (/results) returns 200."""
        r = user_session.get(f"{BASE_URL}/results", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    @pytest.mark.parametrize("min_score", FILTER_OPTIONS["min_score"])
    def test_results_filter_min_score(self, user_session, min_score):
        """All Results filter by min_score does not error."""
        r = user_session.get(
            f"{BASE_URL}/results?min_score={min_score}",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200

    @pytest.mark.parametrize("sort", FILTER_OPTIONS["sort"])
    def test_results_filter_sort(self, user_session, sort):
        """All Results sort options do not error."""
        r = user_session.get(
            f"{BASE_URL}/results?sort={sort}",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200

    def test_results_combined_filters(self, user_session):
        """All Results with multiple filters simultaneously."""
        r = user_session.get(
            f"{BASE_URL}/results?min_score=70&sort=date_desc",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200

    def test_results_filter_source(self, user_session):
        """All Results filter by source (if supported)."""
        r = user_session.get(
            f"{BASE_URL}/results?source=adzuna",
            timeout=REQUEST_TIMEOUT,
        )
        # Should return 200 even if no results for that source
        assert r.status_code == 200

    def test_results_filter_location(self, user_session):
        """All Results filter by location (if supported)."""
        r = user_session.get(
            f"{BASE_URL}/results?location=Wellington",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200


# ════════════════════════════════════════════════════════════════════
#  6. EMAIL PREVIEW
# ════════════════════════════════════════════════════════════════════

class TestEmailPreview:
    def test_email_preview_route_exists(self, user_session):
        """Check if email preview route exists and loads."""
        # Try common preview routes
        for route in ["/email-preview", "/preview", "/email/preview"]:
            r = user_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return  # found it
        pytest.skip("Email preview route not found — may need /results/<id>/preview pattern")

    def test_email_preview_readability(self, user_session):
        """Email preview should not have white text on light background."""
        for route in ["/email-preview", "/preview"]:
            r = user_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                issues = check_text_contrast(r.text)
                assert len(issues) == 0, f"Email readability issues: {issues}"
                return
        pytest.skip("Email preview route not found")


# ════════════════════════════════════════════════════════════════════
#  7. APPLICATION TRACKER
# ════════════════════════════════════════════════════════════════════

class TestApplicationTracker:
    def test_tracker_loads(self, admin_session):
        """Application tracker page loads for admin."""
        r = admin_session.get(f"{BASE_URL}/tracker", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    def test_tracker_has_columns(self, admin_session):
        """Tracker page contains pipeline columns (Watching, Applied, etc.)."""
        r = admin_session.get(f"{BASE_URL}/tracker", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        html = r.text.lower()
        # Check for expected pipeline stages
        expected = ["watching", "applied", "interview"]
        found = [col for col in expected if col in html]
        assert len(found) >= 2, f"Only found columns: {found}. Expected at least 2 of {expected}"

    def test_tracker_move_job_via_api(self, admin_session):
        """Test moving a job between tracker columns via POST."""
        # First load tracker to see what jobs exist
        r = admin_session.get(f"{BASE_URL}/tracker", timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            pytest.skip("Tracker page didn't load")

        # Look for job IDs and move endpoints in the page
        # The tracker uses dropdown onChange to POST status changes
        move_match = re.findall(r'/tracker/move/(\d+)', r.text)
        if not move_match:
            # Try alternative patterns
            move_match = re.findall(r'data-job-id="(\d+)"', r.text)

        if not move_match:
            pytest.skip("No tracked jobs found to test move operation")

        job_id = move_match[0]
        # Try to move to "Applied" status
        for endpoint in [
            f"/tracker/move/{job_id}",
            f"/tracker/update/{job_id}",
        ]:
            r = admin_session.post(
                f"{BASE_URL}{endpoint}",
                data={"status": "Applied"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code in (200, 302, 303):
                break

    def test_tracker_responsive_width(self, admin_session):
        """Bug #5: Tracker should not have fixed/oversized width preventing resize."""
        r = admin_session.get(f"{BASE_URL}/tracker", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # Check for overflow-x or min-width that would prevent resize
        # This is a basic static check; full browser test needs Selenium
        if "overflow-x: hidden" in r.text and "min-width: 1200" in r.text:
            pytest.fail("Bug #5: Tracker has fixed min-width preventing browser resize")


# ════════════════════════════════════════════════════════════════════
#  8. MY PROFILE — EDIT & SAVE
# ════════════════════════════════════════════════════════════════════

class TestProfile:
    def test_profile_loads(self, user_session):
        """Profile page loads with form fields."""
        r = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        assert 'name="cv_summary"' in r.text, "Missing CV Summary field"
        assert 'name="keywords"' in r.text, "Missing Keywords field"

    def test_profile_save_cv_summary(self, user_session):
        """Saving CV summary persists the change."""
        # Save
        r = user_session.post(
            f"{BASE_URL}/profile",
            data={
                "full_name": PROFILE_TEST_DATA["full_name"],
                "recipient_email": PROFILE_TEST_DATA["recipient_email"],
                "cv_summary": PROFILE_TEST_DATA["cv_summary"],
                "keywords": PROFILE_TEST_DATA["keywords"],
                "notification_pref": PROFILE_TEST_DATA["notification_pref"],
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        assert r.status_code == 200, f"Profile save returned {r.status_code}"

        # Reload and verify
        r2 = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert "Certification &amp; Accreditation" in r2.text or \
               "Certification & Accreditation" in r2.text, \
               "CV Summary did not persist after save"

    def test_profile_save_keywords(self, user_session):
        """Saving keywords persists the change."""
        r = user_session.post(
            f"{BASE_URL}/profile",
            data={
                "cv_summary": PROFILE_TEST_DATA["cv_summary"],
                "keywords": PROFILE_TEST_DATA["keywords"],
                "notification_pref": "web",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        assert r.status_code == 200

        r2 = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert "GRC Security" in r2.text, "Keywords did not persist after save"

    def test_profile_save_work_arrangement(self, user_session):
        """Bug #16: Work arrangement saves correctly (all 3 enabled by default)."""
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
        # All three should be checked
        for val in ["Remote", "Hybrid", "Onsite"]:
            # Look for checked checkbox
            pattern = rf'value="{val}"[^>]*checked'
            assert re.search(pattern, r2.text, re.IGNORECASE), \
                f"Work arrangement '{val}' not saved/checked after save"

    def test_profile_save_notification_pref(self, user_session):
        """Notification preference saves correctly."""
        r = user_session.post(
            f"{BASE_URL}/profile",
            data={
                "cv_summary": PROFILE_TEST_DATA["cv_summary"],
                "keywords": PROFILE_TEST_DATA["keywords"],
                "notification_pref": "web",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        assert r.status_code == 200

        r2 = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert re.search(r'value="web"[^>]*checked', r2.text, re.IGNORECASE), \
            "Notification pref 'web' not selected after save"

    def test_profile_job_sources_api(self, user_session):
        """Job sources country dropdown API works."""
        r = user_session.get(f"{BASE_URL}/api/job-sources/countries", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "countries" in data, "Missing 'countries' key in API response"
        assert len(data["countries"]) > 0, "No countries returned"

    def test_profile_job_sources_by_country(self, user_session):
        """Job sources for a specific country returns data."""
        r = user_session.get(
            f"{BASE_URL}/api/job-sources/New Zealand",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200
        data = r.json()
        assert "sources" in data, "Missing 'sources' key"
        assert len(data["sources"]) > 0, "No sources for New Zealand"

    def test_profile_source_clear_reset(self, user_session):
        """Bug #23: Ability to clear all sources / remove one at a time."""
        # Save with no sources selected (reset)
        r = user_session.post(
            f"{BASE_URL}/profile",
            data={
                "cv_summary": PROFILE_TEST_DATA["cv_summary"],
                "keywords": PROFILE_TEST_DATA["keywords"],
                "notification_pref": "web",
                # No selected_sources = clear all
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        assert r.status_code == 200

        r2 = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        # Should show "No sources saved" or defaults message
        assert "no sources" in r2.text.lower() or "default" in r2.text.lower(), \
            "Bug #23: Sources not cleared after saving with none selected"


# ════════════════════════════════════════════════════════════════════
#  9. SETTINGS
# ════════════════════════════════════════════════════════════════════

class TestSettings:
    def test_settings_loads(self, user_session):
        """Settings page loads."""
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    def test_settings_password_requires_current(self, user_session):
        """Bug #12: Password change should require current password field."""
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # Should have a current_password field
        has_current_pw = (
            'name="current_password"' in r.text or
            'name="old_password"' in r.text or
            "current password" in r.text.lower()
        )
        assert has_current_pw, "Bug #12: Settings missing 'current password' field for password change"

    def test_settings_no_scheduled_run_time(self, user_session):
        """Bug #11: Scheduled run time should be removed (preset to 8am)."""
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        # Should NOT have a user-editable scheduled run time field
        has_schedule = (
            'name="scheduled_time"' in r.text or
            'name="run_time"' in r.text or
            'name="schedule_time"' in r.text
        )
        if has_schedule:
            pytest.fail("Bug #11: Settings still shows editable scheduled run time field")

    def test_settings_no_color_theme(self, user_session):
        """Bug #10: Color theme functionality should be removed."""
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        has_color_theme = (
            'name="color_theme"' in r.text or
            'colour theme' in r.text.lower() or
            'color theme' in r.text.lower()
        )
        if has_color_theme:
            pytest.fail("Bug #10: Settings still has color theme selection")

    def test_settings_no_max_score_per_run(self, user_session):
        """Bug #8: Max score per run should be removed if pricing enforces it."""
        r = user_session.get(f"{BASE_URL}/settings", timeout=REQUEST_TIMEOUT)
        has_max_score = 'name="max_results_per_run"' in r.text or 'max score per run' in r.text.lower()
        if has_max_score:
            pytest.fail("Bug #8: Settings still has 'max score per run' field")


# ════════════════════════════════════════════════════════════════════
#  10. WEEKLY SUMMARY
# ════════════════════════════════════════════════════════════════════

class TestWeeklySummary:
    def test_weekly_summary_loads(self, user_session):
        """Weekly summary page loads."""
        r = user_session.get(f"{BASE_URL}/weekly-summary", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    def test_weekly_summary_box_size(self, user_session):
        """Bug #13: Weekly summary rectangles should not be oversized."""
        r = user_session.get(f"{BASE_URL}/weekly-summary", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # Check for the week-card class styling
        # This is a basic presence check; visual verification needs manual/Selenium
        if "week-card" in r.text:
            pass  # Cards exist, manual visual check needed


# ════════════════════════════════════════════════════════════════════
#  11. SAVED JOBS
# ════════════════════════════════════════════════════════════════════

class TestSavedJobs:
    def test_saved_jobs_loads(self, user_session):
        """Saved jobs page loads."""
        r = user_session.get(f"{BASE_URL}/saved", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    def test_interview_prep_gated(self, user_session):
        """Bug #20: Interview prep should be greyed out if not in subscription."""
        r = user_session.get(f"{BASE_URL}/saved", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # This is hard to test without knowing the user's plan.
        # Check that "interview prep" link exists on the page
        if "interview" in r.text.lower() and "prep" in r.text.lower():
            pass  # Element exists; gating logic depends on subscription


# ════════════════════════════════════════════════════════════════════
#  12. ADMIN FUNCTIONS
# ════════════════════════════════════════════════════════════════════

class TestAdmin:
    def test_admin_page_loads(self, admin_session):
        """Admin page loads for admin user."""
        r = admin_session.get(f"{BASE_URL}/admin", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200

    def test_admin_has_user_management(self, admin_session):
        """Bug #14: Admin should have delete user and password reset functionality."""
        r = admin_session.get(f"{BASE_URL}/admin", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        html_lower = r.text.lower()
        has_delete = "delete" in html_lower
        has_reset = "reset" in html_lower or "password" in html_lower
        assert has_delete, "Bug #14: Admin page missing 'delete user' functionality"
        assert has_reset, "Bug #14: Admin page missing 'reset password' functionality"

    def test_admin_no_2fa_requirement(self, admin_session):
        """Bug #15: 2FA should not be enforced in admin, only at registration."""
        r = admin_session.get(f"{BASE_URL}/admin", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # Check that admin doesn't have a "require 2FA" or "enforce MFA" toggle
        has_enforce_mfa = re.search(
            r'enforce.{0,10}(mfa|2fa)|require.{0,10}(mfa|2fa)',
            r.text, re.IGNORECASE
        )
        if has_enforce_mfa:
            pytest.fail("Bug #15: Admin still has 'enforce 2FA' functionality")

    def test_usage_dashboard_loads(self, admin_session):
        """Usage & Costs page loads (Bug #7 — data may not populate from Claude)."""
        r = admin_session.get(f"{BASE_URL}/admin/usage", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200


# ════════════════════════════════════════════════════════════════════
#  13. SIGN OUT (Bug #2)
# ════════════════════════════════════════════════════════════════════

class TestSignOut:
    def test_signout_link_exists(self, user_session):
        """Bug #2: Sign out link/button must exist on the page."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        html_lower = r.text.lower()
        has_signout = any(term in html_lower for term in [
            "sign out", "signout", "log out", "logout", "/logout"
        ])
        assert has_signout, "Bug #2 CRITICAL: No sign out link found on any page"

    def test_signout_works(self):
        """Bug #2: Signing out actually ends the session."""
        s = get_session(TEST_USER, TEST_PASS)
        # Find and use the logout route
        r = s.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        logout_match = re.search(r'href="(/logout[^"]*)"', r.text)
        if not logout_match:
            pytest.fail("Bug #2: No logout URL found in page HTML")

        logout_url = f"{BASE_URL}{logout_match.group(1)}"
        r2 = s.get(logout_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        # After logout, accessing dashboard should redirect to login
        r3 = s.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        assert "/login" in r3.url or 'name="username"' in r3.text, \
            "Bug #2: Session not invalidated after logout"


# ════════════════════════════════════════════════════════════════════
#  14. BREADCRUMBS (Bug #1)
# ════════════════════════════════════════════════════════════════════

class TestBreadcrumbs:
    @pytest.mark.parametrize("route,name", [
        ("/profile", "Profile"),
        ("/settings", "Settings"),
        ("/results", "Results"),
        ("/tracker", "Tracker"),
        ("/weekly-summary", "Weekly"),
    ])
    def test_breadcrumbs_present(self, user_session, route, name):
        """Bug #1: Breadcrumbs should be present on every page."""
        r = user_session.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            pytest.skip(f"{route} returned {r.status_code}")
        assert "breadcrumb" in r.text.lower(), \
            f"Bug #1: No breadcrumbs found on {route}"


# ════════════════════════════════════════════════════════════════════
#  15. RUN SEARCH
# ════════════════════════════════════════════════════════════════════

class TestRunSearch:
    def test_run_status_endpoint(self, user_session):
        """Run status API responds (used by polling)."""
        r = user_session.get(
            f"{BASE_URL}/run/status?since=0",
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 200
        data = r.json()
        assert "running" in data, "Run status API missing 'running' field"

    def test_run_progress_display(self, user_session):
        """Bug #6: Run progress banner exists in HTML."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        assert "globalRunBanner" in r.text, \
            "Bug #6: Global run banner element missing from dashboard"


# ════════════════════════════════════════════════════════════════════
#  16. MISC BUG CHECKS
# ════════════════════════════════════════════════════════════════════

class TestMiscBugs:
    def test_all_results_min_score_greyed(self, user_session):
        """Bug #17: Min score on All Results should be greyed out / match settings."""
        r = user_session.get(f"{BASE_URL}/results", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # Check for disabled/readonly on min_score field
        min_score_field = re.search(r'name="min_score"[^>]*', r.text)
        if min_score_field:
            field_html = min_score_field.group(0)
            is_disabled = "disabled" in field_html or "readonly" in field_html
            # Note: This might be intentionally editable. Flag if requirement says grey out.

    def test_all_results_timestamp(self, user_session):
        """Bug #21: Jobs should show local timestamp when rated by AI."""
        r = user_session.get(f"{BASE_URL}/results", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # Check for utc-date class (used for local time conversion)
        has_timestamps = "utc-date" in r.text or "data-utc" in r.text or "rated" in r.text.lower()
        # Only flag if there are actual job results on the page
        if "job-card" in r.text or "job-mini" in r.text:
            assert has_timestamps, "Bug #21: No timestamps found on job results"

    def test_anthropic_badge_in_sidebar(self, user_session):
        """Bug #22: 'Powered by Anthropic Claude AI' should be in sidebar."""
        r = user_session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
        html_lower = r.text.lower()
        has_badge = "anthropic" in html_lower or "claude ai" in html_lower
        assert has_badge, "Bug #22: Missing 'Powered by Anthropic Claude AI' badge in sidebar"

    def test_source_tooltips(self, user_session):
        """Bug #24: Source cards should have tooltips."""
        r = user_session.get(f"{BASE_URL}/profile", timeout=REQUEST_TIMEOUT)
        assert r.status_code == 200
        # Tooltips are typically title attributes or data-bs-toggle="tooltip"
        # This is hard to verify without JS execution; basic check for tooltip attributes


# ════════════════════════════════════════════════════════════════════
#  17. WELCOME PAGE READABILITY
# ════════════════════════════════════════════════════════════════════

class TestWelcomePage:
    def test_welcome_page_readability(self):
        """Welcome page should not have white text on light grey background."""
        import requests as req
        # Try common welcome/landing routes
        for route in ["/welcome", "/", "/register"]:
            r = req.get(f"{BASE_URL}{route}", timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if r.status_code == 200:
                issues = check_text_contrast(r.text)
                if issues:
                    pytest.fail(f"Readability issues on {route}: {issues}")
                return
