"""Cover letter from user-owned config template (instant, no LLM)."""

from __future__ import annotations

from typing import Any

from src.config import AppConfig


def render_template(
    config: AppConfig,
    *,
    position: str = "",
    company: str = "",
    location: str = "",
) -> str:
    tmpl = config.cover_letter.template or ""
    return tmpl.format(
        full_name=config.profile.full_name,
        position=position or "the open role",
        company=company or "your company",
        location=location or config.profile.location_full,
        years_experience=config.experience.total_experience_years,
    ).strip()


def build_cover_letter(
    config: AppConfig,
    ollama: Any = None,
    *,
    position: str = "",
    company: str = "",
    location: str = "",
    adapt: bool = False,
) -> str:
    """Fill cover letter from config template instantly ({company}, {position}, …).

    Different company/role names per job are expected (template substitution).
    Ollama is never called — keep the apply loop fast.
    """
    del ollama, adapt
    if not config.cover_letter.enabled:
        return ""
    return render_template(
        config, position=position, company=company, location=location
    )
