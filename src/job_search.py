"""LinkedIn job search URL building, filters, and card parsing."""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from src import selectors as sel
from src.config import AppConfig
from src.utils import contains_any, normalize_text

logger = logging.getLogger("easyapply")

DATE_POSTED_MAP = {
    "24h": "r86400",
    "past_week": "r604800",
    "past_month": "r2592000",
}

EXPERIENCE_MAP = {
    "Internship": "1",
    "Entry level": "2",
    "Associate": "3",
    "Mid-Senior": "4",
    "Director": "5",
    "Executive": "6",
}

WORKPLACE_MAP = {
    "onsite": "1",
    "remote": "2",
    "hybrid": "3",
}


@dataclass
class JobCard:
    job_id: str
    title: str
    company: str
    location: str
    link: str
    description_snippet: str = ""


class JobSearch:
    def __init__(self, driver: WebDriver, config: AppConfig) -> None:
        self.driver = driver
        self.config = config

    def iter_queries(self) -> list[tuple[str, str]]:
        """Return (keywords, location) pairs based on strategy.

        Keywords come from `positions` and/or `tech_keywords` (see config.search).
        """
        keywords = self.config.search_keywords() or [""]
        locations = self.config.locations or [""]
        strategy = self.config.search.strategy
        pairs: list[tuple[str, str]] = []
        if strategy == "location_outer":
            for loc in locations:
                for kw in keywords:
                    pairs.append((kw, loc))
        else:
            # position_outer / positions_and_tech / round_robin: keyword outer
            for kw in keywords:
                for loc in locations:
                    pairs.append((kw, loc))
        return pairs

    def build_search_url(self, keywords: str, location: str) -> str:
        f = self.config.search.filters
        params: dict[str, str] = {
            "keywords": keywords,
            "location": location,
        }
        if f.easy_apply_only:
            params["f_AL"] = "true"
        if f.date_posted in DATE_POSTED_MAP:
            params["f_TPR"] = DATE_POSTED_MAP[f.date_posted]
        exp_ids = [EXPERIENCE_MAP[e] for e in f.experience_level if e in EXPERIENCE_MAP]
        if exp_ids:
            params["f_E"] = ",".join(exp_ids)
        wp_ids = [WORKPLACE_MAP[w.lower()] for w in f.workplace_type if w.lower() in WORKPLACE_MAP]
        if wp_ids:
            params["f_WT"] = ",".join(wp_ids)
        return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)

    def open_search(self, keywords: str, location: str) -> None:
        url = self.build_search_url(keywords, location)
        logger.info("Opening search: %s | %s", keywords, location)
        self.driver.get(url)

    def is_blacklisted(self, card: JobCard) -> str | None:
        bl = self.config.search.blacklists
        if contains_any(card.title, bl.title_keywords):
            return "title_keyword"
        if contains_any(card.company, bl.companies):
            return "company"
        return None

    def requires_strict_german_c1(self, text: str) -> bool:
        if not self.config.search.blacklists.skip_strict_german_c1_required:
            return False
        t = normalize_text(text)
        patterns = (
            "german c1",
            "c1 german",
            "deutsch c1",
            "c1 deutsch",
            "native german",
            "deutsch als muttersprache",
            "fluent german required",
            "verhandlungssicheres deutsch",
        )
        return any(p in t for p in patterns)

    def parse_cards(self) -> list[JobCard]:
        cards: list[JobCard] = []
        elements = self.driver.find_elements(By.CSS_SELECTOR, sel.JOB_CARD)
        for el in elements:
            try:
                job_id = (
                    el.get_attribute("data-occludable-job-id")
                    or el.get_attribute("data-job-id")
                    or ""
                )
                link_el = None
                for css in (
                    "a.job-card-list__title--link",
                    "a.job-card-container__link",
                    "a[href*='/jobs/view/']",
                ):
                    found = el.find_elements(By.CSS_SELECTOR, css)
                    if found:
                        link_el = found[0]
                        break
                title = (link_el.text if link_el else el.text).strip().split("\n")[0]
                href = link_el.get_attribute("href") if link_el else ""
                if not job_id and href:
                    m = re.search(r"/jobs/view/(\d+)", href)
                    if m:
                        job_id = m.group(1)
                company = ""
                for css in (
                    ".artdeco-entity-lockup__subtitle",
                    ".job-card-container__primary-description",
                    ".job-card-container__company-name",
                ):
                    found = el.find_elements(By.CSS_SELECTOR, css)
                    if found and found[0].text.strip():
                        company = found[0].text.strip()
                        break
                location = ""
                for css in (
                    ".job-card-container__metadata-item",
                    ".artdeco-entity-lockup__caption",
                ):
                    found = el.find_elements(By.CSS_SELECTOR, css)
                    if found and found[0].text.strip():
                        location = found[0].text.strip()
                        break
                if not title and not job_id:
                    continue
                cards.append(
                    JobCard(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=location,
                        link=href or f"https://www.linkedin.com/jobs/view/{job_id}",
                    )
                )
            except Exception as exc:
                logger.debug("Card parse skip: %s", exc)
        return cards

    def already_applied_visible(self) -> bool:
        page = normalize_text(self.driver.page_source[:50000])
        return "applied" in page and ("easy apply" not in page or "application submitted" in page)

    def job_description_text(self) -> str:
        for css in (
            ".jobs-description-content__text",
            ".jobs-box__html-content",
            "#job-details",
        ):
            els = self.driver.find_elements(By.CSS_SELECTOR, css)
            if els:
                return els[0].text
        return ""
