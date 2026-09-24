#!/usr/bin/env python3
"""Add a company to companies.txt from its careers/job-board URL.

    python add_company.py https://boards.greenhouse.io/airbnb "Airbnb"
    python add_company.py https://jobs.lever.co/palantir
    python add_company.py https://jobs.ashbyhq.com/ramp
    python add_company.py https://jobs.smartrecruiters.com/Visa
    python add_company.py https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite "NVIDIA"
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

PATH = Path(__file__).resolve().parent / "companies.txt"


def to_line(url: str, name: str | None) -> str:
    u = urlparse(url if "://" in url else "https://" + url)
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
    nm = f"|{name}" if name else ""
    if "greenhouse.io" in host:
        if "for=" in u.query:
            return f"greenhouse:{re.search(r'for=([^&]+)', u.query).group(1)}{nm}"
        return f"greenhouse:{parts[0]}{nm}"
    if "lever.co" in host:
        return f"lever:{parts[0]}{nm}"
    if "ashbyhq.com" in host:
        return f"ashby:{parts[0]}{nm}"
    if "smartrecruiters.com" in host:
        return f"smartrecruiters:{parts[0]}{nm}"
    if "myworkdayjobs.com" in host:
        tenant = host.split(".")[0]
        site = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)][0]
        return f"workday:{host}|{tenant}|{site}|{name or tenant.title()}"
    raise SystemExit(f"Unrecognized job board URL: {url}\n"
                     "Supported: Greenhouse, Lever, Ashby, SmartRecruiters, Workday.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    line = to_line(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    existing = PATH.read_text()
    if line.split("|")[0] in existing:
        print("Already present:", line)
    else:
        PATH.write_text(existing.rstrip("\n") + "\n" + line + "\n")
        print("Added:", line)
