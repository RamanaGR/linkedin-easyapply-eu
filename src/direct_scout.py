"""Direct (external) job scouting, ATS classification, AI match scoring, and Markdown queue."""

from __future__ import annotations

import csv
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from src.config import AppConfig, load_resume_json
from src.job_search import JobCard
from src.ollama_client import OllamaClient, sanitize_human_text
from src.utils import normalize_text, project_path, short_delay

logger = logging.getLogger("easyapply")


# ATS Domain & Path Rules: (substring, ats_name, tier)
# Tiers:
#   - "Fast 1-Page" (no account wall, 1-page upload + submit)
#   - "Account Portal" (requires login / multi-step portal)
#   - "Company Site" (custom domain career page)
ATS_RULES: tuple[tuple[str, str, str], ...] = (
    ("greenhouse.io", "Greenhouse", "Fast 1-Page"),
    ("grnh.se", "Greenhouse", "Fast 1-Page"),
    ("lever.co", "Lever", "Fast 1-Page"),
    ("ashbyhq.com", "Ashby", "Fast 1-Page"),
    ("personio.de", "Personio", "Fast 1-Page"),
    ("personio.com", "Personio", "Fast 1-Page"),
    ("recruitee.com", "Recruitee", "Fast 1-Page"),
    ("teamtailor.com", "Teamtailor", "Fast 1-Page"),
    ("join.com", "JOIN", "Fast 1-Page"),
    ("workable.com", "Workable", "Fast 1-Page"),
    ("bamboohr.com", "BambooHR", "Fast 1-Page"),
    ("pinpointhq.com", "Pinpoint", "Fast 1-Page"),
    ("dover.com", "Dover", "Fast 1-Page"),
    ("breezy.hr", "BreezyHR", "Fast 1-Page"),
    ("workdayjobs.com", "Workday", "Account Portal"),
    ("myworkdayjobs.com", "Workday", "Account Portal"),
    ("myworkdaysite.com", "Workday", "Account Portal"),
    ("smartrecruiters.com", "SmartRecruiters", "Account Portal"),
    ("successfactors.", "SAP SuccessFactors", "Account Portal"),
    ("jobs2web.com", "SAP SuccessFactors", "Account Portal"),
    ("icims.com", "iCIMS", "Account Portal"),
    ("taleo.net", "Oracle Taleo", "Account Portal"),
    ("oraclecloud.com", "Oracle Cloud HCM", "Account Portal"),
    ("avature.net", "Avature", "Account Portal"),
    ("eightfold.ai", "Eightfold", "Account Portal"),
    ("softgarden.io", "Softgarden", "Fast 1-Page"),
    ("onlyfy.jobs", "Onlyfy", "Fast 1-Page"),
)

EASY_APPLY_MARKERS = (
    "easy apply",
    "einfach bewerben",
    "candidatura semplificata",
    "solliciteren",
    "candidature simplifiée",
)

DIRECT_CSV_FIELDS = [
    "timestamp",
    "job_id",
    "score",
    "ats_name",
    "ats_tier",
    "title",
    "company",
    "location",
    "matched_skills",
    "visa_language_note",
    "reason",
    "external_url",
    "linkedin_url",
]


@dataclass
class DirectJobEvaluation:
    job_id: str
    title: str
    company: str
    location: str
    linkedin_url: str
    external_url: str
    ats_name: str
    ats_tier: str
    score: int
    matched_skills: list[str] = field(default_factory=list)
    visa_language_note: str = ""
    reason: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )


