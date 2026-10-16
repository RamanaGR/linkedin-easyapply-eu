"""Cover letter from user-owned config template."""

from __future__ import annotations

import logging

from src.config import AppConfig
from src.ollama_client import OllamaClient, sanitize_human_text

logger = logging.getLogger("easyapply")


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
    ollama: OllamaClient | None,
    *,
    position: str = "",
    company: str = "",
    location: str = "",
    adapt: bool = True,
) -> str:
    if not config.cover_letter.enabled:
        return ""
    base = render_template(
        config, position=position, company=company, location=location
    )
    if not adapt or ollama is None:
        return base

    try:
        prompt = (
            "Lightly adapt this cover letter for the role. Keep the same structure and facts. "
            "Do not add new skills or employers. Keep it short. Preserve paragraph breaks.\n\n"
            f"Role: {position}\nCompany: {company}\nLocation: {location}\n\n"
            f"Template:\n{base}"
        )
        adapted = ollama.answer_question(
            "Adapt the cover letter.",
            config.context_blob(),
            extra=prompt,
        )
        if len(adapted) < 40:
            return base
        # sanitize_human_text collapses newlines; restore simple paragraphs
        cleaned = sanitize_human_text(adapted)
        return cleaned
    except Exception as exc:
        logger.warning("Cover letter adapt failed, using template: %s", exc)
        return base
