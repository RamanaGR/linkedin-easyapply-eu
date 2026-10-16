# linkedin-easyapply-eu

Fault-tolerant EU-market LinkedIn Easy Apply bot (Python + Selenium `undetected-chromedriver`) powered by a local Ollama stack.

**Default mode is dry-run** — forms are filled and discarded; nothing is submitted until you pass `--live` or set `session.dry_run: false`.

> LinkedIn automation may violate LinkedIn’s User Agreement. Use at your own risk, keep caps low, and prefer dry-run while validating.

## Features (v1 Full Core)

- Multi-model Ollama: Q&A (`gemma4:e4b`), intent (`llama3.2`), embeddings (`nomic-embed-text`)
- EU rules: salary EUR, **no employer sponsorship**, own Germany visa, CEFR, GDPR, notice period
- Cover letter from **your** config template
- Unattended form failures → screenshot/HTML dump → discard → next job
- Captcha / 2FA → **pause for human** (timeout configurable)
- CSV + markdown run summary

## Setup

```bash
cd linkedin-easyapply-eu
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # edit with your real profile
cp .env.example .env                 # LINKEDIN_EMAIL / PASSWORD + Ollama

# Place your CV PDF at the path in config uploads.Resume
# e.g. assets/Ramana_Gangarao_CV.pdf
```

Required local models (already on your machine if listed):

- `gemma4:e4b-it-q4_K_M` (fallback `gemma4:e2b-it-q4_K_M`)
- `llama3.2:latest`
- `nomic-embed-text:latest`

## Commands

```bash
# Health check (config + Ollama)
python -m src.main --check

# Ollama / EU rule smoke tests (no browser)
python test_ollama_smoke.py

# Dry-run (default): search + fill + discard, no submit
python -m src.main --dry-run --max-apps 5

# Roles + tech stack (default from config tech_keywords)
python -m src.main --dry-run --max-apps 5

# Tech-only or roles-only
python -m src.main --tech-only --max-apps 5
python -m src.main --roles-only --max-apps 5
python -m src.main --tech "LangGraph" --tech "RAG" --max-apps 3

# Live submit (only when you are ready)
python -m src.main --live --max-apps 3
```

First login may require **mobile approve** after Sign in (the only human step). The bot waits (`session.login_2fa_wait_minutes`) then continues search/apply with **no captcha pauses** mid-run.

### Apple Silicon note

If you see `OSError: [Errno 86] Bad CPU type in executable` for chromedriver, the bot auto-downloads a **mac-arm64** ChromeDriver into `.drivers/` and ad-hoc codesigns it. You can also delete a bad cache manually:

```bash
rm -rf ~/Library/Application\ Support/undetected_chromedriver
```

## Config notes

| Item | Behavior |
|---|---|
| `requires_visa_sponsorship` | `No` |
| `can_provide_own_germany_visa` | `Yes` |
| `cover_letter.template` | Edit freely; placeholders `{full_name}`, `{position}`, `{company}`, `{location}`, `{years_experience}` |
| `session.dry_run` | `true` in example |
| PII | Only `config.example.yaml` is committed; keep `config.yaml` + `.env` local |

## Outputs

- `output/applications.csv` — per-job status
- `output/seen_jobs.csv` — cross-run dedupe
- `output/unanswered_questions.csv` — hard failures
- `output/reports/run_summary_*.md` — end-of-run report
- `crash_dumps/` — PNG + HTML on validation failure

## Docs

- [`Doc/Architecture.md`](Doc/Architecture.md) — product spec
- [`Doc/Implementation_Plan.md`](Doc/Implementation_Plan.md) — phased plan + locked decisions
- [`Doc/DOM_Selectors.md`](Doc/DOM_Selectors.md) — selector registry
