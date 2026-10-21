# Role & Objective

You are a Senior UI & LinkedIn Automation Architect. Upgrade our existing Python + Selenium (`undetected-chromedriver`) LinkedIn Easy Apply Bot (documented in `@ARCHITECTURE.md`) into a **fault-tolerant, 100% unattended EU Market LinkedIn Easy Apply Bot** powered by our local Ollama stack (`gemma4:e4b-it-q4_K_M`, `llama3.2:latest`, and `nomic-embed-text:latest`).

Read the existing codebase (`src/main.py`, `src/config.py`, `src/bot.py`, `src/job_search.py`, `src/application.py`, `src/qa_manager.py`, `src/qa_intent.py`, `src/ollama_client.py`, `src/utils.py`, `src/logger.py`) and apply all specifications below cleanly and modularly.

---



## 1. Ground-Truth Configuration (`config.yaml`)

Update `config.yaml` to support all EU market filters, compensation (`75,000 - 95,000 EUR`), visa/sponsorship rules, CEFR language levels, and unattended fault recovery:

```yaml
profile:
  full_name: "Ramana Gangarao"
  first_name: "Ramana"
  last_name: "Gangarao"
  date_of_birth: "04/04/1995"
  email: "gangaraoramana0404@gmail.com"
  phone_number: "+91 8555021702"
  phone_country_code: "India (+91)"
  current_city: "Bangalore"
  current_country: "India"
  location_full: "Bangalore, Karnataka, India"
  linkedin_url: "https://www.linkedin.com/in/ramana-gangarao"
  github_url: "https://github.com/ramanagangarao"

experience:
  total_experience_years: 8
  skill_years:
    Python: 8
    Java: 5
    SQL: 8
    FastAPI: 4
    Spring Boot: 4
    AWS: 6
    GCP: 3
    Docker: 6
    Kubernetes: 4
    Apache Kafka: 4
    LangGraph: 2
    LangChain: 3
    Google ADK: 2
    MCP: 2
    Generative AI: 3
    RAG: 3
    LLM Evaluation: 2
    DeepEval: 2
    Langfuse: 2
    Neo4j: 2
    Milvus: 2
    PostgreSQL: 6
    Redis: 4
    PySpark: 4
    SageMaker: 4
    MLOps: 5
    React.js: 3
    Terraform: 3

eu_work_authorization:
  currently_in_eu: false
  # Applicant does NOT need employer sponsorship; will self-apply for visa after job offer.
  requires_visa_sponsorship: "No"
  can_provide_own_germany_visa: "Yes"
  germany_visa_timeline: "5-8 weeks"
  germany_visa_statement: "I do not require employer visa sponsorship. After a job offer I will apply for my own German work visa and permit within 5 to 8 weeks. My notice period is 30 days."
  willing_to_relocate: "Yes"
  notice_period_days: 30
  notice_period_weeks: 4
  notice_period_months: 1

compensation_eu:
  currency: "EUR"
  expected_salary_eur_min: 75000
  expected_salary_eur_max: 95000
  expected_salary_eur_target: 85000     # Used when input strictly requires a single number
  expected_salary_range_text: "75000 - 95000 EUR"
  current_salary_eur: 45000
  # Backward-compatible keys for existing src/config.py
  salary: 85000
  rate: 55
  current_ctc_lpa: 30
  expected_ctc_lpa: 40

languages_cefr:
  English:
    cefr: "C2"
    linkedin_label: "Native or bilingual"
    german_label: "Verhandlungssicher"
    numeric_0_to_10: 10
  Telugu:
    cefr: "C2"
    linkedin_label: "Native or bilingual"
    german_label: "Muttersprache"
    numeric_0_to_10: 10
  German:
    cefr: "A1"
    linkedin_label: "Elementary"
    german_label: "Grundkenntnisse"
    status: "A1, currently learning"
    numeric_0_to_10: 2
  default_other_language:
    cefr: "None"
    linkedin_label: "None"
    german_label: "Keine"
    numeric_0_to_10: 0

education:
  highest_degree: "Master's Degree"
  masters_degree: "Master of Science in Information Technology"
  masters_institution: "Atlantis University, Miami, USA"
  masters_dates: "08/2024 - 10/2025"
  bachelors_degree: "Bachelor of Technology in Electronics and Instrumentation Engineering"
  bachelors_institution: "Keshav Memorial Institute of Technology (JNTUH), Hyderabad, India"
  bachelors_dates: "07/2013 - 04/2016"
  certifications:
    - "AWS Certified Cloud Practitioner (2023)"

gdpr_and_consents:
  accept_gdpr_privacy_policy: true
  accept_talent_pool_storage: true
  uncheck_follow_company: true

uploads:
  Resume: "assets/your_cv.pdf"
  Cover Letter: ""              # optional PDF path; leave empty if unused

cover_letter:
  enabled: true
  # User-editable template. Placeholders: {full_name}, {position}, {company}, {location}, {years_experience}
  # Bot fills placeholders then lightly adapts via Ollama while keeping the template structure.
  template: |
    Dear Hiring Team,

    I am applying for the {position} role at {company}. I have {years_experience}+ years of experience in AI and software engineering, with focus on agentic systems, RAG, and production LLM applications.

    I am based in Bangalore, India, and I do not require employer visa sponsorship. After a job offer I can apply for my own German work visa within 5 to 8 weeks. My notice period is 30 days.

    Please find my CV attached. I look forward to hearing from you.

    Kind regards,
    {full_name}

positions:
  - "Senior AI Engineer"
  - "Lead AI Engineer"
  - "AI Software Engineer"
  - "Generative AI Engineer"
  - "Agentic AI Engineer"
  - "LLM Engineer"
  - "Machine Learning Engineer"

# Search by main tech stack as well as role titles
tech_keywords:
  - "LangGraph"
  - "LangChain"
  - "RAG"
  - "LLM"
  - "Generative AI"
  - "Agentic AI"
  - "Google ADK"
  - "MCP"
  - "FastAPI"
  - "Python LLM"
  - "MLOps"
  - "Amazon Bedrock"

locations:
  - "Germany"
  - "Berlin, Germany"
  - "Munich, Germany"
  - "Frankfurt, Germany"
  - "Hamburg, Germany"
  - "Netherlands"
  - "Amsterdam, Netherlands"
  - "European Union"

search:
  strategy: "positions_and_tech"   # roles + tech_keywords × locations (job-ID deduped)
  use_positions: true
  use_tech_keywords: true
  filters:
    easy_apply_only: true
    date_posted: "past_week"          # 24h | past_week | past_month
    workplace_type:
      - "hybrid"
      - "remote"
      - "onsite"
    experience_level:
      - "Mid-Senior"
      - "Associate"
  blacklists:
    title_keywords:
      - "Intern"
      - "Praktikum"
      - "Werkstudent"
      - "Working Student"
      - "PhD"
    companies:
      - "Crossover"
      - "Canonical"
    skip_strict_german_c1_required: true

session:
  max_search_time: 7200
  max_applications_per_run: 50
  unattended_mode: true               # Never block on human ENTER; log, discard modal, and continue
  require_submission_confirmation: false
  dry_run: true                          # Default safe; use --live to submit

qa_memory:
  fuzzy_high_threshold: 88
  fuzzy_low_threshold: 75
  use_ollama_intent: true
  use_embeddings: true
```

