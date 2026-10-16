# Implementation Plan — EU LinkedIn Easy Apply Bot

**Source of truth:** [`Architecture.md`](./Architecture.md)  
**Status:** v1 Full Core — implementation in progress / scaffolded  
**Build mode:** Greenfield from scratch  
**v1 tier:** Full Core (confirmed)  
**Defaults for unanswered items:** daily cap 50, all workplace types equally, captcha pause 15 min then abort, EN+DE phone labels, `requirements.txt`, cover letter draft template OK  
**Improvements:** Accepted and wired into code

---

## 0. Reality check vs Architecture.md

| Architecture assumption | Actual repo state | Locked decision |
|---|---|---|
| Upgrade existing Selenium bot | No `src/` tree | **Build from scratch** using Architecture as product spec |
| `ARCHITECTURE.md` Section 10 DOM issues | File not present | Treat as Easy Apply form-filling DOM work; maintain living `Doc/DOM_Selectors.md` during Phase 4 |
| Resume PDF + JSON under `assets/` | Missing | Create JSON from Architecture §2; user supplies PDF locally (not committed if PII-sensitive path) |
| Ollama models | Confirmed on machine | See Locked Decisions §1 |

---

## 1. Locked decisions (from your answers)

| # | Topic | Decision |
|---|---|---|
| 1 | Build mode | Greenfield — no port from another repo |
| 2 | Missing Section 10 | Interpret as LinkedIn Easy Apply form-filling; document selectors as we discover them |
| 3 | Ollama models | Primary Q&A: `gemma4:e4b-it-q4_K_M`; fallback: `gemma4:e2b-it-q4_K_M`; intent: `llama3.2:latest`; embed: `nomic-embed-text:latest`. Also available (optional later): `qwen2.5-coder:7b` / `14b` |
| 4 | Login | Credentials in `.env`. After clicking Login, **wait** for possible mobile verification; user completes first-time 2FA manually; then reuse persistent Chrome profile |
| 5 | Dry run | `session.dry_run: true` by default; real submit only when user flips it |
| 6 | Captcha / checkpoint | **Pause for human** (do not discard-loop). Log clearly; wait until user resolves or timeout aborts run |
| 7 | Experience dates | Confirmed — Architecture §2 matches CV |
| 8 | Visa / sponsorship | **No employer sponsorship.** `requires_visa_sponsorship: "No"`. Own Germany visa after offer: `"Yes"`. Free-text uses updated `germany_visa_statement` |
| 9 | Cover letter | Config **template** (user edits/corrects). Bot fills placeholders + light Ollama adapt, then applies. Optional PDF upload path if provided |
| 10 | Location fields | Always **Bangalore, Karnataka, India** (or Bangalore typeahead) |
| 11 | Search | **All** `positions × locations` combinations, with **job-ID dedupe** across the run |
| 12 | PII in git | Commit **`config.example.yaml` only**. Real `config.yaml` + `.env` gitignored. Architecture.md examples may keep placeholders in docs; runtime uses local config |
| 13 | v1 scope | See Section 8 — pick a tier (recommended: **v1 Full Core**) |

### Accepted improvements (will implement)

- Visa decision table (sponsorship → No; own visa → Yes)
- Bilingual EN/DE (and basic NL) question keyword maps
- Cover letter from config template
- Search cartesian product + job-ID dedupe + `search.strategy: position_outer` default (iterate positions, then locations)
- Randomized human pacing + daily/run caps
- Captcha/checkpoint → pause for human
- Applied/skipped ID persistence across runs
- `config.example.yaml` only in git
- Pydantic (or equivalent) typed config
- Selector registry + `Doc/DOM_Selectors.md`
- Structured logging with `job_id` / status
- Feature flags from config
- `dry_run` default true + ToS note in README
- CLI overrides: `--max-apps`, `--dry-run`, `--check`
- Crash dump index CSV

---

## 2. Goals (v1)

1. Unattended EU-market LinkedIn Easy Apply (Germany / Netherlands / EU).
2. Local multi-model Ollama stack for Q&A, intent matching, and embeddings.
3. Structured profile/config for compensation, **no-sponsorship visa policy**, CEFR, GDPR, blacklists, cover-letter template.
4. Fault-tolerant loop: never block on forms; dump, discard, continue — **except** login 2FA / captcha (pause for human).
5. End-of-run telemetry (`applications.csv` + markdown summary).

---

## 3. Suggested project layout

