"""Offline tests: python -m pytest -q tests/  (or python tests/test_watcher.py)"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher as W  # noqa: E402
import summary as S  # noqa: E402
import add_company as A  # noqa: E402

NOW = dt.datetime(2026, 9, 24, 18, 0, tzinfo=dt.timezone.utc)
BA_DESC = ("We need a Business Analyst to gather business requirements, build Power BI dashboards and "
           "write SQL queries. Advanced Excel (pivot tables, Power Query). Partner with cross-functional "
           "stakeholders, define KPIs, drive process improvement and root cause analysis. "
           "2-4 years of experience in data analysis or business analysis. Salary: $85,000 - $105,000. Hybrid.")
SC_DESC = ("Supply Chain Analyst: demand planning and forecasting, inventory optimization, procurement and supplier "
           "performance KPIs, SAP and Excel, SQL a plus. 3+ years of supply chain experience. Remote (US).")


def mins_ago(m):
    return NOW - dt.timedelta(minutes=m)


def gh_jobs(_c):
    J = W.make_job
    return [
        J(company="Acme", title="Business Analyst", location="San Francisco, CA", url="https://acme/1",
          source="Greenhouse (official ATS)", ats="greenhouse", job_id="1", posted_at=mins_ago(3),
          ts_note="Greenhouse first_published", description=BA_DESC),
        J(company="Acme", title="Supply Chain Analyst", location="Remote - US", url="https://acme/2",
          source="Greenhouse (official ATS)", ats="greenhouse", job_id="2", posted_at=mins_ago(10),
          ts_note="t", description=SC_DESC),
        J(company="Acme", title="Data Analyst", location="Toronto, Ontario, Canada", url="https://acme/3",
          source="g", ats="greenhouse", job_id="3", posted_at=mins_ago(40), ts_note="t", description=BA_DESC),
        J(company="Acme", title="Senior Manager, Business Intelligence", location="Austin, TX", url="u4",
          source="g", ats="greenhouse", job_id="4", posted_at=mins_ago(20), ts_note="t", description=BA_DESC),
        J(company="Acme", title="Senior Data Analyst", location="Chicago, IL", url="u5", source="g",
          ats="greenhouse", job_id="5", posted_at=mins_ago(30), ts_note="t",
          description="SQL, Tableau. 8+ years of experience in analytics required."),
        J(company="Acme", title="Operations Analyst", location="Dallas, TX", url="u6", source="g",
          ats="greenhouse", job_id="6", posted_at=mins_ago(180), ts_note="t",
          description="Weekly reporting. Some Excel. 5+ years of experience."),
        J(company="Acme", title="Software Engineer", location="Seattle, WA", url="u7", source="g",
          ats="greenhouse", job_id="7", posted_at=mins_ago(2), ts_note="t", description="Go, k8s"),
        J(company="Acme", title="Procurement Analyst", location="Phoenix, AZ", url="u8", source="g",
          ats="greenhouse", job_id="8", posted_at=mins_ago(3000), ts_note="t", description=SC_DESC),
    ]


def adz_jobs(_c):
    # Same role as Acme job 1, seen via the aggregator -> must dedupe to ONE alert.
    return [W.make_job(company="Acme, Inc.", title="Business Analyst", location="San Francisco, California, US",
                       url="https://adzuna/x", source="Adzuna (aggregator)", ats="adzuna", job_id="a1",
                       posted_at=mins_ago(1), ts_note="t", description=BA_DESC)]


WD_ROUND = {"n": 0}


def wd_jobs(_c):
    base = [W.make_job(company="BigCo", title="Inventory Analyst", location="Memphis, TN", url="https://wd/1",
                       source="Workday (official ATS)", ats="workday", job_id="R1", posted_at=None,
                       ts_note="timestamp unavailable (Workday says 'Posted Today')", description=SC_DESC)]
    if WD_ROUND["n"] >= 1:
        base.append(W.make_job(company="BigCo", title="Demand Planning Analyst", location="Memphis, TN",
                               url="https://wd/2", source="Workday (official ATS)", ats="workday", job_id="R2",
                               posted_at=None, ts_note="timestamp unavailable (Workday says 'Posted Today')",
                               description=SC_DESC))
    return base


def setup_tmp():
    d = Path(tempfile.mkdtemp())
    W.STATE_DIR = d
    W.SEEN_PATH, W.ALERTS_PATH, W.HEALTH_PATH = d / "seen.json", d / "alerts.jsonl", d / "health.json"
    S.ALERTS_PATH, S.HEALTH_PATH, S.ROOT = W.ALERTS_PATH, W.HEALTH_PATH, d
    return d


COMPANIES = [{"ats": "greenhouse", "raw": "greenhouse:acme", "slug": "acme"},
             {"ats": "adzuna", "raw": "adzuna:analyst", "slug": "analyst"},
             {"ats": "workday", "raw": "workday:bigco", "slug": "bigco"}]
FETCH = {"greenhouse": gh_jobs, "adzuna": adz_jobs, "workday": wd_jobs}


def test_full_cycle(monkeypatch=None):
    setup_tmp()
    sent_payloads = []
    W.send_ntfy = lambda alert, url: sent_payloads.append(alert)
    WD_ROUND["n"] = 0
    sent = W.run(now=NOW, companies=COMPANIES, fetchers=FETCH, details={})
    titles = sorted(r["title"] for r in sent)
    print("Round 1 alerts:", [(r["title"], r["company"], r["priority"], r["match"], r["score"]) for r in sent])
    assert "Business Analyst" in titles and titles.count("Business Analyst") == 1, "dedupe across sources"
    assert "Supply Chain Analyst" in titles
    assert "Data Analyst" not in titles, "Toronto must be excluded"
    assert not any("Manager" in t for t in titles)
    assert "Senior Data Analyst" not in titles, "8+ yrs must be excluded"
    assert "Software Engineer" not in titles
    assert "Procurement Analyst" not in titles, "50h old must be excluded"
    assert "Inventory Analyst" not in titles, "Workday baseline must be silent on first look"
    ba = next(r for r in sent if r["title"] == "Business Analyst")
    assert ba["priority"] == "URGENT" and ba["match"] == "High"
    assert ba["salary"].startswith("$85,000")
    sc = next(r for r in sent if r["title"] == "Supply Chain Analyst")
    assert sc["priority"] == "VERY NEW"

    # Round 2, 5 minutes later: nothing repeats; the new Workday job alerts with "time unavailable".
    WD_ROUND["n"] = 1
    sent2 = W.run(now=NOW + dt.timedelta(minutes=5), companies=COMPANIES, fetchers=FETCH, details={})
    print("Round 2 alerts:", [(r["title"], r["priority"]) for r in sent2])
    assert [r["title"] for r in sent2] == ["Demand Planning Analyst"]
    assert sent2[0]["posted_at"] is None and "unavailable" in sent2[0]["priority"]
    body = next(a["body"] for a in sent_payloads if "Demand Planning" in a["title"])
    assert "Posted: timestamp unavailable" in body and "Detected:" in body

    # Round 3: nothing new at all.
    assert W.run(now=NOW + dt.timedelta(minutes=10), companies=COMPANIES, fetchers=FETCH, details={}) == []
    lines = W.ALERTS_PATH.read_text().splitlines()
    assert len(lines) == len(sent) + 1

    # Print one full alert so a human can eyeball the format.
    print("\n----- sample alert -----\n" + sent_payloads[0]["body"])

    text = S.summary(now=NOW + dt.timedelta(minutes=20), send=False)
    assert "New jobs found: " in text and "Acme" in text


def test_helpers():
    assert W.extract_years("Requires 3+ years of experience with SQL")[0] == 3
    assert W.extract_years("2-4 years of relevant analytics experience")[1] == "2–4 years"
    assert W.extract_years("Great team, 401k")[0] is None
    mk = lambda loc, desc="": W.make_job(location=loc, description=desc, ats="greenhouse")
    assert W.is_us(mk("New York, NY"))
    assert W.is_us(mk("Remote"))
    assert W.is_us(mk("Remote - United States"))
    assert W.is_us(mk("London, UK; New York, NY"))
    assert not W.is_us(mk("London, United Kingdom"))
    assert not W.is_us(mk("Bengaluru, India"))
    assert not W.is_us(mk("Remote - Canada"))
    cfg = W.load_config()
    assert W.title_ok(cfg, "Business Systems Analyst II")
    assert W.title_ok(cfg, "Demand Planning Analyst")
    assert W.title_ok(cfg, "Product Operations Associate")
    assert W.title_ok(cfg, "Project Coordinator")
    assert not W.title_ok(cfg, "Director, Business Analytics")
    assert not W.title_ok(cfg, "Data Engineer")
    assert not W.title_ok(cfg, "Lead Data Analyst")
    job = W.make_job(title="Data Analyst", description="Excellent communication skills required. R&D team.")
    sc = W.score(cfg, job)
    assert "Excel" not in sc["matched"] and "R" not in sc["matched"], sc["matched"]
    job = W.make_job(title="Data Analyst", description="Must have an active security clearance. Must be a U.S. citizen.")
    assert set(W.score(cfg, job)["flags"]) >= {"Security clearance", "US citizenship required"}


def test_add_company():
    assert A.to_line("https://boards.greenhouse.io/airbnb", "Airbnb") == "greenhouse:airbnb|Airbnb"
    assert A.to_line("https://job-boards.greenhouse.io/stripe/jobs/123", None) == "greenhouse:stripe"
    assert A.to_line("https://jobs.lever.co/palantir/abc", None) == "lever:palantir"
    assert A.to_line("https://jobs.ashbyhq.com/ramp", "Ramp") == "ashby:ramp|Ramp"
    assert A.to_line("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", "NVIDIA") == \
        "workday:nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite|NVIDIA"


def test_companies_file_parses():
    cs = W.load_companies()
    assert len(cs) > 100
    assert all(c["ats"] in W.FETCHERS for c in cs), {c["ats"] for c in cs}


if __name__ == "__main__":
    test_helpers()
    test_add_company()
    test_companies_file_parses()
    test_full_cycle()
    print("\nALL TESTS PASSED")
