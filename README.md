# Job Watcher - real-time analyst job alerts

Every 5 minutes, GitHub Actions polls company job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, plus the optional Adzuna aggregator), finds **new** Business / Data / Operations / Supply Chain / Product / Project / BI analyst roles that fit your background, and sends a push notification to your phone through the free **ntfy** app.

- Uses the **original posting timestamp** from the employer's ATS whenever one exists. When a source has no exact time (Workday), the alert says `Posted: timestamp unavailable` and shows the time the watcher first detected it. The watcher never makes up a posting time.
- Priorities: **URGENT** (≤5 min), **VERY NEW** (5–15), **RECENT** (15–60). Jobs older than 60 min are skipped unless the match is unusually strong (score ≥78, up to 6 h).
- **No duplicates:** every job it has seen is logged in `state/seen.json`. If the same role shows up on Adzuna and on the company's ATS, you get one alert, and it uses the official application link.
- **Strong matches** also list ATS keywords, requirements that aren't on your profile, and a note on whether to tailor your resume.
- Requirements it can't judge for you (clearance, citizenship, sponsorship, travel) are flagged for you to check. It doesn't assume anything about them.
- **Daily summary** around 7:50 PM Pacific. An **hourly watchdog** alerts you if monitoring stops. A failed cycle alerts you right away.
- It never applies to jobs.

---

## Setup (about 15 minutes)

### 1. Phone alerts (ntfy)
1. Install **ntfy** from the App Store or Google Play.
2. Tap **+** → subscribe to the topic **`bee-jobs-9d470ec6533b`** (server: ntfy.sh).
   Treat the topic name like a password. Anyone who knows it can read your alerts.

### 2. Create the GitHub repo
1. Sign in at github.com (a free account works) → **New repository** → name it `job-watcher` → choose **Public** → Create.
   > Why public: runs in public repos are free with no limit. A private repo gets 2,000 free minutes a month, and a 5-minute schedule uses that up in about a week. The repo holds your skills config and the job titles you were alerted on. Your name, resume and contact details are not in it.
2. Upload this folder. The easiest way is Terminal on your Mac:
   ```bash
   cd ~/Downloads/job-watcher        # wherever you unzipped it
   git init && git add . && git commit -m "job watcher"
   git branch -M main
   git remote add origin https://github.com/<your-username>/job-watcher.git
   git push -u origin main
   ```
   (Uploading through the browser misses the hidden `.github` folder, so use the commands above or GitHub Desktop.)

### 3. Secrets (repo → Settings → Secrets and variables → Actions → New repository secret)
| Name | Value | Required? |
|---|---|---|
| `NTFY_TOPIC` | `bee-jobs-9d470ec6533b` | **Yes** |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Free key from developer.adzuna.com. Adds broad coverage of companies that aren't in the list | Recommended |
| `HEALTHCHECK_URL` | Ping URL from healthchecks.io (free). It emails you if GitHub itself stops running the watcher | Optional |

### 4. Permissions
Repo → **Settings → Actions → General → Workflow permissions** → **Read and write permissions** → Save.

### 5. Turn it on
1. **Actions** tab → enable workflows if GitHub asks.
2. Run **Validate sources** once (Run workflow, with prune = true). It checks all ~200 sources and comments out any that don't resolve. The results are saved in `state/validation.md`.
3. Run **Job watcher (every 5 min)** once by hand. After that it runs by itself.
4. Optional: run **Daily summary + watchdog** with `summary` to see a test summary on your phone.

On the first cycle the watcher quietly records every open Workday job as already seen, because Workday has no timestamps to tell which ones are new. Alerts start once new jobs appear.

---

## Customizing
- **Add companies:** `python add_company.py https://jobs.lever.co/somecompany "Some Company"`, then commit. Works with Greenhouse, Lever, Ashby, SmartRecruiters and Workday URLs. Every push to `companies.txt` runs validation again automatically.
- **Titles, skills, thresholds:** edit `config.toml`. It covers title include/exclude patterns, your skills and their synonyms, skills you don't have yet, the experience cap (`max_years = 5`), and the alert age and score cutoffs.
- **Too many or too few alerts:** raise or lower `min_score` (default 45).
- **Test locally:** `python watcher.py --dry-run` prints alerts without sending or saving anything. `python tests/test_watcher.py` runs the offline tests.

## Honest limitations
- **LinkedIn and Indeed are not polled.** They block automated access, and scraping them breaks their terms. Most jobs listed there come from the employer ATS feeds covered here, often a little earlier. Adzuna picks up much of the rest.
- **GitHub's 5-minute schedule is best effort.** When GitHub is busy, runs can be 5–15+ minutes late. Each alert shows both the real posting time and the detection time, so you can always see the true age.
- **Workday** only says "Posted Today". Those alerts come with *timestamp unavailable* and a detection time. The watcher checks the top results for "analyst", "supply chain" and "coordinator" at each Workday company.
- **Adzuna's time** is when Adzuna indexed the job, which can be later than the original posting. Alerts label it that way.
- **Match scores are keyword-based.** Read the posting before you apply.