```text
linkedin-easyapply-eu/
├── Doc/
│   ├── Architecture.md
│   ├── Implementation_Plan.md
│   └── DOM_Selectors.md            # filled during Phase 4
├── assets/                         # local; gitignore real CV if desired
│   ├── .gitkeep
│   ├── Ramana_Gangarao_CV.pdf      # local only
│   └── Ramana_Gangarao_Resume.json
├── config.example.yaml             # committed (fake/placeholder PII)
├── config.yaml                     # gitignored (your real profile)
├── .env.example                    # committed
├── .env                            # gitignored (LINKEDIN_EMAIL, LINKEDIN_PASSWORD, OLLAMA_*)
├── requirements.txt
├── test_ollama_smoke.py
├── crash_dumps/                    # gitignored
├── output/                         # gitignored
│   ├── applications.csv
│   ├── unanswered_questions.csv
│   ├── qa_memory.csv
│   ├── seen_jobs.csv               # cross-run job ID memory
│   └── reports/
└── src/
    ├── __init__.py
    ├── main.py
    ├── config.py
    ├── bot.py                      # login wait-for-2FA, captcha pause
    ├── job_search.py
    ├── application.py
    ├── qa_manager.py
    ├── qa_intent.py
    ├── ollama_client.py
    ├── cover_letter.py             # template render + optional Ollama adapt
    ├── reporter.py
    ├── selectors.py                # selector registry
    ├── utils.py
    └── logger.py
```

---

## 4. Phased delivery

### Phase 0 — Prerequisites & scaffolding (0.5–1 day)

| # | Task | Done when |
|---|---|---|
| 0.1 | Python 3.11+ venv | Documented in README |
| 0.2 | `requirements.txt` (selenium, undetected-chromedriver, pyyaml, python-dotenv, rapidfuzz, httpx, numpy, pydantic) | Install OK |
| 0.3 | Scaffold `src/` stubs | Importable |
| 0.4 | `.env.example`, `config.example.yaml`, `.gitignore` | No secrets in git |
| 0.5 | Verify Ollama tags from locked list | Smoke ping OK |
| 0.6 | Resume JSON from Architecture §2; user places CV PDF locally | Paths resolve |

### Phase 1 — Configuration & resume (0.5 day)

| # | Task | Notes |
|---|---|---|
| 1.1 | `config.example.yaml` + local `config.yaml` | Visa No / own visa Yes; cover_letter template |
| 1.2 | Pydantic config loader + validation | Fail fast at startup |
| 1.3 | Resume JSON loader | Grounding for Ollama |
| 1.4 | Backward-compat salary keys | `salary`, `rate`, LPA |

### Phase 2 — Ollama, humanizer, cover letter, smoke (1 day)

| # | Task | Notes |
|---|---|---|
| 2.1 | Multi-role client + e4b→e2b fallback | |
| 2.2 | Grounded system prompt + `sanitize_human_text` | |
| 2.3 | Intent matcher (`llama3.2`) | |
| 2.4 | Embeddings cache on qa_memory | |
| 2.5 | `cover_letter.py` template → placeholders → optional adapt | |
| 2.6 | Smoke: salary, visa **No**/own-visa **Yes**, notice units, sanitizer, cover letter | |

### Phase 3 — Browser, login, search (1–2 days)

| # | Task | Notes |
|---|---|---|
| 3.1 | Undetected Chrome + persistent profile dir | |
| 3.2 | `.env` login → wait after Login click for mobile/2FA | Manual first time |
| 3.3 | Captcha/checkpoint detector → pause for human | |
| 3.4 | Search: all positions × locations, Easy Apply filters | Dedupe by job ID |
| 3.5 | Blacklists + skip strict German C1 (JD + form) | |
| 3.6 | Session limits + pacing delays | |
| 3.7 | `dry_run: true` path logs `DRY_RUN` without Submit | |

### Phase 4 — Easy Apply form engine (2–3 days)

| # | Task | Notes |
|---|---|---|
| 4.1–4.4 | Modal steps, sections, text, dropdowns | Live `DOM_Selectors.md` |
| 4.5 | Location typeahead → Bangalore | |
| 4.6 | Compensation EUR / number / LPA | |
| 4.7 | Visa matrix + notice period | **Sponsorship = No** |
| 4.8 | CEFR maps | |
| 4.9 | GDPR, resume upload, cover letter text/template | |
| 4.10 | Submit heuristics EN/DE (only if not dry_run) | |
| 4.11 | Unknown fields → unanswered_questions.csv | |

### Phase 5 — Unattended fault recovery (1 day)

| # | Task | Notes |
|---|---|---|
| 5.1 | 2-round validation learning | |
| 5.2 | Dump → discard → `FAILED_VALIDATION` → next | |
| 5.3 | Exceptions → `FAILED_EXCEPTION` | Never hang on ENTER |
| 5.4 | Captcha still pauses (exception to unattended) | |

### Phase 6 — Reporting & polish (0.5–1 day)

