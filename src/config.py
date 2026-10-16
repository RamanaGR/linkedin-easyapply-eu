"""Load and validate config.yaml + .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from src.utils import ROOT, project_path


class Profile(BaseModel):
    full_name: str
    first_name: str
    last_name: str
    date_of_birth: str = ""
    email: str
    phone_number: str
    phone_country_code: str = "India (+91)"
    current_city: str = "Bangalore"
    current_country: str = "India"
    location_full: str = "Bangalore, Karnataka, India"
    linkedin_url: str = ""
    github_url: str = ""


class Experience(BaseModel):
    total_experience_years: int = 8
    skill_years: dict[str, int] = Field(default_factory=dict)


class EuWorkAuthorization(BaseModel):
    currently_in_eu: bool = False
    requires_visa_sponsorship: str = "No"
    can_provide_own_germany_visa: str = "Yes"
    germany_visa_timeline: str = "5-8 weeks"
    germany_visa_statement: str = ""
    willing_to_relocate: str = "Yes"
    notice_period_days: int = 30
    notice_period_weeks: int = 4
    notice_period_months: int = 1


class CompensationEu(BaseModel):
    currency: str = "EUR"
    expected_salary_eur_min: int = 75000
    expected_salary_eur_max: int = 95000
    expected_salary_eur_target: int = 85000
    expected_salary_range_text: str = "75000 - 95000 EUR"
    current_salary_eur: int = 45000
    salary: int = 85000
    rate: int = 55
    current_ctc_lpa: int = 30
    expected_ctc_lpa: int = 40

    @field_validator("expected_salary_eur_max")
    @classmethod
    def max_gte_min(cls, v: int, info: Any) -> int:
        min_v = info.data.get("expected_salary_eur_min")
        if min_v is not None and v < min_v:
            raise ValueError("expected_salary_eur_max must be >= expected_salary_eur_min")
        return v


class LanguageLevel(BaseModel):
    cefr: str = "None"
    linkedin_label: str = "None"
    german_label: str = "Keine"
    status: str = ""
    numeric_0_to_10: int = 0


class Education(BaseModel):
    highest_degree: str = ""
    masters_degree: str = ""
    masters_institution: str = ""
    masters_dates: str = ""
    bachelors_degree: str = ""
    bachelors_institution: str = ""
    bachelors_dates: str = ""
    certifications: list[str] = Field(default_factory=list)


class GdprAndConsents(BaseModel):
    accept_gdpr_privacy_policy: bool = True
    accept_talent_pool_storage: bool = True
    uncheck_follow_company: bool = True


class Uploads(BaseModel):
    Resume: str = ""
    Cover_Letter: str = Field(default="", alias="Cover Letter")

    model_config = {"populate_by_name": True}


class CoverLetter(BaseModel):
    enabled: bool = True
    template: str = ""


class SearchFilters(BaseModel):
    easy_apply_only: bool = True
    date_posted: str = "past_week"
    workplace_type: list[str] = Field(default_factory=lambda: ["hybrid", "remote", "onsite"])
    experience_level: list[str] = Field(default_factory=lambda: ["Mid-Senior", "Associate"])


class SearchBlacklists(BaseModel):
    title_keywords: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    skip_strict_german_c1_required: bool = True


class Search(BaseModel):
    # position_outer | location_outer | positions_and_tech
    strategy: str = "positions_and_tech"
    # Which keyword sources to use when strategy includes tech
    use_positions: bool = True
    use_tech_keywords: bool = True
    filters: SearchFilters = Field(default_factory=SearchFilters)
    blacklists: SearchBlacklists = Field(default_factory=SearchBlacklists)


class Session(BaseModel):
    max_search_time: int = 7200
    max_applications_per_run: int = 50
    max_applications_per_day: int = 50
    unattended_mode: bool = True
    require_submission_confirmation: bool = False
    dry_run: bool = True
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 6.0
    captcha_wait_minutes: int = 15
    login_2fa_wait_minutes: int = 10


class QaMemory(BaseModel):
    fuzzy_high_threshold: int = 88
    fuzzy_low_threshold: int = 75
    use_ollama_intent: bool = True
    use_embeddings: bool = True


class AppConfig(BaseModel):
    profile: Profile
    experience: Experience = Field(default_factory=Experience)
    eu_work_authorization: EuWorkAuthorization = Field(default_factory=EuWorkAuthorization)
    compensation_eu: CompensationEu = Field(default_factory=CompensationEu)
    languages_cefr: dict[str, LanguageLevel] = Field(default_factory=dict)
    education: Education = Field(default_factory=Education)
    gdpr_and_consents: GdprAndConsents = Field(default_factory=GdprAndConsents)
    uploads: Uploads = Field(default_factory=Uploads)
    cover_letter: CoverLetter = Field(default_factory=CoverLetter)
    positions: list[str] = Field(default_factory=list)
    # LinkedIn keyword searches by main tech stack (in addition to role titles)
    tech_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    search: Search = Field(default_factory=Search)

    def search_keywords(self) -> list[str]:
        """Ordered unique keywords from roles and/or tech stack."""
        keywords: list[str] = []
        if self.search.use_positions:
            keywords.extend(self.positions)
        if self.search.use_tech_keywords:
            keywords.extend(self.tech_keywords)
        # de-dupe, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for k in keywords:
            key = (k or "").strip()
            if not key:
                continue
            low = key.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(key)
        return out
    session: Session = Field(default_factory=Session)
    qa_memory: QaMemory = Field(default_factory=QaMemory)

    def resume_path(self) -> Path:
        return project_path(self.uploads.Resume) if self.uploads.Resume else Path()

    def cover_letter_pdf_path(self) -> Path | None:
        raw = self.uploads.Cover_Letter.strip()
        if not raw:
            return None
        return project_path(raw)

    def context_blob(self) -> str:
        """Compact grounding text for Ollama."""
        p = self.profile
        e = self.eu_work_authorization
        c = self.compensation_eu
        skills = ", ".join(f"{k} ({v}y)" for k, v in self.experience.skill_years.items())
        return (
            f"Name: {p.full_name}\n"
            f"Location: {p.location_full}\n"
            f"Email: {p.email}\n"
            f"Phone: {p.phone_number}\n"
            f"Experience years: {self.experience.total_experience_years}\n"
            f"Skills: {skills}\n"
            f"Education: {self.education.highest_degree}, {self.education.masters_degree}\n"
            f"Visa: sponsorship={e.requires_visa_sponsorship}, "
            f"own_germany_visa={e.can_provide_own_germany_visa}\n"
            f"Visa statement: {e.germany_visa_statement}\n"
            f"Notice: {e.notice_period_days} days / {e.notice_period_weeks} weeks / "
            f"{e.notice_period_months} months\n"
            f"Salary EUR target: {c.expected_salary_eur_target}, "
            f"range: {c.expected_salary_range_text}, LPA expected: {c.expected_ctc_lpa}\n"
            f"Languages: { {k: v.cefr for k, v in self.languages_cefr.items()} }\n"
        )


class EnvSettings(BaseModel):
    linkedin_email: str = ""
    linkedin_password: str = ""
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e4b-it-q4_K_M"
    ollama_model_fallback: str = "gemma4:e2b-it-q4_K_M"
    ollama_intent_model: str = "llama3.2:latest"
    ollama_embed_model: str = "nomic-embed-text:latest"
    chrome_profile_dir: str = "chrome_profile"


def load_env() -> EnvSettings:
    load_dotenv(ROOT / ".env")
    return EnvSettings(
        linkedin_email=os.getenv("LINKEDIN_EMAIL", ""),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", ""),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "gemma4:e4b-it-q4_K_M"),
        ollama_model_fallback=os.getenv("OLLAMA_MODEL_FALLBACK", "gemma4:e2b-it-q4_K_M"),
        ollama_intent_model=os.getenv("OLLAMA_INTENT_MODEL", "llama3.2:latest"),
        ollama_embed_model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest"),
        chrome_profile_dir=os.getenv("CHROME_PROFILE_DIR", "chrome_profile"),
    )


def load_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or (ROOT / "config.yaml")
    if not cfg_path.exists():
        example = ROOT / "config.example.yaml"
        raise FileNotFoundError(
            f"Missing {cfg_path}. Copy {example.name} to config.yaml and fill your profile."
        )
    with cfg_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    # Normalize Cover Letter key for pydantic
    uploads = raw.get("uploads") or {}
    if "Cover Letter" in uploads and "Cover_Letter" not in uploads:
        uploads["Cover Letter"] = uploads.get("Cover Letter", "")
        raw["uploads"] = uploads
    return AppConfig.model_validate(raw)


def load_resume_json(path: Path | None = None) -> dict[str, Any]:
    import json

    resume_path = path or project_path("assets", "Ramana_Gangarao_Resume.json")
    if not resume_path.exists():
        return {}
    with resume_path.open(encoding="utf-8") as f:
        return json.load(f)
