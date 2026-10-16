"""Dropdown / radio intent matching via Ollama + heuristics."""

from __future__ import annotations

import logging
import re

from src.config import AppConfig
from src.ollama_client import OllamaClient
from src.utils import normalize_text

logger = logging.getLogger("easyapply")


class IntentMatcher:
    def __init__(self, config: AppConfig, ollama: OllamaClient) -> None:
        self.config = config
        self.ollama = ollama

    def pick(self, question: str, options: list[str], preferred: str | None = None) -> str:
        options = [o.strip() for o in options if o and o.strip()]
        if not options:
            return ""
        if preferred:
            hit = self._fuzzy_option(preferred, options)
            if hit:
                return hit

        rule = self._rule_based(question, options)
        if rule:
            return rule

        if self.config.qa_memory.use_ollama_intent:
            try:
                return self.ollama.match_option(question, options, hint=preferred or "")
            except Exception as exc:
                logger.warning("Intent model failed: %s", exc)
        return options[0]

    def _fuzzy_option(self, preferred: str, options: list[str]) -> str | None:
        pref = normalize_text(preferred)
        for o in options:
            if pref == normalize_text(o):
                return o
        for o in options:
            if pref in normalize_text(o) or normalize_text(o) in pref:
                return o
        return None

    def _rule_based(self, question: str, options: list[str]) -> str | None:
        q = normalize_text(question)
        auth = self.config.eu_work_authorization
        langs = self.config.languages_cefr

        # Sponsorship → No
        if any(
            k in q
            for k in (
                "sponsor",
                "sponsorship",
                "visa sponsorship",
                "arbeitgeber",
                "visum vom",
                "require sponsorship",
            )
        ) and "own" not in q and "provide your own" not in q:
            return self._yes_no(options, want_yes=auth.requires_visa_sponsorship.lower() == "yes")

        # Own visa / work permit → Yes
        if any(
            k in q
            for k in (
                "own visa",
                "own work permit",
                "provide your own",
                "eigenes visum",
                "work authorization",
            )
        ):
            return self._yes_no(options, want_yes=auth.can_provide_own_germany_visa.lower() == "yes")

        if "relocate" in q or "umzug" in q or "umziehen" in q:
            return self._yes_no(options, want_yes=auth.willing_to_relocate.lower() == "yes")

        # Language proficiency
        lang_key = None
        if "english" in q or "englisch" in q:
            lang_key = "English"
        elif "german" in q or "deutsch" in q:
            lang_key = "German"
        elif "telugu" in q:
            lang_key = "Telugu"
        elif any(k in q for k in ("french", "dutch", "spanish", "französisch", "niederländisch", "spanisch")):
            lang_key = "default_other_language"

        if lang_key and lang_key in langs:
            level = langs[lang_key]
            for candidate in (
                level.cefr,
                level.linkedin_label,
                level.german_label,
                str(level.numeric_0_to_10),
                level.status,
            ):
                hit = self._fuzzy_option(candidate, options) if candidate else None
                if hit:
                    return hit
            # common synonyms
            synonyms = {
                "English": ["native", "bilingual", "fluent", "c2", "verhandlungssicher"],
                "German": ["elementary", "beginner", "a1", "grundkenntnisse"],
                "Telugu": ["native", "bilingual", "muttersprache", "c2"],
                "default_other_language": ["none", "no proficiency", "keine", "0"],
            }
            for syn in synonyms.get(lang_key, []):
                for o in options:
                    if syn in normalize_text(o):
                        return o

        # GDPR / privacy → Yes / agree
        if any(k in q for k in ("privacy", "gdpr", "datenschutz", "consent", "einwilligung", "talent pool")):
            for o in options:
                on = normalize_text(o)
                if on in ("yes", "ja", "agree", "i agree", "accept"):
                    return o

        return None

    def _yes_no(self, options: list[str], want_yes: bool) -> str | None:
        yes_pat = re.compile(r"^(yes|ja|y)$", re.I)
        no_pat = re.compile(r"^(no|nein|n)$", re.I)
        for o in options:
            if want_yes and yes_pat.match(o.strip()):
                return o
            if not want_yes and no_pat.match(o.strip()):
                return o
        for o in options:
            on = normalize_text(o)
            if want_yes and (on.startswith("yes") or on == "ja"):
                return o
            if not want_yes and (on.startswith("no") or on == "nein"):
                return o
        return None
