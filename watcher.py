#!/usr/bin/env python3
"""
Job Watcher — polls company ATS job feeds, finds NEW matching analyst roles,
and pushes alerts to your phone via ntfy.

Standard library only. Run:
    python watcher.py            # one polling cycle (what GitHub Actions runs every 5 min)
    python watcher.py --dry-run  # poll + print alerts, send nothing, save nothing
    python watcher.py --validate # check every entry in companies.txt, write state/validation.md
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
SEEN_PATH = STATE_DIR / "seen.json"
ALERTS_PATH = STATE_DIR / "alerts.jsonl"
HEALTH_PATH = STATE_DIR / "health.json"
UTC = dt.timezone.utc
UA = "Mozilla/5.0 (job-watcher; personal job alerts)"
TIMEOUT = 20
SEEN_RETENTION_DAYS = 60

# --------------------------------------------------------------------------- config

def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    flags = re.IGNORECASE
    cfg["_title_inc"] = [re.compile(p, flags) for p in cfg["titles"]["include"]]
    cfg["_title_exc"] = [re.compile(p, flags) for p in cfg["titles"]["exclude"]]
    for sect in ("skills", "gaps", "flags"):
        cfg["_" + sect] = {k: [re.compile(p, flags) for p in v] for k, v in cfg[sect].items()}
    return cfg


def load_companies() -> list[dict]:
    out = []
    for raw in (ROOT / "companies.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        ats, rest = line.split(":", 1)
        parts = [p.strip() for p in rest.split("|")]
        ent = {"ats": ats.strip().lower(), "raw": line}
        if ent["ats"] == "workday":  # workday:host|tenant|site|Display Name
            if len(parts) < 3:
                continue
            ent.update(host=parts[0], tenant=parts[1], site=parts[2],
                       name=parts[3] if len(parts) > 3 else parts[1].title())
            ent["slug"] = f"{parts[1]}/{parts[2]}"
        else:
            ent.update(slug=parts[0], name=parts[1] if len(parts) > 1 else None)
        out.append(ent)
    return out

# --------------------------------------------------------------------------- http

def http_json(url: str, data: dict | None = None, retries: int = 1):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 400, 401, 403):
                raise
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


def parse_ts(v) -> dt.datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):  # epoch ms (Lever)
        return dt.datetime.fromtimestamp(v / 1000, tz=UTC)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)

# --------------------------------------------------------------------------- job model

def make_job(**kw) -> dict:
    job = dict(company="", title="", location="", workplace="", url="", source="", ats="",
               job_id="", posted_at=None, ts_note="", description="", salary="",
               needs_detail=False, detail=None)
    job.update(kw)
    return job

# --------------------------------------------------------------------------- fetchers
# Each returns a list of job dicts. Descriptions may be filled later by `detail`.

def fetch_greenhouse(c: dict) -> list[dict]:
    slug = c["slug"]
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    name = c.get("name") or slug.title()
    jobs = []
    for j in data.get("jobs", []):
        jid = j.get("id")
        jobs.append(make_job(
            company=name, title=j.get("title", "").strip(),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""), source="Greenhouse (official ATS)", ats="greenhouse",
            job_id=str(jid), posted_at=parse_ts(j.get("first_published")),
            ts_note="Greenhouse first_published", needs_detail=True,
            detail=f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}?pay_transparency=true"))
    return jobs


def detail_greenhouse(job: dict) -> None:
    d = http_json(job["detail"])
    job["description"] = strip_html(d.get("content"))
    pay = d.get("pay_input_ranges") or []
    if pay:
        p = pay[0]
        try:
            lo, hi = int(p["min_cents"]) // 100, int(p["max_cents"]) // 100
            job["salary"] = f"${lo:,} – ${hi:,} {p.get('currency_type', '')}".strip()
        except (KeyError, ValueError, TypeError):
            pass


def fetch_lever(c: dict) -> list[dict]:
    slug = c["slug"]
    data = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    name = c.get("name") or slug.title()
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        lists = "\n".join(f"{x.get('text','')}\n{strip_html(x.get('content'))}" for x in j.get("lists") or [])
        desc = "\n".join(filter(None, [j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")]))
        sal = ""
        sr = j.get("salaryRange") or {}
        if sr.get("min"):
            sal = f"${sr['min']:,} – ${sr.get('max', sr['min']):,} {sr.get('currency','')} {sr.get('interval','')}".strip()
        locs = cats.get("allLocations") or [cats.get("location", "")]
        jobs.append(make_job(
            company=name, title=j.get("text", "").strip(),
            location=" / ".join(filter(None, locs)) + (f" ({j['country']})" if j.get("country") else ""),
            workplace=(j.get("workplaceType") or "").replace("unspecified", ""),
            url=j.get("hostedUrl", ""), source="Lever (official ATS)", ats="lever",
            job_id=j.get("id", ""), posted_at=parse_ts(j.get("createdAt")),
            ts_note="Lever createdAt", description=desc, salary=sal))
    return jobs


def fetch_ashby(c: dict) -> list[dict]:
    slug = c["slug"]
    data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    name = c.get("name") or slug.title()
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
        wp = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
        jobs.append(make_job(
            company=name, title=j.get("title", "").strip(),
            location=" / ".join(filter(None, locs)) + (f" ({country})" if country else ""),
            workplace=wp, url=j.get("jobUrl", ""), source="Ashby (official ATS)", ats="ashby",
            job_id=j.get("id", ""), posted_at=parse_ts(j.get("publishedAt")),
            ts_note="Ashby publishedAt", description=j.get("descriptionPlain", "") or strip_html(j.get("descriptionHtml")),
            salary=comp))
    return jobs


def fetch_smartrecruiters(c: dict) -> list[dict]:
    slug = c["slug"]
    jobs = []
    for q in ("analyst", "coordinator", "supply chain"):
        url = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?"
               + urllib.parse.urlencode({"q": q, "country": "us", "limit": 100}))
        data = http_json(url)
        for j in data.get("content", []):
            loc = j.get("location") or {}
            locs = ", ".join(filter(None, [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]))
            wp = "Remote" if loc.get("remote") else ("Hybrid" if loc.get("hybrid") else "")
            jobs.append(make_job(
                company=(j.get("company") or {}).get("name") or c.get("name") or slug,
                title=j.get("name", "").strip(), location=locs, workplace=wp,
                url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                source="SmartRecruiters (official ATS)", ats="smartrecruiters", job_id=str(j.get("id")),
                posted_at=parse_ts(j.get("releasedDate")), ts_note="SmartRecruiters releasedDate",
                needs_detail=True, detail=j.get("ref")))
    uniq = {j["job_id"]: j for j in jobs}
    return list(uniq.values())


def detail_smartrecruiters(job: dict) -> None:
    d = http_json(job["detail"])
    secs = ((d.get("jobAd") or {}).get("sections") or {})
    job["description"] = "\n".join(strip_html((secs.get(k) or {}).get("text"))
                                   for k in ("jobDescription", "qualifications", "additionalInformation"))
    if d.get("applyUrl"):
        job["url"] = d["applyUrl"]


def fetch_workday(c: dict) -> list[dict]:
    base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}"
    jobs = {}
    for q in ("analyst", "supply chain", "coordinator"):
        data = http_json(base + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q})
        for j in data.get("jobPostings", []):
            path = j.get("externalPath", "")
            posted = (j.get("postedOn") or "")
            if not path or "today" not in posted.lower():
                # Workday only exposes "Posted Today / Yesterday / N Days Ago" — no exact timestamp.
                # We only consider "Posted Today" jobs that we haven't seen before.
                continue
            jid = (j.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]
            jobs[path] = make_job(
                company=c["name"], title=j.get("title", "").strip(), location=j.get("locationsText", ""),
                url=f"https://{c['host']}/en-US/{c['site']}{path}", source="Workday (official ATS)",
                ats="workday", job_id=str(jid), posted_at=None,
                ts_note=f"timestamp unavailable (Workday says '{posted}')",
                needs_detail=True, detail=base + path)
    return list(jobs.values())


def detail_workday(job: dict) -> None:
    d = http_json(job["detail"]).get("jobPostingInfo", {})
    job["description"] = strip_html(d.get("jobDescription"))
    if d.get("externalUrl"):
        job["url"] = d["externalUrl"]
    if d.get("remoteType"):
        job["workplace"] = d["remoteType"]


def fetch_adzuna(c: dict) -> list[dict]:
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and key):
        return []
    params = {"app_id": app_id, "app_key": key, "results_per_page": 50, "sort_by": "date",
              "max_days_old": 1, "title_only": c.get("slug") or "analyst", "content-type": "application/json"}
    data = http_json("https://api.adzuna.com/v1/api/jobs/us/search/1?" + urllib.parse.urlencode(params))
    jobs = []
    for j in data.get("results", []):
        sal = ""
        if j.get("salary_min"):
            sal = f"${int(j['salary_min']):,} – ${int(j.get('salary_max') or j['salary_min']):,}"
            if j.get("salary_is_predicted") == "1":
                sal += " (Adzuna estimate, not listed)"
        jobs.append(make_job(
            company=(j.get("company") or {}).get("display_name", ""), title=strip_html(j.get("title")),
            location=(j.get("location") or {}).get("display_name", "") + ", US",
            url=j.get("redirect_url", ""), source="Adzuna (aggregator)", ats="adzuna",
            job_id=str(j.get("id")), posted_at=parse_ts(j.get("created")),
            ts_note="Adzuna 'created' = when Adzuna indexed it; original post may be earlier",
            description=strip_html(j.get("description")), salary=sal))
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday, "adzuna": fetch_adzuna}
DETAILS = {"greenhouse": detail_greenhouse, "smartrecruiters": detail_smartrecruiters, "workday": detail_workday}

# --------------------------------------------------------------------------- filters

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
US_CITIES = ("new york", "nyc", "san francisco", "los angeles", "seattle", "chicago", "boston", "austin",
             "atlanta", "denver", "dallas", "houston", "phoenix", "san diego", "san jose", "miami",
             "philadelphia", "pittsburgh", "washington", "salt lake", "portland", "minneapolis",
             "nashville", "charlotte", "raleigh", "detroit", "columbus", "tampa", "orlando", "irvine",
             "palo alto", "mountain view", "sunnyvale", "menlo park", "redwood city", "oakland",
             "santa monica", "carlsbad", "brooklyn", "jersey city", "st. louis", "kansas city",
             "indianapolis", "cincinnati", "cleveland", "baltimore", "richmond", "memphis", "louisville")
NON_US = ("canada", "toronto", "vancouver", "montreal", "ontario", "mexico", "brazil", "são paulo",
          "argentina", "colombia", "united kingdom", " uk", "london", "england", "ireland", "dublin",
          "germany", "berlin", "munich", "france", "paris", "spain", "madrid", "barcelona", "portugal",
          "lisbon", "netherlands", "amsterdam", "poland", "warsaw", "india", "bangalore", "bengaluru",
          "hyderabad", "pune", "mumbai", "delhi", "gurgaon", "noida", "chennai", "singapore", "japan",
          "tokyo", "australia", "sydney", "melbourne", "philippines", "manila", "israel", "tel aviv",
          "emea", "apac", "latam", "europe", "sweden", "stockholm", "switzerland", "zurich", "china",
          "shanghai", "hong kong", "korea", "seoul", "taiwan", "vietnam", "costa rica", "romania",
          "czech", "prague", "italy", "milan", "belgium", "denmark", "norway", "finland", "austria",
          "south africa", "nigeria", "kenya", "egypt", "uae", "dubai", "saudi", "new zealand")


def is_us(job: dict) -> bool:
    # Explicit trailing country code added from ATS metadata, e.g. "Toronto (CA)" or "Austin, TX (US)".
    cc = re.search(r"\(([A-Z]{2,3})\)\s*$", job["location"] or "")
    if cc and job.get("ats") in ("lever", "ashby") and cc.group(1) not in ("US", "USA", "HQ") and not re.search(r"\(US|USA\)", job["location"]):
        return False
    loc = f" {job['location']} ".lower()
    if re.search(r"united states|\busa\b|\bu\.s\.?\b|\bus\b|\(us\)|, us\b|americas|north america", loc):
        us_hint = True
    else:
        us_hint = False
    if not us_hint:
        for ab, full in US_STATES.items():
            if re.search(rf"(,|\s|-)\s*{ab}\b", job["location"]) or full.lower() in loc:
                us_hint = True
                break
    if not us_hint and any(c in loc for c in US_CITIES):
        us_hint = True
    non_us = any(n in loc for n in NON_US)
    if us_hint:
        return True  # multi-location posts that include a US site are fine
    if non_us:
        return False
    # Bare "Remote" / empty location: keep only if the description doesn't say it's non-US.
    text = job.get("description", "").lower()[:4000]
    if re.search(r"(based in|located in|resident of|must reside in) (canada|the uk|india|europe|mexico)", text):
        return False
    return "remote" in loc or loc.strip() == "" or job["ats"] == "workday"


def title_ok(cfg: dict, title: str) -> bool:
    if any(p.search(title) for p in cfg["_title_exc"]):
        return False
    return any(p.search(title) for p in cfg["_title_inc"])


CORE_TITLE = re.compile(r"business (systems |data |intelligence |operations |process )?analyst|\bbi analyst|"
                        r"data analyst|operations analyst|supply chain (data |planning |operations |business )?analyst|"
                        r"procurement analyst|inventory analyst|demand plann|reporting analyst|"
                        r"process improvement analyst|analytics analyst|business intelligence", re.I)


def extract_years(text: str) -> tuple[int | None, str]:
    """Return (minimum years mentioned in an experience requirement, human string)."""
    best = None
    phrase = ""
    for m in re.finditer(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
                         r"(?:'|’)?\s*(?:of\s+)?(?:[a-z/,&\- ]{0,60}?)experience", text, re.I):
        lo = int(m.group(1))
        if lo > 20:
            continue
        if best is None or lo < best:
            best = lo
            hi = m.group(2)
            phrase = f"{lo}–{hi} years" if hi else f"{lo}+ years"
    return best, (phrase or "Not specified")


def detect_workplace(job: dict) -> str:
    w = (job.get("workplace") or "").lower()
    blob = (job["location"] + " " + job.get("description", "")[:3000]).lower()
    if "hybrid" in w or re.search(r"\bhybrid\b", blob):
        return "Hybrid"
    if "remote" in w or "remote" in job["location"].lower():
        return "Remote"
    if "onsite" in w or "on-site" in w or "office" in w:
        return "Onsite"
    if re.search(r"\b(on-?site|in[- ]office)\b", blob):
        return "Onsite"
    return "Not stated"


SAL_RE = re.compile(r"\$\s?\d{2,3}(?:,\d{3}|k)(?:\.\d+)?\s*(?:-|–|to)\s*\$?\s?\d{2,3}(?:,\d{3}|k)", re.I)
HOURLY_RE = re.compile(r"\$\s?\d{2}(?:\.\d{2})?\s*(?:-|–|to)\s*\$?\s?\d{2}(?:\.\d{2})?\s*(?:/|per)\s*(?:hour|hr)", re.I)


def score(cfg: dict, job: dict) -> dict:
    text = f"{job['title']}\n{job.get('description','')}"
    matched = [k for k, pats in cfg["_skills"].items() if any(p.search(text) for p in pats)]
    gaps = [k for k, pats in cfg["_gaps"].items() if any(p.search(text) for p in pats)]
    flags = [k for k, pats in cfg["_flags"].items() if any(p.search(text) for p in pats)]
    years, years_txt = extract_years(job.get("description", ""))

    heavy = {"SQL", "Power BI", "Python", "Excel", "Supply Chain", "Procurement", "Inventory",
             "Forecasting", "Process Improvement", "Business Analysis", "Data Analysis", "Tableau",
             "Business Intelligence", "KPI Development", "SAP", "NetSuite", "Snowflake"}
    s = 35 if CORE_TITLE.search(job["title"]) else 25
    s += min(45, sum(6 if m in heavy else 3 for m in matched))
    if years is None or years <= cfg["experience"]["max_years"]:
        s += 15
    elif years <= cfg["experience"]["max_years"] + 1:
        s += 5
    else:
        s -= 15
    t = job["title"].lower()
    if re.search(r"\b(senior|sr\.?)\b", t):
        s -= 5
    if re.search(r"\b(junior|jr\.?|associate|entry|early career|new grad)\b|\bi\b$| i$", t):
        s += 5
    s -= min(10, 2 * len(gaps))
    if not job.get("description"):
        s -= 5  # couldn't read description; less confident
    s = max(0, min(100, s))

    salary = job.get("salary") or ""
    if not salary:
        m = SAL_RE.search(job.get("description", "")) or HOURLY_RE.search(job.get("description", ""))
        salary = m.group(0) if m else "Not listed"

    return dict(score=s, matched=matched, gaps=gaps, flags=flags, years=years, years_txt=years_txt,
                salary=salary, workplace=detect_workplace(job))


def why_lines(job: dict, sc: dict) -> list[str]:
    lines = []
    lines.append(f"Title \"{job['title']}\" is in your target analyst categories")
    core = [m for m in sc["matched"] if m in ("SQL", "Power BI", "Python", "Excel", "Tableau", "DAX",
                                               "Power Query", "Snowflake", "SAP", "NetSuite", "Salesforce")]
    domain = [m for m in sc["matched"] if m in ("Supply Chain", "Procurement", "Inventory", "Forecasting",
                                                 "Process Improvement", "Lean Six Sigma", "Root Cause Analysis",
                                                 "KPI Development", "Business Analysis", "Requirements Analysis",
                                                 "Process Mapping", "Operations Analysis", "Data Validation")]
    if core:
        lines.append("Posting asks for tools you use: " + ", ".join(core))
    if domain:
        lines.append("Responsibilities overlap your experience: " + ", ".join(domain))
    if sc["years"] is None:
        lines.append("No minimum years stated")
    else:
        lines.append(f"Experience ask ({sc['years_txt']}) vs. your analyst background — check fit")
    return lines[:4]

# --------------------------------------------------------------------------- state

def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedup_key(job: dict) -> str:
    """Cross-source key so the same role on Adzuna + the company ATS is treated as one job."""
    company = re.sub(r"\b(inc|llc|corp(oration)?|co|ltd|company|the)\b", "", norm(job["company"])).strip()
    title = re.sub(r"\s*\(.*?\)\s*", " ", job["title"])
    title = norm(re.sub(r"\s+-\s+.*$", "", title))
    city = norm(re.split(r"[,/;|]", job["location"] or "")[0])
    return f"{company}|{title}|{city}"


def source_key(job: dict) -> str:
    return f"{job['ats']}:{job['job_id']}"

# --------------------------------------------------------------------------- alerting

def priority_for(age_min: float | None) -> str:
    if age_min is None:
        return "NEW (posting time unavailable)"
    if age_min <= 5:
        return "URGENT"
    if age_min <= 15:
        return "VERY NEW"
    if age_min <= 60:
        return "RECENT"
    return "OLDER — unusually strong match"


def fmt_local(d: dt.datetime | None, tz: str) -> str:
    if d is None:
        return "timestamp unavailable"
    try:
        from zoneinfo import ZoneInfo
        loc = d.astimezone(ZoneInfo(tz))
        return loc.strftime("%a %b %d, %Y %I:%M %p %Z")
    except Exception:  # noqa: BLE001
        return d.strftime("%Y-%m-%d %H:%M UTC")


def build_alert(job: dict, sc: dict, now: dt.datetime, cfg: dict) -> dict:
    tz = cfg.get("alerting", {}).get("timezone", "America/Los_Angeles")
    age = None if job["posted_at"] is None else (now - job["posted_at"]).total_seconds() / 60
    prio = priority_for(age)
    level = "High" if sc["score"] >= cfg["alerting"]["high_score"] else "Moderate"
    strong = sc["score"] >= cfg["alerting"]["strong_match_score"]
    posted_txt = fmt_local(job["posted_at"], tz)
    age_txt = (f"{int(age)} minutes" if age is not None and age < 120
               else f"{age/60:.1f} hours" if age is not None else "unknown — first detected " + fmt_local(now, tz))

    lines = ["🚨 NEW JOB ALERT", "",
             f"Posted: {posted_txt}", f"Age: {age_txt}", f"Priority: {prio}", "",
             "Company:", job["company"], "",
             "Position:", job["title"], "",
             "Location:", job["location"] or "Not stated", sc["workplace"], "",
             "Salary:", sc["salary"], "",
             "Match:", f"{level} (score {sc['score']}/100)", "",
             "Why it matches:"] + [f"• {w}" for w in why_lines(job, sc)] + ["",
             "Key skills:", ", ".join(sc["matched"]) or "(description not available)", "",
             "Experience required:", sc["years_txt"], "",
             "Application:", job["url"], "",
             "Source:", job["source"], "",
             "Posted:", f"{posted_txt} ({job['ts_note']})",
             "Detected:", fmt_local(now, tz)]
    if sc["flags"]:
        lines += ["", "⚠️ Verify (unknown about you, not assumed): " + ", ".join(sc["flags"])]
    if strong:
        tailor = ("Likely yes — address: " + ", ".join(sc["gaps"])) if sc["gaps"] else \
                 ("Light tailoring: mirror these keywords in your summary/bullets" if len(sc["matched"]) >= 6
                  else "Yes — posting emphasizes skills beyond what's detected; review description")
        lines += ["", "🔥 STRONG MATCH",
                  "ATS keywords: " + ", ".join(sc["matched"][:15]),
                  "Missing / not on your profile: " + (", ".join(sc["gaps"]) or "none detected"),
                  "Resume tailoring: " + tailor,
                  "Apply: " + job["url"]]
    title = ("🔥 " if strong else "") + f"{prio}: {job['title']} — {job['company']}"
    return dict(title=title[:250], body="\n".join(lines), priority=prio, level=level, strong=strong,
                age_min=None if age is None else round(age, 1))


def send_ntfy(alert: dict, url: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("  (NTFY_TOPIC not set — alert printed only)")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    prio_num = {"URGENT": 5, "VERY NEW": 5, "RECENT": 4}.get(alert["priority"], 4)
    payload = {"topic": topic, "title": alert["title"], "message": alert["body"][:3900],
               "priority": prio_num, "tags": ["rotating_light"] + (["fire"] if alert["strong"] else []),
               "click": url, "actions": [{"action": "view", "label": "Open posting", "url": url}]}
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    req = urllib.request.Request(server, data=json.dumps(payload).encode(), headers=headers)
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def send_plain(title: str, body: str, priority: int = 3, tags=None) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print(title + "\n" + body)
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": body[:3900], "priority": priority,
               "tags": tags or []}
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    urllib.request.urlopen(urllib.request.Request(server, data=json.dumps(payload).encode(),
                                                  headers=headers), timeout=TIMEOUT).read()

# --------------------------------------------------------------------------- main cycle

def run(dry_run: bool = False, now: dt.datetime | None = None, companies=None, fetchers=None,
        details=None) -> list[dict]:
    cfg = load_config()
    now = now or dt.datetime.now(UTC)
    companies = companies if companies is not None else load_companies()
    fetchers = fetchers or FETCHERS
    details = details or DETAILS
    seen = load_json(SEEN_PATH, {"keys": {}, "boards": {}})
    health = load_json(HEALTH_PATH, {"boards": {}})
    max_age = cfg["alerting"]["max_age_minutes"]
    strong_age = cfg["alerting"]["strong_match_max_age_minutes"]

    # Adzuna is rate-limited (free tier) — poll it at most every 30 min.
    last_adz = parse_ts(seen.get("last_adzuna"))
    poll_adzuna = last_adz is None or (now - last_adz).total_seconds() >= 29 * 60
    todo = [c for c in companies if c["ats"] != "adzuna" or poll_adzuna]

    t0 = time.time()
    results: dict[str, list[dict]] = {}

    def _fetch(c):
        return c, fetchers[c["ats"]](c)

    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        futs = [ex.submit(_fetch, c) for c in todo if c["ats"] in fetchers]
        for f in cf.as_completed(futs):
            try:
                c, jobs = f.result()
                results[c["raw"]] = jobs
            except Exception:  # noqa: BLE001
                pass  # recorded below as a failed board
    failed = []
    for c in todo:
        if c["ats"] in fetchers and c["raw"] not in results:
            failed.append(c["raw"])
    if any(c["ats"] == "adzuna" for c in todo):
        seen["last_adzuna"] = now.isoformat()

    # ---- candidate selection
    candidates = []
    for c in todo:
        if c["raw"] not in results:
            continue
        first_time_board = c["raw"] not in seen["boards"]
        for job in results[c["raw"]]:
            sk = source_key(job)
            if sk in seen["keys"]:
                continue
            dk = dedup_key(job)
            if dk in seen["keys"]:
                seen["keys"][sk] = {"t": now.isoformat(), "dup_of": dk}
                continue
            # Not seen before.
            reliable = job["posted_at"] is not None
            age = (now - job["posted_at"]).total_seconds() / 60 if reliable else None
            if not reliable and first_time_board:
                # First look at a board with no timestamps: baseline silently (we can't know it's new).
                seen["keys"][sk] = {"t": now.isoformat(), "baseline": True}
                continue
            if reliable and age > strong_age:
                seen["keys"][sk] = {"t": now.isoformat(), "old": True}
                continue
            if not title_ok(cfg, job["title"]):
                seen["keys"][sk] = {"t": now.isoformat(), "skip": "title"}
                continue
            candidates.append((c, job, age))
        seen["boards"][c["raw"]] = now.isoformat()

    # ---- details for candidates (only the few that passed title/age)
    def _detail(item):
        c, job, age = item
        if job.get("needs_detail") and job["ats"] in details:
            try:
                details[job["ats"]](job)
            except Exception as e:  # noqa: BLE001
                job["detail_error"] = repr(e)[:120]
        return item

    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        candidates = list(ex.map(_detail, candidates))

    alerts = []
    for c, job, age in candidates:
        sk, dk = source_key(job), dedup_key(job)
        if not is_us(job):
            seen["keys"][sk] = {"t": now.isoformat(), "skip": "non-US"}
            continue
        sc = score(cfg, job)
        hard = cfg["experience"]["hard_max_years"]
        too_senior = sc["years"] is not None and sc["years"] > cfg["experience"]["max_years"] and \
            (sc["years"] > hard or sc["score"] < cfg["alerting"]["strong_match_score"])
        too_old = age is not None and age > max_age and sc["score"] < cfg["alerting"]["strong_match_score"]
        if too_senior or too_old or sc["score"] < cfg["alerting"]["min_score"]:
            reason = "senior" if too_senior else "old" if too_old else f"score {sc['score']}"
            seen["keys"][sk] = {"t": now.isoformat(), "skip": reason}
            continue
        alert = build_alert(job, sc, now, cfg)
        rec = {"alerted_at": now.isoformat(), "company": job["company"], "title": job["title"],
               "job_id": job["job_id"], "location": job["location"], "workplace": sc["workplace"],
               "url": job["url"], "posted_at": job["posted_at"].isoformat() if job["posted_at"] else None,
               "age_min": alert["age_min"], "priority": alert["priority"], "source": job["source"],
               "match": alert["level"], "score": sc["score"], "strong": alert["strong"],
               "skills": sc["matched"], "gaps": sc["gaps"], "flags": sc["flags"],
               "salary": sc["salary"], "years": sc["years_txt"]}
        alerts.append((alert, rec, sk, dk))

    # Same job found on several sources this cycle -> keep ONE, preferring the official ATS posting.
    by_dk: dict[str, tuple] = {}
    dupes = []
    for a in alerts:
        dk = a[3]
        cur = by_dk.get(dk)
        if cur is None:
            by_dk[dk] = a
            continue
        a_official = "aggregator" not in a[1]["source"]
        cur_official = "aggregator" not in cur[1]["source"]
        if a_official and not cur_official:
            dupes.append(cur)
            by_dk[dk] = a
        else:
            dupes.append(a)
    for _, rec, sk, dk in dupes:
        seen["keys"][sk] = {"t": now.isoformat(), "dup_of": dk}
    alerts = list(by_dk.values())

    # Strongest / newest first
    alerts.sort(key=lambda a: (-a[1]["score"], a[1]["age_min"] if a[1]["age_min"] is not None else 999))
    sent = []
    for alert, rec, sk, dk in alerts:
        print("\n" + "=" * 60 + "\n" + alert["body"])
        if not dry_run:
            try:
                send_ntfy(alert, rec["url"])
            except Exception as e:  # noqa: BLE001
                print(f"  !! ntfy send failed: {e!r} — will retry next cycle")
                continue  # don't mark as seen, so it retries
            seen["keys"][sk] = {"t": now.isoformat(), "alerted": True}
            seen["keys"][dk] = {"t": now.isoformat(), "alerted": True, "src": sk}
            with ALERTS_PATH.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        sent.append(rec)

    # ---- prune + health
    cutoff = now - dt.timedelta(days=SEEN_RETENTION_DAYS)
    seen["keys"] = {k: v for k, v in seen["keys"].items() if (parse_ts(v.get("t")) or now) >= cutoff}
    for c in todo:
        b = health["boards"].setdefault(c["raw"], {"fail_streak": 0})
        if c["raw"] in failed:
            b["fail_streak"] += 1
            b["last_error_at"] = now.isoformat()
        else:
            b["fail_streak"] = 0
            b["last_ok_at"] = now.isoformat()
            b["jobs"] = len(results.get(c["raw"], []))
    health.update(last_run=now.isoformat(), duration_s=round(time.time() - t0, 1),
                  boards_ok=len(results), boards_failed=len(failed), alerts_sent=len(sent))
    print(f"\nCycle done: {len(results)} boards ok, {len(failed)} failed, "
          f"{len(candidates)} candidates, {len(sent)} alerts, {health['duration_s']}s")
    if failed:
        print("Failed boards: " + ", ".join(failed[:30]) + (" …" if len(failed) > 30 else ""))

    if not dry_run:
        STATE_DIR.mkdir(exist_ok=True)
        SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))  # one key per line = small git diffs
        HEALTH_PATH.write_text(json.dumps(health, indent=1))
        # If most sources are failing, say so rather than silently failing.
        if todo and len(failed) > 0.5 * len(todo):
            try:
                send_plain("⚠️ Job watcher degraded",
                           f"{len(failed)} of {len(todo)} job sources failed this cycle. "
                           "Monitoring is running but coverage is reduced.", 4, ["warning"])
            except Exception:  # noqa: BLE001
                pass
    return sent

# --------------------------------------------------------------------------- validate

def validate(prune: bool = False) -> None:
    companies = load_companies()
    rows = []

    def _chk(c):
        try:
            jobs = FETCHERS[c["ats"]](c)
            return c, "ok", len(jobs), ""
        except Exception as e:  # noqa: BLE001
            return c, "FAIL", 0, repr(e)[:120]

    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for c, st, n, err in ex.map(_chk, [c for c in companies if c["ats"] in FETCHERS]):
            rows.append((c["raw"], st, n, err))
    rows.sort(key=lambda r: (r[1] != "FAIL", r[0]))
    ok = sum(1 for r in rows if r[1] == "ok")
    md = [f"# Source validation — {dt.datetime.now(UTC):%Y-%m-%d %H:%M UTC}", "",
          f"{ok} of {len(rows)} sources OK. Remove or fix FAIL lines in companies.txt.", "",
          "| Source | Status | Jobs | Error |", "|---|---|---|---|"]
    md += [f"| `{r[0]}` | {r[1]} | {r[2]} | {r[3]} |" for r in rows]
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "validation.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    if prune:
        bad = {r[0] for r in rows if r[1] == "FAIL" and not r[0].startswith("adzuna:")}
        src = ROOT / "companies.txt"
        out = []
        for raw in src.read_text().splitlines():
            key = raw.split("#", 1)[0].strip()
            out.append(f"# FAILED VALIDATION: {raw}" if key in bad else raw)
        src.write_text("\n".join(out) + "\n")
        print(f"Commented out {len(bad)} failing sources in companies.txt")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--prune", action="store_true", help="with --validate: comment out failing sources")
    a = ap.parse_args()
    if a.validate:
        validate(prune=a.prune)
    else:
        try:
            run(dry_run=a.dry_run)
        except Exception as e:  # noqa: BLE001
            print(f"FATAL: {e!r}")
            try:
                send_plain("🛑 Job watcher crashed", f"Monitoring cycle failed: {e!r}\nCheck GitHub Actions logs.",
                           5, ["warning"])
            finally:
                sys.exit(1)
