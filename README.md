# linkedin-easyapply-eu

Fault-tolerant EU-market LinkedIn Easy Apply bot (Python + Selenium `undetected-chromedriver`) powered by a local Ollama stack.

**Default mode is dry-run** — forms are filled and discarded; nothing is submitted until you pass `--live` or set `session.dry_run: false`.

> LinkedIn automation may violate LinkedIn’s User Agreement. Use at your own risk, keep caps low, and prefer dry-run while validating.

## Features

- Multi-model Ollama: Q&A (`gemma4:e4b`), intent (`llama3.2`), embeddings (`nomic-embed-text`)
- EU rules: salary EUR, **no employer sponsorship**, own Germany visa, CEFR, GDPR, notice period
- Cover letter from **your** config template (`{company}`, `{position}`, …)
- Search by job titles and/or tech keywords; Easy Apply + workplace filters
- Unattended form failures → screenshot/HTML dump → discard → next job
- Stops cleanly on LinkedIn’s **daily Easy Apply limit** (search right-pane banner)
- Captcha / 2FA → **pause for human** (timeout configurable)
- CSV + markdown run summary

## Setup

```bash
cd linkedin-easyapply-eu
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # edit with your real profile
cp .env.example .env                 # LinkedIn + Ollama settings
cp assets/resume.example.json assets/resume.json   # structured resume for Q&A

# Place your CV PDF at the path in config uploads.Resume (gitignored)
# e.g. assets/your_cv.pdf
```

### Never commit personal data

| File | Role | In git? |
|---|---|---|
| `config.example.yaml` | Sample profile / search / session | yes |
| `config.yaml` | Your real profile | **no** (gitignored) |
| `.env.example` | Sample secrets | yes |
| `.env` | LinkedIn + Ollama credentials | **no** |
| `assets/resume.example.json` | Sample structured resume | yes |
| `assets/resume.json` | Your structured resume (Q&A context) | **no** |
| `assets/*.pdf` / your CV | Uploaded to Easy Apply | **no** |

The bot loads `assets/resume.json` first (falls back to a legacy personal filename if present).

### Required Ollama models

```bash
ollama pull gemma4:e4b-it-q4_K_M   # or use fallback in .env
ollama pull llama3.2:latest
ollama pull nomic-embed-text:latest
```

Optional: set `EASYAPPLY_DEBUG_FILL=true` in `.env` to log maxlength / digit / retry fill decisions.

## Commands

```bash
# Health check (config + Ollama)
python -m src.main --check

# Ollama / EU rule smoke tests (no browser)
python test_ollama_smoke.py

# Dry-run (default): search + fill + discard, no submit
python -m src.main --dry-run --max-apps 5

# Tech-only or roles-only (overrides search.strategy)
python -m src.main --tech-only --max-apps 5
python -m src.main --roles-only --max-apps 5
python -m src.main --tech "LangGraph" --tech "RAG" --max-apps 3

# Live submit (only when you are ready)
python -m src.main --live --max-apps 3
```

First login may require **mobile approve** after Sign in. The bot waits (`session.login_2fa_wait_minutes` / `LOGIN_2FA_WAIT_MINUTES`) then continues.

If LinkedIn shows *“We limit daily submissions… Save this job and apply tomorrow.”* on the search results pane, the run stops with `STOPPED_EASY_APPLY_LIMIT`.

### Apple Silicon note

If you see `OSError: [Errno 86] Bad CPU type in executable` for chromedriver, the bot auto-downloads a **mac-arm64** ChromeDriver into `.drivers/` and ad-hoc codesigns it. You can also clear a bad cache:

```bash
rm -rf ~/Library/Application\ Support/undetected_chromedriver
```

## Config notes

| Item | Behavior |
|---|---|
| `profile` / `experience` / `eu_work_authorization` | Answers for common Easy Apply fields |
| `compensation_eu` | EUR salary targets; used for numeric salary questions |
| `languages_cefr` | CEFR + LinkedIn/German labels |
| `uploads.Resume` | Path to local CV PDF (gitignored) |
| `cover_letter.template` | Placeholders `{full_name}`, `{position}`, `{company}`, `{location}`, `{years_experience}` |
| `positions` / `tech_keywords` / `locations` | Search queries |
| `search.strategy` | `positions_and_tech` (or CLI `--roles-only` / `--tech-only`) |
| `search.filters` | Easy Apply only, date posted, workplace, experience level |
| `session.dry_run` | `true` in example; use `--live` to submit |
| `session.max_applications_per_run` / `_per_day` | Caps; CLI `--max-apps` overrides per-run |
| `qa_memory` | Fuzzy match + optional Ollama intent / embeddings |

## Outputs

- `output/applications.csv` — per-job status
- `output/seen_jobs.csv` — cross-run dedupe
- `output/unanswered_questions.csv` — hard failures
- `output/reports/run_summary_*.md` — end-of-run report
- `crash_dumps/` — PNG + HTML on validation failure

## Docs

- [`Doc/Architecture.md`](Doc/Architecture.md) — product spec
- [`Doc/Implementation_Plan.md`](Doc/Implementation_Plan.md) — phased plan + locked decisions
- [`Doc/DOM_Selectors.md`](Doc/DOM_Selectors.md) — selector registry (incl. daily Easy Apply limit)