---



## 2. Sync Structured Resume Context (`assets/resume.json`)

Copy `assets/resume.example.json` → `assets/resume.json` (gitignored) and fill your details. Place your CV PDF at the path in `uploads.Resume` so the bot loads your local EU CV:

- **Header:** Ramana Gangarao | Senior AI & Software Engineer | Agentic AI · RAG · LLM Evaluation | Bangalore, India | Born 04/04/1995 | +91 8555021702 | [gangaraoramana0404@gmail.com](mailto:gangaraoramana0404@gmail.com)
- **Summary Note:** "I can provide my own visa/work permit for Germany within 5–8 weeks. My notice period is 30 days. 8+ years of project experience."
- **Skills:** Python, Java, SQL, LangGraph, Google ADK, LangChain, MCP, ReAct, Multi-Agent Systems, HITL, Claude, Gemini, OpenAI, Amazon Bedrock, RAG, Hybrid Retrieval, Embeddings, LLM-as-a-Judge, Docling, Unstructured.io, DeepEval, Langfuse, MLflow, SageMaker, Model Monitoring, FastAPI, Spring Boot, Spring, Pydantic, REST APIs, Event-Driven Architecture, Apache Kafka, Neo4j, Milvus, PostgreSQL, Redis, Presidio, Liquibase, Google Document AI, AWS, GCP, Docker, Kubernetes, Terraform, Jenkins, GitHub Actions, CI/CD.
- **Experience:**
  1. `07/2026 – today`: **Lead AI & Software Engineer**, Synechron Technologies Pvt. Ltd., Bangalore, India (US financial AI platform for LLM agent observability, quality evaluation, and regulatory compliance at Wells Fargo. Python, FastAPI, Apache Kafka, LLM-as-a-Judge, React.js HITL workflows, PostgreSQL, Liquibase, Harness CI/CD).
  2. `12/2025 – 05/2026`: **AI Engineer**, Innovapath INC, San Francisco, USA (US healthcare AI platform for automated contract understanding at Optum. 9-agent Google ADK pipeline, Google Document AI, Vertex AI Gemini, 16-node Neo4j knowledge graph, ReAct agents, MCP interface with Pydantic, FastAPI, DeepEval, 40–50% lower review effort, p95 latency < 5s, Docker, GCP Cloud Run/GKE).
  3. `01/2025 – 10/2025`: **Graduate Research Assistant (AI & Systems)**, Atlantis University, Miami, USA (LangGraph multi-agent RAG platform using Amazon Bedrock Claude Sonnet, Milvus hybrid retrieval, Amazon Titan embeddings, cross-encoder reranking, Docling/Unstructured.io for 1,000+ profiles, Redis semantic caching, Langfuse/DeepEval).
  4. `09/2021 – 08/2024`: **Software Developer / MLOps Engineer**, Wipro Limited, Chennai, India (Alight US workforce intelligence platform for 36M+ users. AWS/SageMaker ML pipelines and feature stores using Python and PySpark for RoBERTa/XGBoost models, MLflow, migrated 50,000+ documents improving retrieval by 20%).
  5. `09/2016 – 09/2021`: **Software Engineer**, INNOVAPATH IT Solutions PVT Ltd., Hyderabad, India (Enterprise HR CRM using Java, Spring Boot, REST APIs, JPA/Hibernate, Docker, AWS, Jenkins).