def extract_target_url_from_href(href: str) -> str:
    """Unwrap LinkedIn `/safety/go/?url=...` or externalApply redirect URLs if present."""
    raw = (href or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        for key in ("url", "trkEmail", "dest", "redirect"):
            vals = qs.get(key)
            if vals and vals[0].startswith("http"):
                return unquote(vals[0]).strip()
    except Exception:
        pass
    if raw.startswith("http") and "linkedin.com" not in raw.lower():
        return raw
    return ""


def classify_ats_url(url: str) -> tuple[str, str]:
    """Return (ats_name, ats_tier) from an external application URL."""
    unwrapped = extract_target_url_from_href(url) or (url or "").strip()
    raw = unwrapped.lower()
    if not raw or "linkedin.com" in raw:
        return ("LinkedIn Redirect", "Company Site")
    for pattern, name, tier in ATS_RULES:
        if pattern in raw:
            return (name, tier)
    try:
        host = (urlparse(raw).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            return (f"Company Site ({host})", "Company Site")
    except Exception:
        pass
    return ("Company Site", "Company Site")


class DirectJobScout:
    """Inspects LinkedIn job pages for external apply links, scores JD fit, and writes Markdown queue."""

    def __init__(
        self,
        driver: WebDriver | None,
        config: AppConfig,
        ollama: OllamaClient | None = None,
    ) -> None:
        self.driver = driver
        self.config = config
        self.ollama = ollama
        self.resume = load_resume_json()
        self.seen_path = project_path("output", "direct_seen_jobs.csv")
        self.csv_path = project_path("output", "direct_jobs.csv")
        self.queue_path = project_path(
            self.config.direct_apply.queue_file or "output/direct_jobs_queue.md"
        )
        self.seen_ids = self._load_seen()
        self.collected_this_run: list[DirectJobEvaluation] = []
        self._ensure_files()

    def _ensure_files(self) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=DIRECT_CSV_FIELDS).writeheader()

    def _load_seen(self) -> set[str]:
        ids: set[str] = set()
        if not self.seen_path.exists():
            return ids
        try:
            with self.seen_path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    jid = (row.get("job_id") or "").strip()
                    if jid:
                        ids.add(jid)
        except Exception as exc:
            logger.debug("Could not load direct_seen_jobs.csv: %s", exc)
        return ids

    def mark_seen(self, job_id: str, status: str, score: int = 0) -> None:
        if not job_id:
            return
        self.seen_ids.add(job_id)
        exists = self.seen_path.exists()
        with self.seen_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["job_id", "status", "score", "timestamp"]
            )
            if not exists:
                writer.writeheader()
            writer.writerow(
                {
                    "job_id": job_id,
                    "status": status,
                    "score": score,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            )

    def inspect_apply_type(self) -> tuple[str, object | None, str]:
        """Inspect the current LinkedIn job view page.

        Returns ``(kind, element, direct_href)`` where ``kind`` is one of:
          - ``"easy_apply"``: Job uses LinkedIn Easy Apply modal
          - ``"direct"``: Job has an external Apply button/link
          - ``"none"``: No apply control found (or closed job)
        """
        assert self.driver
        try:
            info = self.driver.execute_script(
                """
                const easyMarkers = arguments[0];
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const inModal = (el) => !!(el && el.closest(
                    'dialog, .artdeco-modal, [role="dialog"], .jobs-easy-apply-modal'
                ));
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 4 && r.height > 4;
                };

                const selectors = [
                    'a[aria-label*="Apply on company website"]',
                    'button[aria-label*="Apply on company website"]',
                    'a[aria-label*="Auf Unternehmenswebsite bewerben"]',
                    'a[href*="linkedin.com/safety/go/"]',
                    'button[aria-label*="Easy Apply"]',
                    'button[aria-label*="Einfach bewerben"]',
                    'button.jobs-apply-button',
                    'a.jobs-apply-button',
                    '#jobs-apply-button-id',
                    'button[data-live-test-job-apply-button]',
                    'a[data-live-test-job-apply-button]',
                    '.jobs-s-apply button',
                    '.jobs-s-apply a',
                    '.jobs-apply-button--top-card button',
                    '.jobs-apply-button--top-card a',
                    'button[aria-label*="Apply"]',
                    'a[aria-label*="Apply"]',
                    'button[aria-label*="Bewerben"]',
                    'a[aria-label*="Bewerben"]'
                ];

                const seen = new Set();
                const candidates = [];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (seen.has(el) || inModal(el) || !isVisible(el)) continue;
                        const href = (el.getAttribute('href') || '').toLowerCase();
                        if (href.includes('/jobs/search-results/') || href.includes('/jobs/collections/')) {
                            continue;
                        }
                        seen.add(el);
                        candidates.push(el);
                    }
                }

                // Fallback: look for primary top-card buttons/links with Apply text
                if (candidates.length === 0) {
                    const roots = document.querySelectorAll(
                        '.job-details-jobs-unified-top-card, .jobs-unified-top-card, '
                        + '.jobs-details__main-content, main'
                    );
                    const scope = roots.length ? roots : [document.body];
                    for (const root of scope) {
                        for (const el of root.querySelectorAll('a, button')) {
                            if (seen.has(el) || inModal(el) || !isVisible(el)) continue;
                            const href = (el.getAttribute('href') || '').toLowerCase();
                            if (href.includes('/jobs/search-results/') || href.includes('/jobs/collections/')) {
                                continue;
                            }
                            const text = norm(el.innerText || el.textContent);
                            const aria = norm(el.getAttribute('aria-label'));
                            const blob = text + ' ' + aria;
                            if (
                                (text === 'apply' || text === 'bewerben'
                                 || text.startsWith('apply ') || aria.includes('apply')
                                 || easyMarkers.some(m => blob.includes(m)))
                                && !blob.includes('save')
                                && !blob.includes('saved')
                                && !blob.includes('filter')
                                && !blob.includes('how jobgether works')
                            ) {
                                seen.add(el);
                                candidates.push(el);
                            }
                        }
                    }
                }

                for (const el of candidates) {
                    const text = norm(el.innerText || el.textContent);
                    const aria = norm(el.getAttribute('aria-label'));
                    const blob = text + ' ' + aria;
                    const href = (el.getAttribute('href') || '').trim();
                    for (const m of easyMarkers) {
                        if (blob.includes(m)) {
                            return { kind: 'easy_apply', el: el, href: href };
                        }
                    }
                    const html = norm(el.innerHTML);
                    if (html.includes('linkedin-bug') && blob.includes('easy')) {
                        return { kind: 'easy_apply', el: el, href: href };
                    }
                }

                for (const el of candidates) {
                    const text = norm(el.innerText || el.textContent);
                    const aria = norm(el.getAttribute('aria-label'));
                    const blob = text + ' ' + aria;
                    const href = (el.getAttribute('href') || '').trim();
                    if (blob.includes('save') || blob.includes('how jobgether works')) continue;
                    if (
                        aria.includes('apply on company website')
                        || aria.includes('unternehmenswebsite')
                        || text === 'apply'
                        || text === 'bewerben'
                        || blob.includes('apply')
                        || blob.includes('bewerben')
                    ) {
                        return { kind: 'direct', el: el, href: href };
                    }
                }

                return { kind: 'none', el: null, href: '' };
                """,
                list(EASY_APPLY_MARKERS),
            )
            if not isinstance(info, dict):
                return ("none", None, "")
            return (
                str(info.get("kind") or "none"),
                info.get("el"),
                str(info.get("href") or ""),
            )
        except Exception as exc:
            logger.debug("inspect_apply_type failed: %s", exc)
            return ("none", None, "")

    def resolve_external_apply_url(
        self, apply_el: object | None, direct_href: str, fallback_linkedin_url: str
    ) -> tuple[str, str, str]:
        """Resolve external company/ATS apply URL and classify its ATS.

        If ``resolve_external_url`` is enabled and the Apply button opens a new tab,
        captures the target URL, closes the newly opened tab immediately, and switches
        back to the LinkedIn tab.
        """
        # Fast path: unwrap LinkedIn safety/go/?url=... or direct external URL without opening a tab
        unwrapped = extract_target_url_from_href(direct_href)
        if unwrapped:
            ats_name, ats_tier = classify_ats_url(unwrapped)
            return (unwrapped, ats_name, ats_tier)

        if not self.config.direct_apply.resolve_external_url or self.driver is None:
            url = direct_href or fallback_linkedin_url
            ats_name, ats_tier = classify_ats_url(url)
            return (url, ats_name, ats_tier)

        if apply_el is None:
            ats_name, ats_tier = classify_ats_url(fallback_linkedin_url)
            return (fallback_linkedin_url, ats_name, ats_tier)

        original_handle = self.driver.current_window_handle
        original_handles = set(self.driver.window_handles)
        resolved_url = direct_href or fallback_linkedin_url

        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", apply_el
            )
            short_delay(200)
            try:
                apply_el.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", apply_el)

            # Wait up to 5s for a new tab to appear
            new_handle = None
            deadline = time.time() + 5.0
            while time.time() < deadline:
                current_handles = set(self.driver.window_handles)
                diff = current_handles - original_handles
                if diff:
                    new_handle = next(iter(diff))
                    break
                time.sleep(0.25)

            if new_handle:
                try:
                    self.driver.switch_to.window(new_handle)
                    # Wait briefly for LinkedIn redirect / external URL to settle
                    wait_end = time.time() + 6.0
                    while time.time() < wait_end:
                        cur = (self.driver.current_url or "").strip()
                        if (
                            cur
                            and cur != "about:blank"
                            and "linkedin.com/jobs" not in cur.lower()
                            and "linkedin.com/redir" not in cur.lower()
                        ):
                            resolved_url = cur
                            break
                        if cur and cur != "about:blank":
                            resolved_url = cur
                        time.sleep(0.35)
                finally:
                    # Always close extra tab(s) and return to original tab
                    for h in list(self.driver.window_handles):
                        if h != original_handle and h not in original_handles:
                            try:
                                self.driver.switch_to.window(h)
                                self.driver.close()
                            except Exception:
                                pass
                    self.driver.switch_to.window(original_handle)
            else:
                # Check if a safety modal / external redirect link appeared on same page
                short_delay(400)
                ext_href = self.driver.execute_script(
                    """
                    const links = document.querySelectorAll(
                        'a[href*="externalApply"], a[data-tracking-control-name*="external"], '
                        + 'dialog a[href^="http"], .artdeco-modal a[href^="http"]'
                    );
                    for (const a of links) {
                        const h = (a.getAttribute('href') || '').trim();
                        if (h && !h.includes('linkedin.com/jobs/view')) return h;
                    }
                    return '';
                    """
                )
                if ext_href:
                    resolved_url = str(ext_href)
                # If an Easy Apply dialog accidentally opened, dismiss it
                self._dismiss_any_open_dialog()
        except Exception as exc:
            logger.debug("External URL resolution fallback (%s)", exc)
            try:
                if self.driver.current_window_handle != original_handle:
                    self.driver.switch_to.window(original_handle)
            except Exception:
                pass

        ats_name, ats_tier = classify_ats_url(resolved_url)
        return (resolved_url, ats_name, ats_tier)

    def _dismiss_any_open_dialog(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.execute_script(
                """
                const btn = document.querySelector(
                    'dialog[open] button[aria-label="Dismiss"], '
                    + '.artdeco-modal button[aria-label="Dismiss"]'
                );
                if (btn) btn.click();
                """
            )
        except Exception:
            pass

    def extract_matched_skills(self, title: str, description: str) -> list[str]:
        """Find candidate's configured skills and tech keywords mentioned in the JD."""
        blob = normalize_text(f"{title} {description}")
        candidates: list[str] = []
        for sk in self.config.experience.skill_years.keys():
            if sk and sk not in candidates:
                candidates.append(sk)
        for tk in self.config.tech_keywords:
            if tk and tk not in candidates:
                candidates.append(tk)
        for rs in self.resume.get("skills") or []:
            if isinstance(rs, str) and rs not in candidates:
                candidates.append(rs)

        matched: list[str] = []
        seen_low: set[str] = set()
        for skill in candidates:
            s_norm = normalize_text(skill)
            if not s_norm or s_norm in seen_low:
                continue
            # Handle short acronyms like AI, ML, SQL, AWS, GCP, MCP, RAG with word boundaries
            if len(s_norm) <= 4 and " " not in s_norm:
                hit = (
                    re.search(rf"(?<![a-z0-9]){re.escape(s_norm)}(?![a-z0-9])", blob)
                    is not None
                )
            else:
                hit = s_norm in blob
            if hit:
                seen_low.add(s_norm)
                matched.append(skill)
        return matched

    def detect_visa_and_language_note(self, description: str) -> str:
        """Rule-based check for visa sponsorship, EU work permit, or German language cues."""
        t = normalize_text(description)
        notes: list[str] = []

        if any(
            p in t
            for p in (
                "visa sponsorship",
                "relocation support",
                "relocation package",
                "blue card",
                "chancenkarte",
                "sponsor visa",
            )
        ):
            notes.append("Relocation/Visa friendly")
        elif any(
            p in t
            for p in (
                "no visa sponsorship",
                "cannot sponsor",
                "unable to sponsor",
                "must have a valid work permit",
                "valid eu work permit",
            )
        ):
            notes.append("Note: Mentions EU permit / no employer sponsorship")

        if any(
            p in t
            for p in (
                "english is our working language",
                "working language is english",
                "english-speaking",
                "fluent in english",
                "fluent english",
            )
        ):
            notes.append("English OK")
        if any(p in t for p in ("german b1", "german b2", "deutsch b1", "deutsch b2")):
            notes.append("Mentions German B1/B2")

        return " | ".join(notes) if notes else "English / Standard EU role"

    def score_job_match(
        self, card: JobCard, description: str
    ) -> tuple[int, list[str], str, str]:
        """Compute hybrid match score (0-100), matched skills, visa/language note, and 1-line reason."""
        matched_skills = self.extract_matched_skills(card.title, description)
        visa_note = self.detect_visa_and_language_note(description)

        # Deterministic base score from title + skill overlap
        # Title already passed title_mismatch_reason in bot loop -> base 50
        skill_count = len(matched_skills)
        core_ai_terms = {
            "python",
            "llm",
            "rag",
            "langgraph",
            "langchain",
            "generative ai",
            "agentic ai",
            "google adk",
            "mcp",
            "fastapi",
            "aws",
            "mlops",
        }
        core_hits = sum(
            1 for s in matched_skills if normalize_text(s) in core_ai_terms
        )
        rule_score = min(100, 56 + min(skill_count * 5, 28) + min(core_hits * 4, 16))
        if "Mentions German B1/B2" in visa_note:
            rule_score = max(40, rule_score - 10)

        default_reason = (
            f"Matches {skill_count} profile skills ({', '.join(matched_skills[:5])})"
            if matched_skills
            else f"Role title aligns with target position ({card.title})"
        )

        if (
            not self.config.direct_apply.use_ollama_scoring
            or self.ollama is None
            or not (description or "").strip()
        ):
            return (rule_score, matched_skills[:8], visa_note, default_reason)

        try:
            llm_score, llm_reason, llm_visa = self._ollama_score_jd(
                card, description, matched_skills
            )
            # Blend 60% LLM score + 40% deterministic rule score to prevent hallucinated extremes
            final_score = int(round(0.6 * llm_score + 0.4 * rule_score))
            final_score = max(0, min(100, final_score))
            reason = llm_reason or default_reason
            if llm_visa and llm_visa not in visa_note:
                visa_note = f"{visa_note} ({llm_visa})" if visa_note else llm_visa
            return (final_score, matched_skills[:8], visa_note, reason)
        except Exception as exc:
            logger.debug("Ollama JD scoring fallback to rule score: %s", exc)
            return (rule_score, matched_skills[:8], visa_note, default_reason)

    def _ollama_score_jd(
        self, card: JobCard, description: str, matched_skills: list[str]
    ) -> tuple[int, str, str]:
        """Ask local Ollama intent/chat model for a JSON match evaluation."""
        assert self.ollama
        jd_excerpt = re.sub(r"\s+", " ", (description or "").strip())[:2800]
        system = (
            "You evaluate how well a job description matches a candidate profile. "
            "Use 95-100 when the role is a strong AI/LLM/ML engineering match with multiple overlapping skills. "
            "Respond with ONLY valid JSON containing keys: "
            '{"score": <int 0-100>, "reason": "<max 18 words>", "visa_or_language": "<max 8 words>"}.'
        )
        user = (
            f"Candidate Profile:\n{self.config.context_blob()}\n"
            f"Matched skills found in JD: {', '.join(matched_skills[:10])}\n\n"
            f"Job Title: {card.title}\n"
            f"Company: {card.company}\n"
            f"Location: {card.location}\n"
            f"Job Description Excerpt:\n{jd_excerpt}\n\n"
            "Return JSON only:"
        )
        raw = self.ollama._chat(self.ollama.env.ollama_intent_model, system, user)
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            num = re.search(r"\b(\d{2,3})\b", raw)
            score_val = int(num.group(1)) if num else 75
            return (max(0, min(100, score_val)), "", "")
        data = json.loads(m.group(0))
        score_val = int(data.get("score", 75))
        score_val = max(0, min(100, score_val))
        reason = sanitize_human_text(str(data.get("reason") or ""))[:140]
        visa = sanitize_human_text(str(data.get("visa_or_language") or ""))[:60]
        return (score_val, reason, visa)

    def record_collected_job(self, item: DirectJobEvaluation) -> None:
        """Persist a high-matching direct job to CSV, seen tracker, and Markdown queue."""
        self.collected_this_run.append(item)
        self.mark_seen(item.job_id, "COLLECTED_DIRECT", score=item.score)

        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DIRECT_CSV_FIELDS)
            writer.writerow(
                {
                    "timestamp": item.timestamp,
                    "job_id": item.job_id,
                    "score": item.score,
                    "ats_name": item.ats_name,
                    "ats_tier": item.ats_tier,
                    "title": item.title,
                    "company": item.company,
                    "location": item.location,
                    "matched_skills": ", ".join(item.matched_skills),
                    "visa_language_note": item.visa_language_note,
                    "reason": item.reason,
                    "external_url": item.external_url,
                    "linkedin_url": item.linkedin_url,
                }
            )
        self.write_markdown_queues()

    def _load_all_saved_jobs(self) -> list[DirectJobEvaluation]:
        """Load all collected direct jobs from `output/direct_jobs.csv` (deduped by job_id)."""
        if not self.csv_path.exists():
            return list(self.collected_this_run)
        by_id: dict[str, DirectJobEvaluation] = {}
        try:
            with self.csv_path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    jid = (row.get("job_id") or "").strip()
                    if not jid:
                        continue
                    try:
                        sc = int(float(row.get("score") or 0))
                    except ValueError:
                        sc = 0
                    skills_raw = (row.get("matched_skills") or "").strip()
                    skills = [s.strip() for s in skills_raw.split(",") if s.strip()]
                    by_id[jid] = DirectJobEvaluation(
                        job_id=jid,
                        title=(row.get("title") or "").strip(),
                        company=(row.get("company") or "").strip(),
                        location=(row.get("location") or "").strip(),
                        linkedin_url=(row.get("linkedin_url") or "").strip(),
                        external_url=(row.get("external_url") or "").strip(),
                        ats_name=(row.get("ats_name") or "Company Site").strip(),
                        ats_tier=(row.get("ats_tier") or "Company Site").strip(),
                        score=sc,
                        matched_skills=skills,
                        visa_language_note=(row.get("visa_language_note") or "").strip(),
                        reason=(row.get("reason") or "").strip(),
                        timestamp=(row.get("timestamp") or "").strip(),
                    )
        except Exception as exc:
            logger.warning("Could not read direct_jobs.csv: %s", exc)
        for item in self.collected_this_run:
            by_id[item.job_id] = item
        return list(by_id.values())

    def _preserve_checked_ids(self, md_path: Path) -> set[str]:
        """Keep `[x]` checked state if user already ticked off jobs in `direct_jobs_queue.md`."""
        checked: set[str] = set()
        if not md_path.exists():
            return checked
        try:
            for line in md_path.read_text(encoding="utf-8").splitlines():
                if re.match(r"^\s*-\s*\[[xX]\]", line):
                    m = re.search(r"job_id:(\d+)", line) or re.search(
                        r"/jobs/view/(\d+)", line
                    )
                    if m:
                        checked.add(m.group(1))
        except Exception:
            pass
        return checked

    def write_markdown_queues(self) -> tuple[Path, Path]:
        """Write master `output/direct_jobs_queue.md` and dated `output/reports/direct_jobs_YYYY_MM_DD.md`."""
        checked_ids = self._preserve_checked_ids(self.queue_path)
        all_jobs = self._load_all_saved_jobs()
        all_jobs.sort(key=lambda j: (j.score, j.timestamp), reverse=True)

        master_content = self._render_markdown(
            title="Direct Apply Job Queue (AI-Ranked)",
            jobs=all_jobs,
            checked_ids=checked_ids,
        )
        self.queue_path.write_text(master_content, encoding="utf-8")

        reports_dir = project_path("output", "reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        today_stamp = datetime.now().strftime("%Y_%m_%d")
        dated_path = reports_dir / f"direct_jobs_{today_stamp}.md"
        run_jobs = sorted(
            self.collected_this_run,
            key=lambda j: (j.score, j.timestamp),
            reverse=True,
        )
        dated_content = self._render_markdown(
            title=f"Direct Jobs Collected ({today_stamp})",
            jobs=run_jobs if run_jobs else all_jobs,
            checked_ids=checked_ids,
        )
        dated_path.write_text(dated_content, encoding="utf-8")
        return (self.queue_path, dated_path)

    def _render_markdown(
        self,
        title: str,
        jobs: list[DirectJobEvaluation],
        checked_ids: set[str],
    ) -> str:
        fast_ats = [j for j in jobs if j.ats_tier == "Fast 1-Page"]
        other_ats = [j for j in jobs if j.ats_tier != "Fast 1-Page"]
        pending_count = sum(1 for j in jobs if j.job_id not in checked_ids)
        done_count = sum(1 for j in jobs if j.job_id in checked_ids)

        lines: list[str] = [
            f"# {title}",
            "",
            f"- **Updated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **Total Queued**: {len(jobs)} ({pending_count} pending, {done_count} applied)",
            f"- **Fast 1-Page ATS (Greenhouse / Personio / Lever / Ashby)**: {len(fast_ats)}",
            f"- **Company Portals / Workday / Custom Sites**: {len(other_ats)}",
            f"- **Min Score Filter**: {self.config.direct_apply.min_match_score}%",
            "",
            "> Tip: Mark `- [ ]` as `- [x]` when you finish applying — your checkmarks are preserved across runs.",
            "",
        ]

        if not jobs:
            lines.append("_No matching direct jobs collected yet._")
            return "\n".join(lines) + "\n"

        if fast_ats:
            lines.extend(
                [
                    "## Tier 1: Fast 1-Page ATS (No Account Needed — Apply First)",
                    "",
                ]
            )
            for job in fast_ats:
                lines.extend(self._format_job_entry(job, checked_ids))

        if other_ats:
            lines.extend(
                [
                    "## Tier 2: Company Career Pages & Portals (Workday / SAP / Custom)",
                    "",
                ]
            )
            for job in other_ats:
                lines.extend(self._format_job_entry(job, checked_ids))

        return "\n".join(lines) + "\n"

    def _format_job_entry(
        self, job: DirectJobEvaluation, checked_ids: set[str]
    ) -> list[str]:
        box = "x" if job.job_id in checked_ids else " "
        skills_str = ", ".join(job.matched_skills) if job.matched_skills else "General fit"
        ext_link = job.external_url or job.linkedin_url
        li_link = job.linkedin_url or f"https://www.linkedin.com/jobs/view/{job.job_id}"
        return [
            f"- [{box}] **[{job.score}%]** **{job.title}** @ **{job.company}** ({job.location}) — `{job.ats_name}` <!-- job_id:{job.job_id} -->",
            f"  - **Apply Link**: [Open {job.ats_name} Application]({ext_link}) | [LinkedIn Post]({li_link})",
            f"  - **Matched Skills**: {skills_str}",
            f"  - **Why / Notes**: {job.reason} — _{job.visa_language_note}_",
            "",
        ]