| # | Task | Notes |
|---|---|---|
| 6.1 | `applications.csv` + `seen_jobs.csv` | |
| 6.2 | Console + `run_summary_*.md` | |
| 6.3 | README: setup, models, dry-run, 2FA, ToS | |
| 6.4 | CLI `--check`, `--dry-run`, `--max-apps` | |
| 6.5 | Capped live run when you disable dry_run | |

---

## 5. Dependency graph

```text
Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6
                         └── reporter CSV schema stub early (parallel OK)
```

---

## 6. Testing strategy

| Layer | What | When |
|---|---|---|
| Offline unit | config, visa matrix, notice units, salary, CEFR, sanitizer, cover template | Phase 1–2 |
| Ollama smoke | 3 models + EU scenarios | Phase 2 |
| Browser dry | search + cards; no submit | Phase 3 |
| Form exploration | build `DOM_Selectors.md` from real Easy Apply | Phase 4 |
| Live capped | `dry_run: false`, small `max_applications_per_run` | Phase 5–6 after you approve |

---

## 7. Visa answer matrix (locked)

| Question pattern (EN/DE gist) | Answer |
|---|---|
| Require visa sponsorship / employer sponsorship / Visum vom Arbeitgeber | **No** |
| Can you obtain / provide your own visa or work permit (Germany) | **Yes** |
| Authorized to work in EU/Germany today | **No** (not currently in EU) unless form only allows Yes/No with no nuance — then prefer free-text statement if available |
| Willing to relocate | **Yes** |
| Free text visa / start date | `germany_visa_statement` from config |

---

## 8. What “v1 scope” means (your Q13)

Architecture has many features. “Out of scope for v1” means we **ship a working bot first**, and defer nicer extras so you can start dry-runs sooner.

### Recommended: **v1 Full Core** (default if you say “go”)

| Include in v1 | Defer to v1.1 |
|---|---|
| Config + example YAML, resume JSON | Fancy embedding DB (sqlite) — CSV vectors OK for v1 |
| gemma / llama / nomic all three roles | qwen2.5-coder as alternate Q&A model |
| Easy Apply fill + EU rules + visa matrix | Deep Dutch question coverage (basic NL keywords OK) |
| Cover letter from **your** template | Multi-language cover letter variants |
| Unattended discard on form fail | Auto-retry failed jobs next day |
| Captcha/2FA pause for human | SMS API / fully headless 2FA |
| dry_run default, CSV + markdown report | Web dashboard / UI |
| EN + DE submit/discard/CEFR/visa keywords | Full DE job-description NLP beyond C1 skip |
| Job ID dedupe + pacing | Adaptive ML pacing |
| Selector registry + crash dumps | Large fixture regression suite |

### Slimmer option: **v1 MVP** (fastest first dry-run)

Ship Phases 0–3 + minimal Phase 4 (text + dropdown + resume + visa/salary only), skip embeddings (`use_embeddings: false`), skip cover-letter adapt (template paste only), skip markdown report (CSV only). Add the rest in v1.1.

### Heavier option: **v1 Plus**

Full Core + qwen fallback + sqlite memory + broader DE/NL JD filtering + unit test package from day one.

**Please reply with one of:** `Full Core` (recommended) · `MVP` · `Plus`

---

## 9. Residual questions — resolved with defaults

| Question | Resolution |
|---|---|
| v1 tier | **Full Core** |
| Daily cap | `max_applications_per_day: 50` |
| Workplace | All hybrid/remote/onsite equally |
| Phone UI | Match EN `India (+91)` + DE-tolerant option matching |
| Packaging | `requirements.txt` |
| Captcha timeout | 15 minutes then abort |
| Cover letter template | Draft in config; user edits `config.yaml` |

---

## 10. Definition of Done (v1 Full Core)

- [ ] Greenfield `src/` implements Architecture + locked decisions
- [ ] `config.example.yaml` committed; real PII only local
- [ ] Sponsorship answers **No**; own Germany visa **Yes**
- [ ] Cover letter from config template
- [ ] Three Ollama roles; smoke tests green
- [ ] Login waits for manual 2FA; captcha pauses for human
- [ ] `dry_run` default true; no submit until disabled
- [ ] Unattended form failures → dump → discard → next
- [ ] CSV + run summary MD; seen job IDs persist
- [ ] README covers setup, models, 2FA, dry-run, ToS

---

## 11. Revision log

| Date | Change |
|---|---|
| 2026-10-16 | Initial plan |
| 2026-10-16 | Locked user decisions; accepted improvements; visa → no sponsorship; cover letter template; v1 scope explained |
| 2026-10-16 | Full Core implementation scaffolded under `src/`; smoke + `--check` green |

---

**Next step for you:** Add `.env`, place CV PDF, run `python -m src.main --dry-run --max-apps 3`.
