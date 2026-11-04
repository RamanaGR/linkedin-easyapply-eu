#!/usr/bin/env python3
"""Offline smoke tests for Ollama models + EU answer helpers."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from src.config import load_config, load_env
from src.ollama_client import OllamaClient, sanitize_human_text, truncate_at_word
from src.qa_intent import IntentMatcher
from src.utils import notice_period_value


def test_sanitize() -> None:
    raw = 'Furthermore, I spearheaded — leveraging "AI"; **great**'
    out = sanitize_human_text(raw)
    assert "—" not in out and "–" not in out
    assert ";" not in out
    assert "**" not in out
    assert "spearheaded" not in out.lower()
    assert "Furthermore" not in out
    print("OK sanitize_human_text:", out)


def test_sanitize_resume_preamble() -> None:
    samples = [
        "My resume details: I built RAG systems with Python.",
        "Based on my resume, I have 8 years of experience.",
        "According to my profile I worked on FastAPI services.",
        "My resume details Python Kafka Docker I used these tools.",
    ]
    for raw in samples:
        out = sanitize_human_text(raw)
        low = out.lower()
        assert not low.startswith("my resume details"), out
        assert not low.startswith("based on my resume"), out
        assert not low.startswith("according to my profile"), out
        assert out.strip()
        print("OK preamble stripped:", out[:80])


def test_truncate() -> None:
    text = "I built APIs with FastAPI and deployed them on AWS with Docker."
    out = truncate_at_word(text, 25)
    assert len(out) <= 25
    assert "FastAPI" not in out or len(out) <= 25
    print("OK truncate_at_word:", out)


def test_notice() -> None:
    assert notice_period_value("Notice period in days", 30, 4, 1) == "30"
    assert notice_period_value("Kündigungsfrist Wochen", 30, 4, 1) == "4"
    assert notice_period_value("Notice months", 30, 4, 1) == "1"
    print("OK notice_period_value")


def test_visa_intent(config, ollama) -> None:
    intent = IntentMatcher(config, ollama)
    opts = ["Yes", "No"]
    assert intent.pick("Will you require visa sponsorship?", opts) == "No"
    assert intent.pick("Can you provide your own visa/work permit for Germany?", opts) == "Yes"
    print("OK visa intent rules")


def test_salary(config) -> None:
    c = config.compensation_eu
    assert c.expected_salary_range_text == "75000 - 95000 EUR"
    assert c.expected_salary_eur_target == 85000
    print("OK salary config", c.expected_salary_range_text, c.expected_salary_eur_target)


def test_salary_numeric_vs_range(config) -> None:
    """Mirror ApplicationHandler._salary_answer rules without Selenium."""
    from src.application import ApplicationHandler

    # Minimal stub — only needs config for salary helper
    handler = object.__new__(ApplicationHandler)
    handler.config = config
    numeric = handler._salary_answer("Expected annual salary in EUR", numeric=True)
    free = handler._salary_answer("What is your expected salary range?", numeric=False)
    assert numeric == "85000", numeric
    assert "EUR" in free or "-" in free, free
    assert numeric.isdigit()
    assert not free.replace(" ", "").isdigit()
    print("OK salary numeric vs range:", numeric, "|", free)


def test_shape_digits_and_maxlen(config) -> None:
    from src.application import ApplicationHandler

    handler = object.__new__(ApplicationHandler)
    handler.config = config
    handler.ollama = SimpleNamespace(env=SimpleNamespace(easyapply_debug_fill=False))

    class FakeEl:
        def __init__(self, typ="text", maxlength=None, inputmode="", pattern=""):
            self._a = {
                "type": typ,
                "maxlength": maxlength,
                "inputmode": inputmode,
                "pattern": pattern,
                "aria-label": "",
                "placeholder": "",
                "name": "",
            }

        def get_attribute(self, name):
            return self._a.get(name)

    num_el = FakeEl(typ="number")
    assert handler._field_wants_digits_only(num_el, "Expected salary")
    shaped = handler._shape_answer_for_field(
        num_el, "Expected salary EUR", "75000 - 95000 EUR"
    )
    assert shaped == "85000", shaped

    short_el = FakeEl(typ="text", maxlength="40")
    long = "I have many years of experience building production systems with Python and Java."
    handler._field_max_length = lambda el: 40  # type: ignore[method-assign]
    handler._field_wants_digits_only = lambda el, label="": False  # type: ignore[method-assign]
    handler._is_years_question = lambda label: False  # type: ignore[method-assign]
    truncated = handler._shape_answer_for_field(short_el, "Summary", long)
    assert len(truncated) <= 40, truncated
    print("OK shape digits/maxlen:", shaped, "|", truncated)


def test_years_and_counters(config) -> None:
    from src.application import ApplicationHandler

    handler = object.__new__(ApplicationHandler)
    handler.config = config
    handler.ollama = SimpleNamespace(env=SimpleNamespace(easyapply_debug_fill=False))

    assert handler._is_years_question(
        "How many years of work experience do you have with C++?"
    )
    assert handler._is_years_question("¿Cuántos años de experiencia tienes con Python?*")
    assert handler._is_years_question("Hoeveel jaar werkervaring hebt u met ASP.NET?")
    assert handler._years_answer_for_question(
        "How many years of work experience do you have with Python?"
    ) == "8"
    assert handler._years_answer_for_question(
        "How many years of work experience do you have with C++?"
    ) == "0"

    # Character counters must NOT be treated as validation errors
    assert handler._is_char_counter_text("6/100")
    assert handler._is_char_counter_text("1 of 20 characters")
    assert handler._is_char_counter_text("First name*\n6/100\n6 of 100 characters")
    assert not handler._is_char_counter_text("Invalid input")
    assert not handler._is_char_counter_text("This field is required")

    class FakeEl:
        def get_attribute(self, name):
            return {"type": "text", "maxlength": "20"}.get(name)

    el = FakeEl()
    handler._field_max_length = lambda e: 20  # type: ignore[method-assign]
    shaped = handler._shape_answer_for_field(
        el,
        "¿Cuántos años de experiencia tienes con Python?",
        "I have 8 years of experience with Python.",
    )
    assert shaped == "8", shaped
    assert len(shaped) <= 20
    print("OK years/counters:", shaped)


def test_direct_scout(config, ollama: OllamaClient | None = None) -> None:
    import tempfile
    from pathlib import Path
    from src.direct_scout import DirectJobEvaluation, DirectJobScout, classify_ats_url
    from src.job_search import JobCard, JobSearch

    # 1) ATS classification
    assert classify_ats_url("https://boards.greenhouse.io/acme/jobs/12345") == (
        "Greenhouse",
        "Fast 1-Page",
    )
    assert classify_ats_url("https://acme.jobs.personio.de/job/98765") == (
        "Personio",
        "Fast 1-Page",
    )
    assert classify_ats_url("https://acme.wd3.myworkdayjobs.com/en-US/careers/job/1") == (
        "Workday",
        "Account Portal",
    )
    custom_name, custom_tier = classify_ats_url("https://careers.deeptech.eu/openings/ai-eng")
    assert "deeptech.eu" in custom_name and custom_tier == "Company Site"

    # 2) Search URL builder with easy_apply_only=False
    js = JobSearch(driver=None, config=config)  # type: ignore[arg-type]
    url_ea = js.build_search_url("Senior AI Engineer", "Germany", easy_apply_only=True)
    url_direct = js.build_search_url("Senior AI Engineer", "Germany", easy_apply_only=False)
    assert "f_AL=true" in url_ea, url_ea
    assert "f_AL=true" not in url_direct, url_direct

    # 3) Skill extraction, scoring, and Markdown queue rendering (isolated temp files)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        scout = DirectJobScout(driver=None, config=config, ollama=ollama)
        scout.seen_path = tmp_dir / "direct_seen_jobs.csv"
        scout.csv_path = tmp_dir / "direct_jobs.csv"
        scout.queue_path = tmp_dir / "direct_jobs_queue.md"
        scout.seen_ids = set()
        scout._ensure_files()

        card = JobCard(
            job_id="99887766",
            title="Senior AI Engineer",
            company="EuroAI GmbH",
            location="Berlin, Germany",
            link="https://www.linkedin.com/jobs/view/99887766",
        )
        jd = (
            "We are looking for a Senior AI Engineer in Berlin. "
            "You will build production RAG and Agentic AI systems using Python, "
            "LangGraph, LangChain, FastAPI, Docker, and AWS. "
            "Working language is English. Relocation support / Blue Card possible."
        )
        score, matched, visa_note, reason = scout.score_job_match(card, jd)
        assert 95 <= score <= 100, f"Unexpected score: {score}"
        assert "Python" in matched and "LangGraph" in matched, matched
        assert "English OK" in visa_note or "Relocation" in visa_note, visa_note

        eval_fast = DirectJobEvaluation(
            job_id=card.job_id,
            title=card.title,
            company=card.company,
            location=card.location,
            linkedin_url=card.link,
            external_url="https://boards.greenhouse.io/euroai/jobs/99887766",
            ats_name="Greenhouse",
            ats_tier="Fast 1-Page",
            score=score,
            matched_skills=matched,
            visa_language_note=visa_note,
            reason=reason,
        )
        scout.record_collected_job(eval_fast)
        md_text = scout.queue_path.read_text(encoding="utf-8")
        assert "Tier 1: Fast 1-Page ATS" in md_text
        assert "Greenhouse" in md_text
        assert "EuroAI GmbH" in md_text

        # Verify user checkmarks [x] are preserved when regenerating queue
        scout.queue_path.write_text(md_text.replace("- [ ]", "- [x]"), encoding="utf-8")
        scout.write_markdown_queues()
        updated_md = scout.queue_path.read_text(encoding="utf-8")
        assert "- [x]" in updated_md, "User checkmark [x] was not preserved!"

    print(
        f"OK direct_scout: score={score}% matched={matched[:5]} ats=Greenhouse/Personio/Workday"
    )


def test_models(ollama: OllamaClient) -> None:
    models = ollama.ensure_models()
    print("OK models available:", models)
    # Tiny chat with constraints
    ans = ollama.answer_question(
        "What is your expected salary in EUR as a single number?",
        "Expected salary target is 85000 EUR. Range 75000 - 95000 EUR.",
        max_chars=12,
        digits_only=True,
    )
    print("OK chat answer:", ans)
    assert ans.isdigit() or ans == "", ans
    emb = ollama.embed("visa sponsorship question")
    assert len(emb) > 10
    print("OK embedding dim:", len(emb))
    choice = ollama.match_option(
        "Do you require sponsorship?",
        ["Yes", "No"],
        hint="No",
    )
    print("OK intent option:", choice)


def main() -> int:
    env = load_env()
    try:
        config = load_config()
    except FileNotFoundError as exc:
        print(exc)
        print("Copy config.example.yaml -> config.yaml first.")
        return 1

    test_sanitize()
    test_sanitize_resume_preamble()
    test_truncate()
    test_notice()
    test_salary(config)
    test_salary_numeric_vs_range(config)
    test_shape_digits_and_maxlen(config)
    test_years_and_counters(config)
    test_direct_scout(config, ollama=None)

    ollama = OllamaClient(env)
    try:
        test_models(ollama)
        test_visa_intent(config, ollama)
        test_direct_scout(config, ollama=ollama)
    except Exception as exc:
        print("FAIL ollama:", exc)
        return 2
    finally:
        ollama.close()

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
