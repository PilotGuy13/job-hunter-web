"""
job_engine.py — Core search, scoring and email engine for Job Hunter Web.
Called by both the scheduler and the "Run Now" button.
"""
import hashlib
import json
import logging
import re
import smtplib
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ── All available locations ──────────────────────────────────────────────────
ALL_LOCATIONS = [
    {"name": "Wellington, NZ",  "seek_where": "Wellington",  "adzuna_country": "nz", "adzuna_where": "Wellington", "jooble_loc": "Wellington, New Zealand",  "linkedin_loc": "Wellington, Wellington Region, New Zealand", "region": "nzau"},
    {"name": "Auckland, NZ",    "seek_where": "Auckland",    "adzuna_country": "nz", "adzuna_where": "Auckland",   "jooble_loc": "Auckland, New Zealand",    "linkedin_loc": "Auckland, New Zealand",                      "region": "nzau"},
    {"name": "Sydney, AU",      "seek_where": "Sydney+NSW",  "adzuna_country": "au", "adzuna_where": "Sydney",     "jooble_loc": "Sydney, Australia",        "linkedin_loc": "Sydney, New South Wales, Australia",         "region": "nzau"},
    {"name": "Melbourne, AU",   "seek_where": "Melbourne+VIC","adzuna_country":"au", "adzuna_where": "Melbourne",  "jooble_loc": "Melbourne, Australia",     "linkedin_loc": "Melbourne, Victoria, Australia",             "region": "nzau"},
    {"name": "Brisbane, AU",    "seek_where": "Brisbane+QLD","adzuna_country": "au", "adzuna_where": "Brisbane",   "jooble_loc": "Brisbane, Australia",      "linkedin_loc": "Brisbane, Queensland, Australia",            "region": "nzau"},
    {"name": "Remote",          "seek_where": "Remote",      "adzuna_country": "nz", "adzuna_where": "",           "jooble_loc": "Remote",                   "linkedin_loc": "New Zealand",                                "region": "nzau"},
    {"name": "Norway",          "seek_where": "",            "adzuna_country": "no", "adzuna_where": "",           "jooble_loc": "Norway",                   "linkedin_loc": "Norway",                                     "region": "nordic", "finn_rss": True},
    {"name": "Denmark",         "seek_where": "",            "adzuna_country": "",   "adzuna_where": "",           "jooble_loc": "Denmark",                  "linkedin_loc": "Denmark",                                    "region": "nordic", "jobindex_rss": True},
    {"name": "Iceland",         "seek_where": "",            "adzuna_country": "",   "adzuna_where": "",           "jooble_loc": "Iceland",                  "linkedin_loc": "Iceland",                                    "region": "nordic"},
    {"name": "Christchurch, NZ","seek_where": "Christchurch","adzuna_country": "nz", "adzuna_where": "Christchurch", "jooble_loc": "Christchurch, New Zealand", "linkedin_loc": "Christchurch, Canterbury, New Zealand",      "region": "nzau"},
    {"name": "Australia",       "seek_where": "",            "adzuna_country": "au", "adzuna_where": "",           "jooble_loc": "Australia",                "linkedin_loc": "Australia",                                   "region": "nzau"},
    {"name": "Canada",          "seek_where": "",            "adzuna_country": "ca", "adzuna_where": "",           "jooble_loc": "Canada",                   "linkedin_loc": "Canada",                                      "region": "intl"},
    {"name": "United States",   "seek_where": "",            "adzuna_country": "us", "adzuna_where": "",           "jooble_loc": "United States",             "linkedin_loc": "United States",                               "region": "intl"},
    {"name": "United Kingdom",  "seek_where": "",            "adzuna_country": "gb", "adzuna_where": "",           "jooble_loc": "United Kingdom",            "linkedin_loc": "United Kingdom",                              "region": "intl"},
    {"name": "Germany",         "seek_where": "",            "adzuna_country": "de", "adzuna_where": "",           "jooble_loc": "Germany",                  "linkedin_loc": "Germany",                                     "region": "intl"},
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

ADZUNA_SUPPORTED = {"nz", "au", "no", "ca", "us", "gb", "de"}


def make_job(title, company, location_str, url, source, listed, description, search_location):
    return {
        "title":           str(title).strip(),
        "company":         str(company).strip() or "Unknown",
        "location":        str(location_str).strip() or search_location,
        "url":             str(url).strip(),
        "source":          source,
        "listed":          str(listed).strip(),
        "description":     str(description).strip()[:300],
        "search_location": search_location,
    }


def job_fingerprint(job):
    key = (job.get("url") or job.get("title", "") + job.get("company", "")).strip()
    return hashlib.md5(key.encode()).hexdigest()


def deduplicate(jobs):
    seen, unique = set(), []
    for job in jobs:
        key = (job["title"].lower().strip(), job["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


# ── Scrapers ─────────────────────────────────────────────────────────────────

def scrape_seek(keyword, location):
    jobs = []
    if not location.get("seek_where"):
        return jobs
    base = "https://www.seek.com.au" if "AU" in location["name"] else "https://www.seek.co.nz"
    url  = f"{base}/{urllib.parse.quote(keyword)}-jobs/in-{urllib.parse.quote(location['seek_where'])}?sortmode=ListedDate"
    try:
        resp = SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.text, "html.parser")
        for article in soup.find_all("article"):
            t = article.find("a",    attrs={"data-automation": "jobTitle"})
            c = article.find("a",    attrs={"data-automation": "jobCompany"})
            l = article.find("a",    attrs={"data-automation": "jobLocation"}) or article.find("span", attrs={"data-automation": "jobLocation"})
            d = article.find("span", attrs={"data-automation": "jobListingDate"})
            if not t:
                continue
            href = t.get("href", "")
            jobs.append(make_job(t.get_text(strip=True), c.get_text(strip=True) if c else "Unknown",
                                 l.get_text(strip=True) if l else location["name"],
                                 f"{base}{href}" if href.startswith("/") else href,
                                 "Seek", d.get_text(strip=True) if d else "", "", location["name"]))
        log.info(f"Seek [{location['name']}] '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"Seek error: {e}")
    return jobs


def fetch_adzuna(keyword, location, app_id, app_key):
    jobs = []
    if not app_id or "YOUR_" in app_id:
        return jobs
    country = location.get("adzuna_country", "")
    if country not in ADZUNA_SUPPORTED:
        return jobs
    params = {"app_id": app_id, "app_key": app_key, "results_per_page": 20, "what": keyword, "sort_by": "date"}
    if location.get("adzuna_where"):
        params["where"] = location["adzuna_where"]
    try:
        resp = requests.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Adzuna [{country}] HTTP {resp.status_code}: {resp.text[:200]}")
            return jobs
        for r in resp.json().get("results", []):
            area = r.get("location", {}).get("area", [])
            jobs.append(make_job(r.get("title",""), r.get("company",{}).get("display_name","Unknown"),
                                 ", ".join(area[-2:]) if area else location["name"],
                                 r.get("redirect_url",""), "Adzuna", r.get("created",""),
                                 r.get("description",""), location["name"]))
        log.info(f"Adzuna [{location['name']}] '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"Adzuna error: {e}")
    return jobs


def fetch_finn_rss(keyword):
    jobs = []
    url = f"https://www.finn.no/rss/job/fulltime/result.rss?q={urllib.parse.quote(keyword)}&occupation=20001"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return jobs
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return jobs
        for item in channel.findall("item"):
            title   = (item.findtext("title")   or "").strip()
            link    = (item.findtext("link")    or "").strip()
            pubdate = (item.findtext("pubDate") or "").strip()
            desc    = BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(separator=" ", strip=True)[:300]
            ce      = item.find("{http://purl.org/dc/elements/1.1/}creator")
            company = ce.text.strip() if ce is not None else "Unknown"
            if title:
                jobs.append(make_job(title, company, "Norway", link, "Finn.no", pubdate, desc, "Norway"))
        log.info(f"Finn.no '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"Finn.no error: {e}")
    return jobs


def fetch_jobindex_rss(keyword):
    jobs = []
    url = f"https://www.jobindex.dk/jobsoegning.rss?q={urllib.parse.quote(keyword)}&lang=en"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return jobs
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return jobs
        for item in channel.findall("item"):
            title   = (item.findtext("title")   or "").strip()
            link    = (item.findtext("link")    or "").strip()
            pubdate = (item.findtext("pubDate") or "").strip()
            desc    = BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(separator=" ", strip=True)[:300]
            ce      = item.find("{http://purl.org/dc/elements/1.1/}creator")
            company = ce.text.strip() if ce is not None else "Unknown"
            if title:
                jobs.append(make_job(title, company, "Denmark", link, "Jobindex", pubdate, desc, "Denmark"))
        log.info(f"Jobindex '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"Jobindex error: {e}")
    return jobs


def fetch_jobicy_rss(keyword):
    jobs = []
    url = f"https://jobicy.com/?feed=job_feed&job_categories=it-security&search_keywords={urllib.parse.quote(keyword)}"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return jobs
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return jobs
        for item in channel.findall("item"):
            title   = (item.findtext("title")   or "").strip()
            link    = (item.findtext("link")    or "").strip()
            pubdate = (item.findtext("pubDate") or "").strip()
            ce      = item.find("{https://jobicy.com/}hiringOrganization")
            company = ce.text.strip() if ce is not None else "Unknown"
            le      = item.find("{https://jobicy.com/}jobLocation")
            loc_str = le.text.strip() if le is not None else "Remote"
            desc    = BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(separator=" ", strip=True)[:300]
            if title:
                jobs.append(make_job(title, company, loc_str, link, "Jobicy", pubdate, desc, "Remote"))
        log.info(f"Jobicy '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"Jobicy error: {e}")
    return jobs


def search_linkedin_jobs(keyword, location):
    jobs = []
    loc_str = location.get("linkedin_loc", location["name"])
    url = f"https://www.linkedin.com/jobs/search/?keywords={urllib.parse.quote(keyword)}&location={urllib.parse.quote(loc_str)}&f_TPR=r86400&sortBy=DD"
    try:
        resp = SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            log.warning(f"LinkedIn [{location['name']}] HTTP {resp.status_code}")
            return jobs
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.find_all("div", class_=re.compile(r"base-card")):
            t = card.find("h3", class_=re.compile(r"base-search-card__title"))
            c = card.find("h4", class_=re.compile(r"base-search-card__subtitle"))
            l = card.find("span", class_=re.compile(r"job-search-card__location"))
            a = card.find("a",   class_=re.compile(r"base-card__full-link"))
            d = card.find("time")
            if not t:
                continue
            jobs.append(make_job(t.get_text(strip=True), c.get_text(strip=True) if c else "Unknown",
                                 l.get_text(strip=True) if l else location["name"],
                                 a.get("href", url) if a else url,
                                 "LinkedIn", d.get("datetime","") if d else "", "", location["name"]))
        log.info(f"LinkedIn [{location['name']}] '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"LinkedIn error: {e}")
    return jobs


# ── Claude Scoring ────────────────────────────────────────────────────────────

def score_jobs(jobs, cv_summary, anthropic_key, max_jobs=25, work_arrangement=None):
    client = Anthropic(api_key=anthropic_key)
    scored = []
    for job in jobs[:max_jobs]:
        try:
            desc_block = f"\nJob snippet:\n{job['description']}" if job.get("description") else ""
            arr = work_arrangement or []
            work_pref = ", ".join(arr) if arr else "No preference (remote, hybrid or onsite all acceptable)"
            prompt = f"""You are a professional CV advisor evaluating job opportunities.

CV Summary:
{cv_summary}

Job:
Title:    {job['title']}
Company:  {job['company']}
Location: {job['location']} ({job['search_location']})
Source:   {job['source']}
URL:      {job['url']}{desc_block}
Candidate work preference: {work_pref}

Respond ONLY with valid JSON, no markdown:
{{
  "compatibility_score":   <0-100>,
  "compatibility_label":   "<Excellent Match|Strong Match|Good Match|Partial Match|Low Match>",
  "match_reasons":         ["reason 1","reason 2","reason 3"],
  "gaps":                  ["gap 1","gap 2"],
  "cv_tweaks":             ["tweak 1","tweak 2"],
  "hiring_manager_search": "<Google query>",
  "linkedin_search":       "<LinkedIn URL>",
  "salary_estimate":       "<local currency range>",
  "apply_priority":        "<High|Medium|Low>"
}}"""
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=900,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            job.update(json.loads(raw))
            scored.append(job)
        except json.JSONDecodeError:
            job.update({"compatibility_score": 50, "compatibility_label": "Unknown",
                        "match_reasons": [], "gaps": [], "cv_tweaks": [],
                        "hiring_manager_search": "", "linkedin_search": "",
                        "salary_estimate": "N/A", "apply_priority": "Medium"})
            scored.append(job)
        except Exception as e:
            log.warning(f"Scoring error for '{job['title']}': {e}")
        time.sleep(0.5)
    return scored


# ── Email ─────────────────────────────────────────────────────────────────────

SOURCE_COLORS = {"Seek": "#0ea5e9", "Adzuna": "#059669", "Jooble": "#7c3aed",
                 "Jobicy": "#db2777", "Finn.no": "#dc2626", "Jobindex": "#2563eb", "LinkedIn": "#0a66c2"}
SCORE_COLORS  = {"Excellent Match": ("#065f46","#d1fae5"), "Strong Match": ("#1e40af","#dbeafe"),
                 "Good Match": ("#92400e","#fef3c7"), "Partial Match": ("#6b7280","#f3f4f6"),
                 "Low Match": ("#6b7280","#f3f4f6"), "Unknown": ("#6b7280","#f3f4f6")}
PRIORITY_BADGE= {"High": ("#dc2626","#fef2f2"), "Medium": ("#d97706","#fffbeb"), "Low": ("#6b7280","#f9fafb")}


def build_email_html(jobs, user_name=""):
    today = date.today().strftime("%A, %d %B %Y")
    priority_order = {"High": 0, "Medium": 1, "Low": 2}
    jobs_sorted = sorted(jobs, key=lambda j: (
        priority_order.get(j.get("apply_priority","Medium"), 1),
        -(j.get("compatibility_score", 0)),
    ))

    job_cards = ""
    for job in jobs_sorted:
        score       = job.get("compatibility_score", 0)
        label       = job.get("compatibility_label", "Unknown")
        s_col, s_bg = SCORE_COLORS.get(label, ("#6b7280","#f3f4f6"))
        priority    = job.get("apply_priority","Medium")
        p_col, p_bg = PRIORITY_BADGE.get(priority, ("#6b7280","#f9fafb"))
        src         = job.get("source","")
        src_color   = SOURCE_COLORS.get(src, "#6b7280")
        salary      = job.get("salary_estimate","")
        listed      = job.get("listed","")
        desc        = job.get("description","")
        hm_search   = job.get("hiring_manager_search","")
        hm_google   = f"https://www.google.com/search?q={urllib.parse.quote(hm_search)}" if hm_search else ""
        hm_li       = job.get("linkedin_search","")

        reasons_html = "".join(f'<li style="margin:3px 0;color:#374151;">{r}</li>' for r in job.get("match_reasons",[]))  or "<li style='color:#6b7280;font-style:italic;'>No reasons available</li>"
        gaps_html    = "".join(f'<li style="margin:3px 0;color:#374151;">{g}</li>' for g in job.get("gaps",[]))           or "<li style='color:#6b7280;font-style:italic;'>No significant gaps</li>"
        tweaks_html  = "".join(f'<li style="margin:4px 0;color:#374151;">{t}</li>' for t in job.get("cv_tweaks",[]))      or "<li style='color:#6b7280;font-style:italic;'>CV is already well-aligned</li>"

        job_cards += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:22px 24px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;align-items:center;">
            <span style="background:{src_color};color:#fff;font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;">{src}</span>
            <span style="background:#f3f4f6;color:#1f2937;font-size:11px;font-weight:600;padding:2px 9px;border-radius:20px;">📍 {job.get('search_location','')}</span>
            <span style="background:{p_bg};color:{p_col};font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;border:1px solid {p_col}44;margin-left:auto;">{priority} Priority</span>
            <span style="background:{s_bg};color:{s_col};font-size:13px;font-weight:700;padding:3px 11px;border-radius:20px;border:1px solid {s_col}33;">{score}% · {label}</span>
          </div>
          <h2 style="margin:0 0 3px 0;font-size:17px;font-weight:700;">
            <a href="{job.get('url','#')}" target="_blank" style="color:#1d4ed8;text-decoration:none;">{job.get('title','')}</a>
          </h2>
          <p style="margin:0 0 10px 0;font-size:14px;color:#111827;">
            🏢 <strong>{job.get('company','')}</strong> &nbsp;·&nbsp; 📍 {job.get('location','')}
            {f'&nbsp;·&nbsp; 💰 {salary}' if salary and salary != 'N/A' else ''}
            {f'&nbsp;·&nbsp; 🕒 {listed}' if listed else ''}
          </p>
          {f'<p style="margin:0 0 10px 0;font-size:13px;color:#374151;font-style:italic;">{desc[:200]}...</p>' if desc else ''}
          <hr style="border:none;border-top:1px solid #f3f4f6;margin:10px 0;">
          <table style="width:100%;border-collapse:collapse;margin-bottom:10px;">
            <tr>
              <td style="width:50%;padding-right:10px;vertical-align:top;">
                <p style="margin:0 0 5px 0;font-size:11px;font-weight:700;color:#1f2937;text-transform:uppercase;letter-spacing:0.06em;">✅ Why You Match</p>
                <ul style="margin:0;padding-left:16px;font-size:13px;line-height:1.6;">{reasons_html}</ul>
              </td>
              <td style="width:50%;padding-left:10px;vertical-align:top;border-left:1px solid #f3f4f6;">
                <p style="margin:0 0 5px 0;font-size:11px;font-weight:700;color:#1f2937;text-transform:uppercase;letter-spacing:0.06em;">⚠️ Gaps</p>
                <ul style="margin:0;padding-left:16px;font-size:13px;line-height:1.6;">{gaps_html}</ul>
              </td>
            </tr>
          </table>
          <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:10px 14px;margin-bottom:10px;">
            <p style="margin:0 0 5px 0;font-size:11px;font-weight:700;color:#166534;text-transform:uppercase;letter-spacing:0.06em;">📝 CV Tweak Suggestions</p>
            <ul style="margin:0;padding-left:16px;font-size:13px;line-height:1.6;">{tweaks_html}</ul>
          </div>
          <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px 14px;margin-bottom:12px;">
            <p style="margin:0 0 5px 0;font-size:11px;font-weight:700;color:#1e40af;text-transform:uppercase;letter-spacing:0.06em;">🔍 Find the Hiring Manager</p>
            <p style="margin:0;font-size:13px;">
              {'<a href="' + hm_google + '" target="_blank" style="color:#1d4ed8;">🌐 Google Search</a>' if hm_google else ''}
              {' &nbsp;|&nbsp; ' if hm_google and hm_li else ''}
              {'<a href="' + hm_li + '" target="_blank" style="color:#1d4ed8;">💼 LinkedIn</a>' if hm_li else ''}
            </p>
          </div>
          <div style="text-align:right;">
            <a href="{job.get('url','#')}" target="_blank" style="background:#1d4ed8;color:#fff;font-size:13px;font-weight:600;padding:7px 18px;border-radius:7px;text-decoration:none;">View &amp; Apply →</a>
          </div>
        </div>"""

    loc_counts = {}
    src_counts = {}
    for j in jobs_sorted:
        loc_counts[j.get("search_location","?")] = loc_counts.get(j.get("search_location","?"), 0) + 1
        src_counts[j.get("source","?")]          = src_counts.get(j.get("source","?"), 0) + 1

    loc_pills = " ".join(f'<span style="background:#e0e7ff;color:#3730a3;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;margin:2px;">{k}: {v}</span>' for k,v in loc_counts.items())
    src_pills = " ".join(f'<span style="background:{SOURCE_COLORS.get(k,"#6b7280")}22;color:{SOURCE_COLORS.get(k,"#6b7280")};padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;margin:2px;">{k}: {v}</span>' for k,v in src_counts.items())

    greeting = f" for {user_name}" if user_name else ""

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Digest — {today}</title></head>
<body style="margin:0;padding:0;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:24px 16px;">
  <div style="border-left:6px solid #1e3a5f;padding:20px 24px;margin-bottom:18px;">
    <div style="font-size:11px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:6px;color:#1e3a5f;">Job Intelligence Digest{greeting}</div>
    <h1 style="margin:0 0 6px 0;font-size:26px;font-weight:800;color:#111827;">Daily Job Digest</h1>
    <p style="margin:0 0 10px 0;font-size:14px;color:#374151;">{today}</p>
    <p style="margin:0;font-size:14px;color:#111827;">📋 <strong>{len(jobs_sorted)}</strong> new roles &nbsp;·&nbsp; 🚀 <strong>{sum(1 for j in jobs_sorted if j.get('apply_priority')=='High')}</strong> high priority &nbsp;·&nbsp; ⭐ <strong>{sum(1 for j in jobs_sorted if 'Excellent' in j.get('compatibility_label',''))}</strong> excellent matches</p>
  </div>
  <div style="background:#fff;border-radius:10px;padding:12px 18px;margin-bottom:16px;border:1px solid #e5e7eb;">
    <div style="margin-bottom:6px;"><span style="font-size:11px;font-weight:700;color:#1f2937;text-transform:uppercase;letter-spacing:0.06em;margin-right:8px;">By Location:</span>{loc_pills}</div>
    <div><span style="font-size:11px;font-weight:700;color:#1f2937;text-transform:uppercase;letter-spacing:0.06em;margin-right:8px;">By Source:</span>{src_pills}</div>
  </div>
  {job_cards or '<p style="text-align:center;color:#6b7280;padding:40px;">No new roles today — check back tomorrow!</p>'}
  <div style="text-align:center;padding:22px 0 8px;font-size:12px;color:#374151;border-top:1px solid #e5e7eb;margin-top:6px;">
    <p style="margin:0;">Automated by Job Hunter Web · Manage at <a href="http://localhost:5000" style="color:#1d4ed8;">localhost:5000</a></p>
  </div>
</div></body></html>"""


def send_email(html_body, job_count, sender_email, smtp_password, recipient_email, user_name=""):
    today   = date.today().strftime("%d %b %Y")
    subject = f"🔒 Job Digest{' for ' + user_name if user_name else ''} — {job_count} new roles | {today}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender_email
    msg["To"]      = recipient_email
    msg.attach(MIMEText(f"Daily job digest — {job_count} new roles. View in HTML email client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(sender_email, smtp_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())


# ── Custom RSS Sources ───────────────────────────────────────────────────────

def fetch_custom_rss(source, keyword, location_name=""):
    """Fetch jobs from a custom RSS source added via the Admin panel."""
    jobs = []
    if not source.rss_url or not source.is_active:
        return jobs
    url = source.build_url(keyword, location_name)
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            log.warning(f"{source.name} RSS {resp.status_code} for '{keyword}'")
            return jobs
        root    = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return jobs
        for item in channel.findall("item"):
            title   = (item.findtext("title")   or "").strip()
            link    = (item.findtext("link")    or "").strip()
            pubdate = (item.findtext("pubDate") or "").strip()
            desc    = BeautifulSoup(
                item.findtext("description") or "", "html.parser"
            ).get_text(separator=" ", strip=True)[:300]
            ce      = item.find("{http://purl.org/dc/elements/1.1/}creator")
            company = ce.text.strip() if ce is not None else "Unknown"
            if not title:
                continue
            jobs.append(make_job(title, company, location_name or "Various",
                                 link, source.name, pubdate, desc,
                                 location_name or "Remote"))
        log.info(f"{source.name} [{location_name}] '{keyword}' -> {len(jobs)}")
    except ET.ParseError as e:
        log.warning(f"{source.name} RSS XML error: {e}")
    except Exception as e:
        log.warning(f"{source.name} RSS error: {e}")
    return jobs


# ── Main run function ─────────────────────────────────────────────────────────

def run_for_user(user, seen_fingerprints: set, progress_callback=None) -> dict:
    """
    Run the full job search pipeline for one user.
    Returns a dict with results summary.
    """
    def progress(msg):
        log.info(msg)
        if progress_callback:
            progress_callback(msg)

    # Get user's selected locations
    selected_loc_names = set(user.locations) if user.locations else {l["name"] for l in ALL_LOCATIONS}
    locations = [l for l in ALL_LOCATIONS if l["name"] in selected_loc_names]
    keywords  = user.keywords if user.keywords else ["Security Architect", "GRC Security", "Cyber Security"]

    all_jobs      = []
    finn_done     = False
    jobindex_done = False
    jobicy_done   = set()

    # Resolve effective API keys (user key > admin shared key)
    try:
        from app import get_effective_key
        effective_anthropic   = get_effective_key(user, 'anthropic_key')
        effective_adzuna_id   = get_effective_key(user, 'adzuna_app_id')
        effective_adzuna_key  = get_effective_key(user, 'adzuna_app_key')
        effective_smtp_pw     = get_effective_key(user, 'smtp_password')
        effective_sender      = get_effective_key(user, 'sender_email')
    except Exception:
        effective_anthropic  = user.anthropic_key or ""
        effective_adzuna_id  = user.adzuna_app_id or ""
        effective_adzuna_key = user.adzuna_app_key or ""
        effective_smtp_pw    = user.smtp_password or ""
        effective_sender     = user.sender_email or ""

    # Log source availability
    if not effective_adzuna_id:
        progress("⚠️ Adzuna API key not set — skipping Adzuna source")
    if not effective_anthropic:
        progress("⚠️ Anthropic API key not set — cannot score jobs")

    # Build custom locations for any user-selected locations not in ALL_LOCATIONS
    all_loc_names = {l["name"] for l in ALL_LOCATIONS}
    for custom_name in (set(user.locations) - all_loc_names):
        if custom_name.strip():
            locations.append({
                "name":           custom_name,
                "seek_where":     "",
                "adzuna_country": "",
                "adzuna_where":   "",
                "jooble_loc":     custom_name,
                "linkedin_loc":   custom_name,
                "region":         "custom",
            })

    for location in locations:
        is_nordic = location.get("region") == "nordic"
        kws = keywords[:4] if is_nordic else keywords

        for keyword in kws:
            seek_results = scrape_seek(keyword, location)
            all_jobs.extend(seek_results)
            if seek_results:
                progress(f"Seek [{location['name']}] '{keyword}' -> {len(seek_results)} jobs")
            time.sleep(1.0)

            adzuna_results = fetch_adzuna(keyword, location, effective_adzuna_id, effective_adzuna_key)
            all_jobs.extend(adzuna_results)
            if adzuna_results:
                progress(f"Adzuna [{location['name']}] '{keyword}' -> {len(adzuna_results)} jobs")
            elif effective_adzuna_id and location.get('adzuna_country') in ADZUNA_SUPPORTED:
                progress(f"Adzuna [{location['name']}] '{keyword}' -> 0 (API returned no results)")
            time.sleep(0.8)
            linkedin_results = search_linkedin_jobs(keyword, location)
            all_jobs.extend(linkedin_results)
            if linkedin_results:
                progress(f"LinkedIn [{location['name']}] '{keyword}' -> {len(linkedin_results)} jobs")
            time.sleep(1.2)
            if keyword not in jobicy_done:
                jobicy_results = fetch_jobicy_rss(keyword)
                all_jobs.extend(jobicy_results)
                if jobicy_results:
                    progress(f"Jobicy '{keyword}' -> {len(jobicy_results)} jobs")
                jobicy_done.add(keyword)
                time.sleep(0.8)

        if location.get("finn_rss") and not finn_done:
            for kw in kws:
                all_jobs.extend(fetch_finn_rss(kw)); time.sleep(0.8)
            finn_done = True

        if location.get("jobindex_rss") and not jobindex_done:
            for kw in kws:
                all_jobs.extend(fetch_jobindex_rss(kw)); time.sleep(0.8)
            jobindex_done = True

    # Fetch from custom RSS sources added via Admin panel
    try:
        from models import JobSource
        custom_sources = JobSource.query.filter_by(is_active=True, is_builtin=False).all()
        for source in custom_sources:
            for location in locations:
                for keyword in (keywords[:4] if location.get("region") == "nordic" else keywords):
                    all_jobs.extend(fetch_custom_rss(source, keyword, location["name"]))
                    time.sleep(0.8)
    except Exception as e:
        log.warning(f"Custom sources error: {e}")

    all_jobs = deduplicate(all_jobs)
    progress(f"Collected {len(all_jobs)} jobs after deduplication")

    new_jobs = [j for j in all_jobs if job_fingerprint(j) not in seen_fingerprints]
    progress(f"{len(new_jobs)} new jobs (not seen before)")

    if not new_jobs:
        return {"status": "ok", "new_jobs": 0, "scored": 0, "relevant": 0, "emailed": False}

    scored_jobs   = score_jobs(new_jobs, user.cv_summary, effective_anthropic, user.max_jobs_to_score, user.work_arrangement)
    relevant_jobs = [j for j in scored_jobs if j.get("compatibility_score", 0) >= user.score_threshold]
    progress(f"{len(relevant_jobs)} relevant jobs (score >= {user.score_threshold})")

    emailed = False
    if relevant_jobs and user.sender_email and user.smtp_password and user.recipient_email:
        try:
            html = build_email_html(relevant_jobs, user.full_name)
            send_email(html, len(relevant_jobs), effective_sender, effective_smtp_pw,
                       user.recipient_email, user.full_name)
            emailed = True
            progress(f"Email sent to {user.recipient_email}")
        except Exception as e:
            progress(f"Email error: {e}")

    return {
        "status":        "ok",
        "new_jobs":      len(new_jobs),
        "scored":        len(scored_jobs),
        "relevant":      len(relevant_jobs),
        "emailed":       emailed,
        "scored_jobs":   scored_jobs,
        "relevant_jobs": relevant_jobs,
    }
