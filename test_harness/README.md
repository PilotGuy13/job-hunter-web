# AI Job Hunter — Automated Test Harness

## What's Included

| File | Purpose | When to Run |
|------|---------|-------------|
| `test_config.py` | Shared config, credentials, helpers | N/A (imported) |
| `test_full_e2e.py` | **Full E2E** — every page, every feature, every bug | After major releases |
| `test_regression.py` | **Regression** — high-risk areas only | After EVERY deployment |
| `requirements.txt` | Python dependencies | Once at setup |

## Quick Start

```bash
# 1. Install dependencies
cd test_harness
pip install -r requirements.txt

# 2. Edit credentials in test_config.py
#    - Set TEST_PASS (test user password)
#    - Set ADMIN_PASS (admin user password)
#    - Confirm BASE_URL is correct

# 3. Run regression (fast — do this after every git push + reload)
pytest test_regression.py -v --tb=short

# 4. Run full E2E (thorough — do this after major changes)
pytest test_full_e2e.py -v --tb=short

# 5. Generate HTML report
pytest test_regression.py -v --html=report_regression.html --tb=short
pytest test_full_e2e.py -v --html=report_e2e.html --tb=short
```

## Running Subsets

```bash
# Only login tests
pytest test_full_e2e.py -v -k "TestLogin"

# Only bug-related checks
pytest test_full_e2e.py -v -k "bug"

# Only profile tests
pytest test_regression.py -v -k "TestProfile"

# Only check for 500 errors (fastest possible check)
pytest test_regression.py -v -k "TestNo500"
```

## Test Coverage by Bug#

| Bug# | Severity | What it Tests | Suite |
|------|----------|---------------|-------|
| #1 | Low | Breadcrumbs on every page | E2E |
| #2 | Critical | Sign out exists + works | Both |
| #5 | Medium | Tracker not oversized | Both |
| #6 | High | Run progress banner exists | Both |
| #8 | Medium | Max score per run removed from settings | Both |
| #10 | Low | Color theme removed from settings | Both |
| #11 | High | Scheduled run time removed | Both |
| #12 | High | Current password required for change | Both |
| #14 | High | Admin: delete user + password reset | Both |
| #15 | Medium | 2FA not enforced in admin | E2E |
| #16 | High | All 3 work arrangements enabled | Both |
| #17 | Medium | Min score greyed out on All Results | E2E |
| #20 | High | Interview prep gated by subscription | E2E |
| #21 | Low | AI rating timestamp on jobs | E2E |
| #22 | Low | Anthropic Claude AI badge in sidebar | E2E |
| #23 | High | Source clear/reset on profile | E2E |
| #24 | Low | Tooltips on source cards | E2E |

## Switching Environments

In `test_config.py`, change `BASE_URL`:

```python
# Live site
BASE_URL = "https://jobhunterweb.pythonanywhere.com"

# Local dev
# BASE_URL = "http://localhost:5001"
```

## Interpreting Results

- **PASSED** — Feature works correctly
- **FAILED** — Bug found or regression detected. The message tells you which bug# and what's wrong.
- **SKIPPED** — Could not test (e.g. no tracked jobs to test move, email preview route unknown)
- **🔴** prefix in failure message — Critical regression, fix before shipping

## Limitations

These tests use `requests` (HTTP-only, no JavaScript execution). They catch:
- 500 errors, broken routes, broken redirects
- Form save/load persistence
- Missing HTML elements and attributes
- API response structure

They **cannot** catch:
- Visual rendering issues (CSS, layout, colours)
- JavaScript-only interactions (drag/drop, dynamic filtering)
- Browser-specific issues

For those, you'd need Selenium/Playwright. The current suite covers ~80% of regressions that have occurred.
