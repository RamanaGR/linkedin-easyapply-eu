"""Shared helpers."""

from __future__ import annotations

import random
import re
import time
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def project_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def human_delay(min_s: float = 2.0, max_s: float = 6.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def short_delay(ms: int) -> None:
    time.sleep(ms / 1000.0)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def contains_any(text: str, keywords: list[str]) -> bool:
    t = normalize_text(text)
    return any(normalize_text(k) in t for k in keywords)


def notice_period_value(question: str, days: int, weeks: int, months: int) -> str:
    q = normalize_text(question)
    if any(k in q for k in ("month", "monat", "months", "monate")):
        return str(months)
    if any(k in q for k in ("week", "woche", "weeks", "wochen")):
        return str(weeks)
    if any(k in q for k in ("date", "datum", "available from", "start date", "ab wann")):
        return (date.today() + timedelta(days=days + 5)).strftime("%m/%d/%Y")
    return str(days)


def ensure_dirs() -> None:
    for rel in (
        "output",
        "output/reports",
        "crash_dumps",
        "assets",
        "chrome_profile",
    ):
        project_path(rel).mkdir(parents=True, exist_ok=True)
