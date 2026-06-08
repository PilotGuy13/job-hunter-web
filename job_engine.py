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


def _get_smtp_settings(sender_email):
    """Return (host, port) based on the sender email domain."""
    domain = sender_email.rsplit("@", 1)[-1].lower() if "@" in sender_email else ""
    SMTP_MAP = {
        "gmail.com": ("smtp.gmail.com", 587),
        "googlemail.com": ("smtp.gmail.com", 587),
        "outlook.com": ("smtp.office365.com", 587),
        "hotmail.com": ("smtp.office365.com", 587),
        "yahoo.com": ("smtp.mail.yahoo.com", 587),
    }
    return SMTP_MAP.get(domain, ("mail.privateemail.com", 587))


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
    # Seek NZ may redirect to nz.seek.com; AU stays at seek.com.au
    is_au = "AU" in location["name"] or location.get("adzuna_country") == "au"
    base = "https://www.seek.com.au" if is_au else "https://www.seek.co.nz"
    url  = f"{base}/{urllib.parse.quote(keyword)}-jobs/in-{urllib.parse.quote(location['seek_where'])}?sortmode=ListedDate"
    try:
        resp = SESSION.get(url, timeout=15, allow_redirects=True)
        # Track the final URL in case of redirect (seek.co.nz -> nz.seek.com)
        final_base = f"{resp.url.split('/')[0]}//{resp.url.split('/')[2]}"
        if resp.status_code != 200:
            log.warning(f"Seek [{location['name']}] HTTP {resp.status_code} for '{keyword}'")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # Strategy 1: Extract JSON-LD structured data (schema.org JobPosting)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else ld.get("itemListElement", [ld])
                for item in items:
                    posting = item.get("item", item) if isinstance(item, dict) else item
                    if not isinstance(posting, dict):
                        continue
                    if posting.get("@type") != "JobPosting":
                        continue
                    title = posting.get("title", "")
                    company_obj = posting.get("hiringOrganization", {})
                    company = company_obj.get("name", "Unknown") if isinstance(company_obj, dict) else "Unknown"
                    loc_obj = posting.get("jobLocation", {})
                    if isinstance(loc_obj, dict):
                        addr = loc_obj.get("address", {})
                        loc_str = addr.get("addressLocality", location["name"]) if isinstance(addr, dict) else location["name"]
                    elif isinstance(loc_obj, list) and loc_obj:
                        addr = loc_obj[0].get("address", {})
                        loc_str = addr.get("addressLocality", location["name"]) if isinstance(addr, dict) else location["name"]
                    else:
                        loc_str = location["name"]
                    link = posting.get("url", "")
                    posted = posting.get("datePosted", "")
                    desc = posting.get("description", "")
                    if isinstance(desc, str):
                        desc = BeautifulSoup(desc, "html.parser").get_text(separator=" ", strip=True)[:300]
                    if title:
                        jobs.append(make_job(title, company, loc_str, link, "Seek", posted, desc, location["name"]))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        # Strategy 2: Look for embedded Redux/Apollo state in script tags
        if not jobs:
            for script in soup.find_all("script"):
                text = script.string or ""
                for marker in ["window.SEEK_REDUX_DATA", "window.__data", "window.__NEXT_DATA__", "window.__APOLLO_STATE__"]:
                    if marker in text:
                        try:
                            json_start = text.index("{", text.index(marker))
                            raw = text[json_start:]
                            # Find balanced braces
                            depth, end = 0, 0
                            for i, ch in enumerate(raw):
                                if ch == "{": depth += 1
                                elif ch == "}": depth -= 1
                                if depth == 0:
                                    end = i + 1
                                    break
                            data = json.loads(raw[:end])
                            # Navigate to job list — structure varies
                            job_list = _extract_seek_jobs_from_state(data)
                            for j in job_list:
                                jobs.append(make_job(
                                    j.get("title", ""), j.get("advertiser", {}).get("description", "Unknown"),
                                    j.get("location", location["name"]),
                                    f"{final_base}/job/{j['id']}" if j.get("id") else "",
                                    "Seek", j.get("listingDate", ""),
                                    j.get("teaser", "")[:300], location["name"],
                                ))
                        except Exception:
                            continue

        # Strategy 3: Updated HTML selectors (fallback for any remaining server-rendered content)
        if not jobs:
            for article in soup.find_all("article"):
                # Try data-automation first, then data-testid, then class-based selectors
                t = (article.find("a", attrs={"data-automation": "jobTitle"})
                     or article.find("a", attrs={"data-testid": "job-title"})
                     or article.find("a", class_=re.compile(r"job.*title", re.I))
                     or article.find("h3"))
                c = (article.find("a", attrs={"data-automation": "jobCompany"})
                     or article.find("a", attrs={"data-testid": "job-company"})
                     or article.find("span", class_=re.compile(r"company|advertiser", re.I)))
                l = (article.find(attrs={"data-automation": "jobLocation"})
                     or article.find(attrs={"data-testid": "job-location"})
                     or article.find("span", class_=re.compile(r"location", re.I)))
                d = (article.find("span", attrs={"data-automation": "jobListingDate"})
                     or article.find("time")
                     or article.find("span", class_=re.compile(r"date|listed", re.I)))
                if not t:
                    continue
                href = t.get("href", "")
                full_url = f"{final_base}{href}" if href.startswith("/") else href
                jobs.append(make_job(
                    t.get_text(strip=True),
                    c.get_text(strip=True) if c else "Unknown",
                    l.get_text(strip=True) if l else location["name"],
                    full_url, "Seek",
                    d.get_text(strip=True) if d else "",
                    "", location["name"],
                ))

        if not jobs:
            log.warning(f"Seek [{location['name']}] '{keyword}' -> 0 jobs (all 3 parse strategies failed). "
                        f"Final URL: {resp.url}, page size: {len(resp.text)} bytes, "
                        f"<article> count: {len(soup.find_all('article'))}, "
                        f"<script type=ld+json> count: {len(soup.find_all('script', type='application/ld+json'))}")
        else:
            log.info(f"Seek [{location['name']}] '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"Seek error [{location['name']}] '{keyword}': {e}")
    return jobs


def _extract_seek_jobs_from_state(data):
    """Walk a nested dict/list looking for Seek job objects (have 'id' + 'title' + 'advertiser')."""
    results = []
    if isinstance(data, dict):
        if "title" in data and "advertiser" in data and "id" in data:
            results.append(data)
        for v in data.values():
            results.extend(_extract_seek_jobs_from_state(v))
    elif isinstance(data, list):
        for item in data:
            results.extend(_extract_seek_jobs_from_state(item))
    return results[:30]


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


def fetch_jooble(keyword, location, api_key):
    """Fetch jobs from Jooble API — aggregates Seek, Adzuna, Indeed & 100+ sources."""
    jobs = []
    if not api_key:
        return jobs
    loc_str = location.get("jooble_loc", location["name"])
    try:
        resp = requests.post(
            f"https://jooble.org/api/{api_key}",
            json={"keywords": keyword, "location": loc_str, "page": 1, "ResultOnPage": 20},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"Jooble [{location['name']}] HTTP {resp.status_code}")
            return jobs
        data = resp.json()
        for r in data.get("jobs", []):
            title = r.get("title", "").strip()
            # Clean HTML from title and snippet
            if "<" in title:
                title = BeautifulSoup(title, "html.parser").get_text(strip=True)
            snippet = r.get("snippet", "")
            if "<" in snippet:
                snippet = BeautifulSoup(snippet, "html.parser").get_text(strip=True)[:300]
            if title:
                jobs.append(make_job(
                    title, r.get("company", "Unknown"),
                    r.get("location", loc_str),
                    r.get("link", ""),
                    "Jooble (aggregator)", r.get("updated", ""),
                    snippet, location["name"],
                ))
        log.info(f"Jooble [{location['name']}] '{keyword}' -> {len(jobs)} (total available: {data.get('totalCount', '?')})")
    except Exception as e:
        log.warning(f"Jooble error [{location['name']}]: {e}")
    return jobs


def scrape_govt_nz(keyword, location):
    """Scrape jobs.govt.nz — NZ public sector job board. No API key needed."""
    jobs = []
    # Only search NZ locations
    loc_name = location["name"]
    if not any(nz in loc_name for nz in ["NZ", "Wellington", "Auckland", "Christchurch", "Remote", "New Zealand"]):
        return jobs
    # Map location to jobs.govt.nz location values
    govt_loc_map = {
        "Wellington, NZ": "Wellington", "Auckland, NZ": "Auckland",
        "Christchurch, NZ": "Christchurch", "Remote": "Flexible Location",
    }
    govt_loc = govt_loc_map.get(loc_name, "")
    loc_param = f'&in_location="{govt_loc}"' if govt_loc else ""
    url = (f"https://jobs.govt.nz/jobtools/jncustomsearch.searchResults"
           f"?in_organid=16563&in_jobDate=All"
           f"&in_skills={urllib.parse.quote(keyword)}"
           f"{loc_param}"
           f"&in_orderby=dateinput+desc")
    try:
        resp = SESSION.get(url, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            log.warning(f"jobs.govt.nz HTTP {resp.status_code}")
            return jobs
        soup = BeautifulSoup(resp.text, "html.parser")
        # Job cards are in div.job-result or similar containers with links to viewFullSingle
        for link in soup.find_all("a", href=re.compile(r"viewFullSingle")):
            title_el = link.find(class_=re.compile(r"title|job-title")) or link
            title = title_el.get_text(strip=True)
            if not title or len(title) < 3:
                continue
            href = link.get("href", "")
            if not href.startswith("http"):
                if not href.startswith("/"):
                    href = "/" + href
                href = f"https://jobs.govt.nz{href}"
            # Try to find employer and location from surrounding elements
            parent = link.find_parent("div") or link.find_parent("li") or link.find_parent("tr")
            company = ""
            job_loc = loc_name
            if parent:
                # Look for employer text
                emp_el = parent.find(class_=re.compile(r"employer|company|agency|organ"))
                if emp_el:
                    company = emp_el.get_text(strip=True)
                loc_el = parent.find(class_=re.compile(r"location"))
                if loc_el:
                    job_loc = loc_el.get_text(strip=True)
                # Also try table cells or spans
                if not company:
                    spans = parent.find_all("span")
                    for s in spans:
                        txt = s.get_text(strip=True)
                        if "Ministry" in txt or "Department" in txt or "Commission" in txt or "Authority" in txt:
                            company = txt
                            break
            jobs.append(make_job(
                title, company or "NZ Government",
                job_loc, href, "NZ Govt Jobs", "", "",
                location["name"],
            ))
        # Deduplicate by URL
        seen_urls = set()
        unique = []
        for j in jobs:
            if j["url"] not in seen_urls:
                seen_urls.add(j["url"])
                unique.append(j)
        jobs = unique[:20]
        log.info(f"jobs.govt.nz [{location['name']}] '{keyword}' -> {len(jobs)}")
    except Exception as e:
        log.warning(f"jobs.govt.nz error [{location['name']}]: {e}")
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
    url = f"https://www.linkedin.com/jobs/search/?keywords={urllib.parse.quote(keyword)}&location={urllib.parse.quote(loc_str)}&f_TPR=r604800&sortBy=DD"
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
    arr = work_arrangement or []
    work_pref = ", ".join(arr) if arr else "No preference (remote, hybrid or onsite all acceptable)"
    system_prompt = [
        {
            "type": "text",
            "text": f"You are a professional CV advisor evaluating job opportunities.\n\nCandidate CV Summary:\n{cv_summary}\n\nCandidate work preference: {work_pref}",
            "cache_control": {"type": "ephemeral"}
        }
    ]
    for job in jobs[:max_jobs]:
        try:
            desc_block = f"\nJob snippet:\n{job['description']}" if job.get("description") else ""
            prompt = f"""Evaluate this job for the candidate:

Title:    {job['title']}
Company:  {job['company']}
Location: {job['location']} ({job['search_location']})
Source:   {job['source']}
URL:      {job['url']}{desc_block}

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
                system=system_prompt,
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

SOURCE_COLORS = {"Seek": "#0ea5e9", "Adzuna": "#059669", "Jooble": "#7c3aed", "Jooble (aggregator)": "#7c3aed",
                 "NZ Govt Jobs": "#1d4ed8",
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
    with smtplib.SMTP(*_get_smtp_settings(sender_email)) as server:
        server.ehlo()
        server.starttls()
        server.login(sender_email, smtp_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())


# ── Job Alert Email ──────────────────────────────────────────────────────────

def _build_alert_email(jobs, user_name=""):
    """Build a short urgent email for excellent matches."""
    cards = ""
    for job in jobs:
        cards += f"""
        <div style="background:#fff;border:2px solid #059669;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="background:#d1fae5;color:#065f46;font-size:13px;font-weight:700;padding:3px 10px;border-radius:20px;">
              {job.get('compatibility_score',0)}% · {job.get('compatibility_label','')}
            </span>
            <span style="font-size:12px;color:#6b7280;">{job.get('source','')} · {job.get('search_location','')}</span>
          </div>
          <h3 style="margin:0 0 4px;font-size:16px;">
            <a href="{job.get('url','#')}" style="color:#1d4ed8;text-decoration:none;">{job.get('title','')}</a>
          </h3>
          <p style="margin:0 0 8px;font-size:13px;color:#374151;">
            {job.get('company','')} · {job.get('location','')}
            {' · ' + job.get('salary_estimate','') if job.get('salary_estimate','') not in ('','N/A') else ''}
          </p>
          <a href="{job.get('url','#')}" style="background:#1d4ed8;color:#fff;padding:6px 16px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">Apply Now →</a>
        </div>"""

    return f"""<!DOCTYPE html><html><body style="font-family:-apple-system,sans-serif;background:#f8fafc;padding:20px;">
    <div style="max-width:600px;margin:0 auto;">
      <div style="background:#dc2626;color:#fff;padding:16px 20px;border-radius:10px 10px 0 0;">
        <h1 style="margin:0;font-size:20px;">🚨 Excellent Match Alert{' for ' + user_name if user_name else ''}</h1>
        <p style="margin:4px 0 0;font-size:13px;opacity:0.9;">{len(jobs)} role{'s' if len(jobs) > 1 else ''} scored 80%+ — act fast!</p>
      </div>
      <div style="background:#fff;padding:20px;border-radius:0 0 10px 10px;border:1px solid #e5e7eb;">
        {cards}
      </div>
      <p style="text-align:center;font-size:11px;color:#94a3b8;margin-top:12px;">&copy; 2026 Silver Fern Consulting Ltd</p>
    </div>
    </body></html>"""


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

def run_for_user(user, seen_fingerprints: set, progress_callback=None, stop_check=None) -> dict:
    """
    Run the full job search pipeline for one user.
    Returns a dict with results summary.
    """
    def progress(msg):
        log.info(msg)
        if progress_callback:
            progress_callback(msg)

    def should_stop():
        if stop_check and stop_check():
            progress("⏹️ Search stopped by user. Saving results collected so far...")
            return True
        return False

    # Get user's selected locations — FIX for Bug #26
    # Admin users: use checkbox selections. Regular users: auto-derive from country.
    COUNTRY_DEFAULT_LOCATIONS = {
        "New Zealand":    ["Wellington, NZ", "Auckland, NZ", "Christchurch, NZ"],
        "Australia":      ["Sydney, AU", "Melbourne, AU", "Brisbane, AU"],
        "Norway":         ["Norway"],
        "Denmark":        ["Denmark"],
        "Iceland":        ["Iceland"],
        "Canada":         ["Canada"],
        "United States":  ["United States"],
        "United Kingdom": ["United Kingdom"],
        "Germany":        ["Germany"],
        "Ireland":        ["Ireland"],
        "France":         ["France"],
        "Singapore":      ["Singapore"],
        "India":          ["India"],
        "Japan":          ["Japan"],
        "Malaysia":       ["Malaysia"],
        "Philippines":    ["Philippines"],
        "United Arab Emirates": ["United Arab Emirates"],
        "Netherlands":    ["Netherlands"],
        "Switzerland":    ["Switzerland"],
        "Hong Kong":      ["Hong Kong"],
    }

    is_admin = getattr(user, "is_admin", False)
    user_locs = user.locations if is_admin else []  # Regular users always use country defaults
    country = getattr(user, "default_country", "") or ""

    if user_locs:
        # Admin with manual location selections
        selected_loc_names = set(user_locs)
        progress(f"📍 Locations: {', '.join(selected_loc_names)}")
    elif country and country in COUNTRY_DEFAULT_LOCATIONS:
        selected_loc_names = set(COUNTRY_DEFAULT_LOCATIONS[country])
        progress(f"📍 Auto-locations for {country}: {', '.join(selected_loc_names)}")
    elif country:
        selected_loc_names = {country}
        progress(f"📍 Auto-locations: {country}")
    else:
        selected_loc_names = {"Wellington, NZ"}
        progress("⚠️ No country configured — defaulting to Wellington, NZ")

    locations = [l for l in ALL_LOCATIONS if l["name"] in selected_loc_names]

    # Build entries for any country-based locations not in ALL_LOCATIONS
    all_loc_names_set = {l["name"] for l in ALL_LOCATIONS}
    for loc_name in selected_loc_names:
        if loc_name not in all_loc_names_set:
            locations.append({
                "name":         loc_name,
                "seek_where":   "",
                "adzuna_country": "",
                "adzuna_where": "",
                "jooble_loc":   loc_name,
                "linkedin_loc": loc_name,
                "region":       "intl",
            })

    keywords  = user.keywords if user.keywords else []
    if not keywords:
        progress("⚠️ No keywords configured in profile — please add search keywords")
        return {"all_jobs": [], "scored": [], "relevant": 0}

    # Log the actual locations and keywords being used
    progress(f"📍 Locations: {', '.join(selected_loc_names)}")
    progress(f"🔑 Keywords: {', '.join(keywords)}")

    all_jobs      = []
    finn_done     = False
    jobindex_done = False
    jobicy_done   = set()

    # Determine which sources the user has enabled — FIX for Bug #4
    # selected_sources is now a @property that returns a parsed list directly
    _sel = user.selected_sources  # returns list, never a raw JSON string
    if not isinstance(_sel, list):
        _sel = []

    if _sel:
        _sel_lower = {s.lower() for s in _sel}
        use_seek     = any("seek" in s for s in _sel_lower)
        use_adzuna   = any("adzuna" in s for s in _sel_lower)
        use_linkedin = any("linkedin" in s for s in _sel_lower)
        use_jobicy   = any("jobicy" in s for s in _sel_lower)
        use_finn     = any("finn" in s for s in _sel_lower)
        use_jobindex = any("jobindex" in s for s in _sel_lower)
        use_jooble   = any("jooble" in s for s in _sel_lower)
        use_govtnz   = any("govt" in s or "government" in s for s in _sel_lower)
        progress(f"✅ Using {len(_sel)} selected sources: {', '.join(_sel)}")
    else:
        use_seek = use_adzuna = use_linkedin = use_jobicy = True
        use_finn = use_jobindex = True
        use_jooble = use_govtnz = True
        progress("⚠️ No sources selected in profile — using all defaults")

    # Resolve effective API keys (user key > admin shared key)
    def _get_effective_key(user_obj, attr):
        """User's own key if set, else admin's key."""
        val = getattr(user_obj, attr, "") or ""
        if val:
            return val
        try:
            from models import User as _U
            admin = _U.query.filter_by(is_admin=True).first()
            return getattr(admin, attr, "") or "" if admin else ""
        except Exception:
            return ""

    effective_anthropic   = _get_effective_key(user, 'anthropic_key')
    effective_adzuna_id   = _get_effective_key(user, 'adzuna_app_id')
    effective_adzuna_key  = _get_effective_key(user, 'adzuna_app_key')
    effective_jooble_key  = _get_effective_key(user, 'jooble_api_key')
    effective_smtp_pw     = _get_effective_key(user, 'smtp_password')
    effective_sender      = _get_effective_key(user, 'sender_email')

    total_locations = len(locations)
    total_keywords = len(keywords)
    progress(f"Searching {total_keywords} keywords across {total_locations} locations...")
    progress(f"PROGRESS:TOTAL:{total_locations * total_keywords}")

    # Log source availability
    if not effective_adzuna_id:
        progress("⚠️ Adzuna API key not set — skipping Adzuna source")
    if use_jooble and not effective_jooble_key:
        progress("⚠️ Jooble API key not set — skipping Jooble source (get free key at jooble.org/api/about)")
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

        # Bug #49: Skip locations where no enabled source would actually search
        loc_has_source = False
        if use_seek and location.get("seek_where"):
            loc_has_source = True
        if use_adzuna and location.get("adzuna_country") in ADZUNA_SUPPORTED:
            loc_has_source = True
        if use_linkedin and location.get("linkedin_loc"):
            loc_has_source = True
        if use_jooble and location.get("jooble_loc"):
            loc_has_source = True
        if use_govtnz and location.get("region") == "nzau":
            loc_has_source = True
        if use_finn and location.get("finn_rss"):
            loc_has_source = True
        if use_jobindex and location.get("jobindex_rss"):
            loc_has_source = True
        if use_jobicy:
            loc_has_source = True
        if not loc_has_source:
            progress(f"⏭️ Skipping {location['name']} — no enabled sources cover this location")
            continue

        kws = keywords[:4] if is_nordic else keywords

        for keyword in kws:
            if should_stop():
                break
            _step = keywords.index(keyword) + 1 if keyword in keywords else 1
            _loc_step = locations.index(location) + 1 if location in locations else 1
            progress(f"PROGRESS:STEP:{(_loc_step - 1) * len(keywords) + _step}")
            progress(f"Searching {location['name']} for '{keyword}'...")
            if use_seek:
                seek_results = scrape_seek(keyword, location)
                all_jobs.extend(seek_results)
                if seek_results:
                    progress(f"Seek [{location['name']}] '{keyword}' -> {len(seek_results)} jobs")
                time.sleep(1.0)

            if use_adzuna:
                adzuna_results = fetch_adzuna(keyword, location, effective_adzuna_id, effective_adzuna_key)
                all_jobs.extend(adzuna_results)
                if adzuna_results:
                    progress(f"Adzuna [{location['name']}] '{keyword}' -> {len(adzuna_results)} jobs")
                elif effective_adzuna_id and location.get('adzuna_country') in ADZUNA_SUPPORTED:
                    progress(f"Adzuna [{location['name']}] '{keyword}' -> 0 (API returned no results)")
                time.sleep(0.8)
            if use_linkedin:
                linkedin_results = search_linkedin_jobs(keyword, location)
                all_jobs.extend(linkedin_results)
                if linkedin_results:
                    progress(f"LinkedIn [{location['name']}] '{keyword}' -> {len(linkedin_results)} jobs")
                time.sleep(1.2)
            if use_jooble and effective_jooble_key:
                jooble_results = fetch_jooble(keyword, location, effective_jooble_key)
                all_jobs.extend(jooble_results)
                if jooble_results:
                    progress(f"Jooble (aggregator) [{location['name']}] '{keyword}' -> {len(jooble_results)} jobs")
                time.sleep(1.0)
            if use_govtnz:
                govt_results = scrape_govt_nz(keyword, location)
                all_jobs.extend(govt_results)
                if govt_results:
                    progress(f"NZ Govt Jobs [{location['name']}] '{keyword}' -> {len(govt_results)} jobs")
                time.sleep(0.8)
            if use_jobicy and keyword not in jobicy_done:
                jobicy_results = fetch_jobicy_rss(keyword)
                all_jobs.extend(jobicy_results)
                if jobicy_results:
                    progress(f"Jobicy '{keyword}' -> {len(jobicy_results)} jobs")
                jobicy_done.add(keyword)
                time.sleep(0.8)

        if use_finn and location.get("finn_rss") and not finn_done:
            for kw in kws:
                all_jobs.extend(fetch_finn_rss(kw)); time.sleep(0.8)
            finn_done = True

        if use_jobindex and location.get("jobindex_rss") and not jobindex_done:
            for kw in kws:
                all_jobs.extend(fetch_jobindex_rss(kw)); time.sleep(0.8)
            jobindex_done = True

        if should_stop():
            break

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

    if should_stop() and not new_jobs:
        return {"status": "stopped", "new_jobs": 0, "scored": 0, "relevant": 0, "emailed": False}

    # Log source/location breakdown
    source_counts = {}
    loc_counts    = {}
    for j in new_jobs:
        source_counts[j.get("source","?")] = source_counts.get(j.get("source","?"), 0) + 1
        loc_counts[j.get("search_location","?")] = loc_counts.get(j.get("search_location","?"), 0) + 1
    progress(f"By source: {source_counts}")
    progress(f"By location: {loc_counts}")

    # Round-robin selection: ensure fair representation across sources and locations
    # Instead of scoring the first N (biased toward Seek), interleave from each source
    import itertools
    from collections import defaultdict
    by_source = defaultdict(list)
    for j in new_jobs:
        by_source[j.get("source", "Unknown")].append(j)

    # Interleave: take one from each source in rotation until we hit max
    max_to_score = user.max_jobs_to_score or 25
    balanced = []
    source_iters = {k: iter(v) for k, v in by_source.items()}
    while len(balanced) < max_to_score and source_iters:
        exhausted = []
        for src, it in source_iters.items():
            if len(balanced) >= max_to_score:
                break
            try:
                balanced.append(next(it))
            except StopIteration:
                exhausted.append(src)
        for src in exhausted:
            del source_iters[src]

    # Log what we selected
    sel_sources = {}
    for j in balanced:
        sel_sources[j.get("source","?")] = sel_sources.get(j.get("source","?"), 0) + 1
    progress(f"Selected {len(balanced)} for scoring (balanced): {sel_sources}")
    progress("PROGRESS:SCORING")
    progress(f"Scoring {len(balanced)} jobs with Claude AI...")

    scored_jobs   = score_jobs(balanced, user.cv_summary, effective_anthropic, max_to_score, user.work_arrangement)
    relevant_jobs = [j for j in scored_jobs if j.get("compatibility_score", 0) >= user.score_threshold]
    progress(f"{len(relevant_jobs)} relevant jobs (score >= {user.score_threshold})")

    # Resolve notification preference BEFORE any email sending
    notify_pref = getattr(user, "notification_pref", "both") or "both"
    emails_sent_count = 0

    # Job alerts: send instant email for excellent matches (score >= 80)
    excellent_jobs = [j for j in relevant_jobs if j.get("compatibility_score", 0) >= 80]
    if excellent_jobs and getattr(user, "enable_job_alerts", True) and notify_pref in ("email", "both") and effective_sender and effective_smtp_pw and user.recipient_email:
        try:
            alert_html = _build_alert_email(excellent_jobs, user.full_name)
            from email.mime.multipart import MIMEMultipart as _MM
            from email.mime.text import MIMEText as _MT
            msg = _MM("alternative")
            msg["Subject"] = f"🚨 {len(excellent_jobs)} Excellent Match{'es' if len(excellent_jobs) > 1 else ''} Found!"
            msg["From"] = effective_sender
            msg["To"] = user.recipient_email
            msg.attach(_MT(alert_html, "html"))
            with smtplib.SMTP(*_get_smtp_settings(effective_sender)) as s:
                s.ehlo(); s.starttls()
                s.login(effective_sender, effective_smtp_pw)
                s.sendmail(effective_sender, user.recipient_email, msg.as_string())
            progress(f"🚨 Alert: {len(excellent_jobs)} excellent matches emailed instantly!")
            emails_sent_count += 1
        except Exception as e:
            progress(f"Alert email error: {e}")

    emailed = False
    if relevant_jobs and notify_pref in ("email", "both") and effective_sender and effective_smtp_pw and user.recipient_email:
        try:
            html = build_email_html(relevant_jobs, user.full_name)
            send_email(html, len(relevant_jobs), effective_sender, effective_smtp_pw,
                       user.recipient_email, user.full_name)
            emailed = True
            emails_sent_count += 1
            progress(f"Email sent to {user.recipient_email}")
        except Exception as e:
            progress(f"Email error: {e}")

    # Log usage stats (after all emails so we capture everything)
    try:
        from models import UsageLog
        from datetime import date as _date
        today = _date.today()
        usage = UsageLog.query.filter_by(user_id=user.id, date=today).first()
        if not usage:
            usage = UsageLog(user_id=user.id, date=today)
        else:
        usage.jobs_searched += len(new_jobs)
        usage.jobs_scored += len(scored_jobs)
        usage.api_calls += len(scored_jobs)
        usage.est_cost_usd += len(scored_jobs) * 0.002
        usage.emails_sent += emails_sent_count
        from models import db as _db
        _db.session.add(usage)
        _db.session.commit()
    except Exception as e:
        log.warning(f"Usage logging error: {e}")

    return {
        "status":        "ok",
        "new_jobs":      len(new_jobs),
        "scored":        len(scored_jobs),
        "relevant":      len(relevant_jobs),
        "emailed":       emailed,
        "scored_jobs":   scored_jobs,
        "relevant_jobs": relevant_jobs,
    }