- **Education:**
  - M.S. in Information Technology, Atlantis University, Miami, USA (`08/2024 – 10/2025`)
  - B.Tech in Electronics and Instrumentation Engineering, Keshav Memorial Institute of Technology (JNTUH), Hyderabad, India (`07/2013 – 04/2016`)
- **Certifications:** AWS Certified Cloud Practitioner (2023)
- **Languages:** English (C2), Telugu (Mother tongue), German (A1, currently learning)

---



## 3. Multi-Model Local Ollama Client & Anti-AI Humanizer (`src/ollama_client.py` & `src/qa_intent.py`)

1. Configure `src/ollama_client.py` to support three model roles loaded from `.env`:
  - `OLLAMA_MODEL` (`gemma4:e4b-it-q4_K_M`, fallback `gemma4:e2b-it-q4_K_M`) for form Q&A and open-ended text answers.
  - `OLLAMA_INTENT_MODEL` (`llama3.2:latest`) for fast dropdown/radio intent matching in `src/qa_intent.py`.
  - `OLLAMA_EMBED_MODEL` (`nomic-embed-text:latest`) for vector cosine similarity matching in `src/qa_manager.py` (cache embeddings locally so vectors are computed once per question in `qa_memory.csv`).
2. **Strict Non-Native Human English Persona & Grounding:**
  - When generating any text answer, inject this system prompt:
  1. Use ONLY facts from the provided Resume and Config context. Never invent skills, companies, degrees, or years.
  2. Write in simple, direct, natural English like a fluent non-native English-speaking engineer.
  3. Keep open-ended answers brief: 1 to 3 simple sentences.
  4. NEVER use em-dashes (—), en-dashes (–), semicolons (;), or markdown formatting (** or ```).
  5. NEVER use AI buzzwords: delve, testament, tapestry, furthermore, moreover, pivotal, spearheaded, unwavering, thrilled, synergy, landscape, cutting-edge, leverage, utilizing, orchestrated, passionate. Use plain words like "built", "worked on", "used", "developed".
  6. Return ONLY the direct answer text with no preamble or quotes.
    `
3. **Deterministic Post-Processor (**`sanitize_human_text(text: str) -> str`**):**
  - Run every AI-generated string through `sanitize_human_text()` before filling any field:
    - Replace `—` and `–` with `,`  or `.` .
    - Replace `;` with `.`.
    - Strip all `*`, ```, and surrounding quotes `"` / `'`.
    - Replace banned AI words case-insensitively (`spearheaded` -> `led`, `leveraged` / `utilizing` -> `used` / `using`, `furthermore` / `moreover` -> `Also`).
    - Collapse extra whitespace.

---



## 4. UI Element Handling & EU Form Rules (`src/application.py`)

Upgrade `src/application.py` to solve all High-Priority DOM issues in `ARCHITECTURE.md` Section 10:

1. **Unified Section Discovery:**
  - Keep `.jobs-easy-apply-form-section__grouping, .fb-dash-form-element` to capture all standard and dashed form elements.
2. **Closed Dropdowns & Native** `<select>`**:**
  - If a field is an Artdeco dropdown trigger or unopened custom select, click to open it first, wait `400ms`, read the portaled options, match via `QAMemoryManager` / `llama3.2:latest`, click the option, and verify selection.
3. **Location / City Typeahead Comboboxes:**
  - Type `Bangalore, Karnataka, India` (or `Bangalore`), wait `800ms` for `.basic-typeahead__triggered-content` / `[role="listbox"]`, press `Keys.ARROW_DOWN` then `Keys.ENTER`, wait `300ms`, and verify no red validation error remains.
4. **EU Compensation (**`75,000 - 95,000 EUR`**):**
  - If a salary field is a `type="number"` or triggers a numeric validation error: fill `85000` (or `75000` if minimum asked).
  - If a salary field is free text and accepts a range: fill `75000 - 95000 EUR`.
  - If the question explicitly specifies `INR` or `LPA`, fall back to `40` (LPA) or `4000000` (INR).
5. **EU Visa / Sponsorship & Notice Period:**
  - If asked *"Will you now or in the future require visa sponsorship?"* (or German equivalents) -> Select `No` (per `requires_visa_sponsorship: "No"`). Applicant self-applies for visa after offer.
  - If asked *"Can you provide your own visa/work permit for Germany?"* -> Select `Yes` (per `can_provide_own_germany_visa: "Yes"`).
  - If asked in free text about visa status -> Fill `germany_visa_statement` from config.
  - Decision table: sponsorship/employer-sponsor questions → No; own visa / work authorization / relocate readiness → Yes; never answer Yes to both "need sponsorship" and "have own visa" in a contradictory way.
  - Notice period unit conversion: `30` for days/Tage, `4` for weeks/Wochen, `1` for months/Monate, or `today + 35 days` for date inputs.
6. **CEFR Language Dropdowns:**
  - Match English -> `C2` / `Native or bilingual` / `Fluent` / `Verhandlungssicher`.
  - Match German -> `A1` / `Elementary` / `Beginner` / `Grundkenntnisse`.
  - Match Telugu -> `C2` / `Native or bilingual` / `Muttersprache`.
  - Match any other EU language (French, Dutch, Spanish) -> `None` / `No proficiency` / `0`.
7. **GDPR Checkboxes & Resume Upload:**
  - Auto-check mandatory GDPR, privacy policy (`Datenschutz`), and data storage consent checkboxes by clicking their `<label>`.
  - Upload the configured Resume PDF ONLY to file inputs whose container label matches `resume`, `cv`, or `lebenslauf`. Never upload the CV into a `Cover Letter` / `Anschreiben` file input if `uploads.Cover Letter` is empty.
  - Cover letter text fields / Anschreiben: if `cover_letter.enabled`, render `cover_letter.template` with job placeholders, optionally lightly adapt via Ollama (keep template structure), run `sanitize_human_text()`, then fill. User owns and corrects the template in config.
8. **Submit & False-Negative Fix:**
  - On the final step, uncheck `#follow-company-checkbox` (`Follow company`).
  - Click `Submit application` / `Bewerbung absenden`.
  - Treat submission as `APPLIED` if any confirmation text appears (`Application sent`, `Bewerbung wurde gesendet`), OR if `.jobs-easy-apply-modal` closes with zero `.artdeco-inline-feedback--error` banners. Dismiss any post-submit popup.

---



## 5. Non-Stopping Fault Recovery (`unattended_mode: true`) & Reporting

1. **Never Stop the Bot on Form Failures:**
  - In `_handle_validation_errors_with_learning()`, run up to 2 rounds of `_auto_fill_from_knowledge()`, feeding the exact `.artdeco-inline-feedback--error` text into the resolver (e.g., if LinkedIn says *"Enter a whole number between 0 and 99"*, force a clean integer).
  - If red validation errors remain after Round 2 and `session.unattended_mode` is `true`:
  1. Save screenshot + HTML to `crash_dumps/job_<job_id>_<timestamp>.png` and `.html`.
  2. Append the failed question + error message to `output/unanswered_questions.csv`.
  3. Click modal Close (`button[aria-label="Dismiss"]`) -> wait `500ms` -> click Confirm Discard (`button[data-control-name="discard_application_confirm_btn"]` or text `Discard` / `Verwerfen`).
  4. Log status `FAILED_VALIDATION` and immediately continue the loop to the next job card.
2. **End-of-Run Telemetry & Summary Report (**`src/reporter.py`**):**
  - Update `output/applications.csv` after every job card (`APPLIED`, `FAILED_VALIDATION`, `FAILED_EXCEPTION`, `SKIPPED_ALREADY_APPLIED`, `SKIPPED_BLACKLIST`, `SKIPPED_LANGUAGE`, `DRY_RUN`).
  - On exit (normal completion or `Ctrl+C`), print a clean console summary table and write `output/reports/run_summary_YYYY_MM_DD_HH_MM_SS.md` showing:
    - Total Scanned, Attempted, Applied (`APPLIED`), Failed (`FAILED_*`), and Skipped (`SKIPPED_*`)
    - Table of all successfully applied jobs (Company, Title, Location, Timestamp, Link)
    - List of any unanswered questions captured during the run so they can be reviewed.
3. Update `test_ollama_smoke.py` to verify `gemma4:e4b-it-q4_K_M`, `llama3.2:latest`, and `nomic-embed-text:latest`, testing EU salary (`75000 - 95000 EUR` vs `85000`), German visa questions, 30-day notice conversion, and `sanitize_human_text()`.

