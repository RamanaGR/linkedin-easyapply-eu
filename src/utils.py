"""Shared helpers."""

from __future__ import annotations

import random
import re
import time
import unicodedata
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


def fold_accents(text: str) -> str:
    """Lowercase + strip accents so 'Cuántos' / 'Cuantós' match 'cuantos'."""
    n = unicodedata.normalize("NFD", normalize_text(text))
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def contains_any(text: str, keywords: list[str]) -> bool:
    t = normalize_text(text)
    return any(normalize_text(k) in t for k in keywords)


def earliest_start_date_mmddyyyy(notice_days: int = 30) -> str:
    """Earliest start as MM/DD/YYYY (notice + small buffer). Typed into date boxes."""
    return (date.today() + timedelta(days=notice_days + 5)).strftime("%m/%d/%Y")


def earliest_start_date_iso(notice_days: int = 30) -> str:
    """Same date as YYYY-MM-DD for native input[type=date]."""
    return (date.today() + timedelta(days=notice_days + 5)).strftime("%Y-%m-%d")


def notice_period_value(question: str, days: int, weeks: int, months: int) -> str:
    """Compact notice/start answer: MM/DD/YYYY for date questions, else day/week/month int."""
    q = fold_accents(question)
    # Pure duration questions (not a calendar date box)
    if any(k in q for k in ("notice period", "kundigungsfrist", "kuendigungsfrist", "notice in")):
        if any(k in q for k in ("month", "monat", "months", "monate")):
            return str(months)
        if any(k in q for k in ("week", "woche", "weeks", "wochen")):
            return str(weeks)
        return str(days)
    if any(k in q for k in ("month", "monat", "months", "monate")) and "datum" not in q and "date" not in q:
        return str(months)
    if any(k in q for k in ("week", "woche", "weeks", "wochen")) and "datum" not in q and "date" not in q:
        return str(weeks)
    # Start date / availability / Startdatum → typeable calendar date
    if any(
        k in q
        for k in (
            "date",
            "datum",
            "startdatum",
            "starttermin",
            "available from",
            "start date",
            "earliest start",
            "ab wann",
            "fruehestmoglich",
            "eintritt",
            "verfugbar",
            "wann ware",
            "wann ware ihr",
        )
    ) or ("start" in q and any(k in q for k in ("wann", "when", "fruh", "frueh"))):
        return earliest_start_date_mmddyyyy(days)
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
