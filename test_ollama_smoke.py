#!/usr/bin/env python3
"""Offline smoke tests for Ollama models + EU answer helpers."""

from __future__ import annotations

import sys

from src.config import load_config, load_env
from src.ollama_client import OllamaClient, sanitize_human_text
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


def test_models(ollama: OllamaClient) -> None:
    models = ollama.ensure_models()
    print("OK models available:", models)
    # Tiny chat
    ans = ollama.answer_question(
        "What is your expected salary in EUR as a single number?",
        "Expected salary target is 85000 EUR. Range 75000 - 95000 EUR.",
    )
    print("OK chat answer:", ans)
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
    test_notice()
    test_salary(config)

    ollama = OllamaClient(env)
    try:
        test_models(ollama)
        test_visa_intent(config, ollama)
    except Exception as exc:
        print("FAIL ollama:", exc)
        return 2
    finally:
        ollama.close()

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
