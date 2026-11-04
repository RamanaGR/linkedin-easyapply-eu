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
from src.utils import contains_any, normalize_text, short_delay

logger = logging.getLogger("easyapply")

# Dropped when extracting the distinctive part of a role query
_TITLE_FILLER = {
    "senior",
    "sr",
    "lead",
    "staff",
    "principal",
    "junior",
    "jr",
    "mid",
    "associate",
    "head",
    "chief",
    "remote",
    "hybrid",
    "onsite",
    "m",
    "w",
    "d",
    "f",
    "n",
}
_GENERIC_ROLE = {
    "engineer",
    "developer",
    "entwickler",
    "ingenieur",
    "specialist",
    "consultant",
    "architect",
}
_TOKEN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "artificial intelligence", "ki"),
    "ml": ("ml", "machine learning"),
    "llm": ("llm", "large language"),
    "generative": ("generative", "genai", "gen ai"),
    "agentic": ("agentic", "multi agent", "multiagent"),
    "engineer": ("engineer", "ingenieur", "entwickler"),
    "software": ("software",),
    "machine": ("machine learning", "ml"),
    "learning": ("machine learning", "ml"),
}
_DEFAULT_TITLE_EXCLUDES = (
    "data engineer",
    "dateningenieur",
    "data analyst",
    "analytics engineer",
    "business intelligence",
    "bi engineer",
    "etl developer",
    "frontend",
    "front-end",
    "front end",
    "ios developer",
    "android",
    "embedded",
    "php",
    "salesforce",
    "recruiter",
    "account executive",
    "product manager",
    "project manager",
    "scrum master",
)

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

# LinkedIn needs geoId — plain location= text often keeps the previous country (e.g. Germany/Munich).
LOCATION_GEO_IDS: dict[str, str] = {
    "germany": "101282230",
    "netherlands": "102890719",
    "luxembourg": "104080236",
    "austria": "103883259",
    "belgium": "100565514",
    "ireland": "104738515",
    "sweden": "105117694",
    "denmark": "104514075",
    "finland": "100456013",
    "portugal": "100364837",
    "spain": "105646813",
    "france": "105015875",
    "switzerland": "106693272",
    "poland": "105072130",
    "czechia": "104508036",
    "czech republic": "104508036",
    "italy": "103350119",
    "norway": "103819153",
    "united kingdom": "101165590",
    "uk": "101165590",
    "european union": "91000000",
    "europe": "91000000",
}

# Card location text markers used to detect wrong-country results after a broken geo filter.
LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "germany": (
        "germany",
        "deutschland",
        "berlin",
        "munich",
        "muenchen",
        "münchen",
        "hamburg",
        "frankfurt",
        "cologne",
        "köln",
        "koln",
        "stuttgart",
        "düsseldorf",
        "dusseldorf",
        "nuremberg",
        "nürnberg",
    ),
    "netherlands": (
        "netherlands",
        "holland",
        "amsterdam",
        "rotterdam",
        "utrecht",
        "the hague",
        "den haag",
        "eindhoven",
    ),
    "luxembourg": ("luxembourg", "luxemburg", "letzebuerg"),
    "austria": ("austria", "österreich", "osterreich", "vienna", "wien", "graz", "linz"),
    "belgium": ("belgium", "belgien", "belgique", "brussels", "bruxelles", "antwerp", "ghent"),
    "ireland": ("ireland", "dublin", "cork", "galway", "limerick"),
    "sweden": ("sweden", "sverige", "stockholm", "gothenburg", "göteborg", "malmö", "malmo"),
    "denmark": ("denmark", "danmark", "copenhagen", "københavn", "kopenhagen", "aarhus"),
    "finland": ("finland", "suomi", "helsinki", "espoo", "tampere"),
    "portugal": ("portugal", "lisbon", "lisboa", "porto"),
    "spain": ("spain", "españa", "espana", "madrid", "barcelona", "valencia", "seville"),
    "france": ("france", "paris", "lyon", "marseille", "toulouse", "bordeaux", "nantes", "lille"),
    "switzerland": (
        "switzerland",
        "schweiz",
        "suisse",
        "zurich",
        "zürich",
        "geneva",
        "genève",
        "bern",
        "basel",
        "lausanne",
    ),
    "poland": ("poland", "polska", "warsaw", "warszawa", "krakow", "kraków", "wroclaw"),
    "czechia": ("czechia", "czech republic", "prague", "praha", "brno"),
    "czech republic": ("czechia", "czech republic", "prague", "praha", "brno"),
    "italy": ("italy", "italia", "rome", "roma", "milan", "milano", "turin", "naples"),
    "norway": ("norway", "norge", "oslo", "bergen"),
    "united kingdom": ("united kingdom", "uk", "london", "manchester", "edinburgh", "bristol"),
    "uk": ("united kingdom", "uk", "london", "manchester", "edinburgh", "bristol"),
}

_EU_WIDE_LOCATIONS = {
    "european union",
    "europe",
    "eu",
    "eea",
    "emea",
    "remote europe",
    "remote - europe",
    "remote eu",
}


def _norm_title(text: str) -> str:
    s = normalize_text(text)
    s = re.sub(r"[/|,;:()\[\]+]+", " ", s)
    s = re.sub(r"[-_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _has_phrase(title_norm: str, phrase: str) -> bool:
    phrase = _norm_title(phrase)
    if not phrase:
        return False
    if " " in phrase:
        return phrase in title_norm
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", title_norm) is not None


def _content_tokens(query: str) -> list[str]:
    q = _norm_title(query)
    for src, dst in (
        ("machine learning", "ml"),
        ("generative ai", "generative"),
        ("artificial intelligence", "ai"),
        ("large language", "llm"),
    ):
        q = q.replace(src, dst)
    return [w for w in q.split() if w and w not in _TITLE_FILLER]


def _title_matches_query(title: str, query: str) -> bool:
    tokens = _content_tokens(query)
    if not tokens:
        return False
    title_norm = _norm_title(title)
    distinctive = [t for t in tokens if t not in _GENERIC_ROLE]
    generic = [t for t in tokens if t in _GENERIC_ROLE]
    required = distinctive or tokens
    for token in required:
        synonyms = _TOKEN_SYNONYMS.get(token, (token,))
        if not any(_has_phrase(title_norm, syn) for syn in synonyms):
            return False
    if generic and distinctive:
        if not any(
            any(_has_phrase(title_norm, syn) for syn in _TOKEN_SYNONYMS.get(tok, (tok,)))
            for tok in generic
        ):
            return False
    return True


def title_matches_target_roles(title: str, query: str, positions: list[str]) -> bool:
    """True when the job title matches the current search or any configured role."""
    candidates: list[str] = []
    for item in [query, *(positions or [])]:
        key = (item or "").strip()
        if key and key.lower() not in {c.lower() for c in candidates}:
            candidates.append(key)
    return any(_title_matches_query(title, item) for item in candidates)


@dataclass
class JobCard:
    job_id: str
    title: str
    company: str
    location: str
    link: str
    description_snippet: str = ""
    is_easy_apply: bool = False


class JobSearch:
    def __init__(self, driver: WebDriver, config: AppConfig) -> None:
        self.driver = driver
        self.config = config

    def iter_queries(self) -> list[tuple[str, str]]:
        """Return (keywords, location) pairs based on strategy.

        Keywords come from `positions` and/or `tech_keywords` (see config.search).

        ``round_robin`` / ``location_outer``: every role in location[0] (Germany)
        first, then the next country. Pair this with per-query/per-role caps so
        one title cannot consume the daily limit.
        """
        keywords = self.config.search_keywords() or [""]
        locations = self.config.locations or [""]
        strategy = (self.config.search.strategy or "round_robin").strip().lower()
        pairs: list[tuple[str, str]] = []
        if strategy in ("round_robin", "location_outer"):
            for loc in locations:
                for kw in keywords:
                    pairs.append((kw, loc))
        else:
            # position_outer / positions_and_tech: keyword outer
            for kw in keywords:
                for loc in locations:
                    pairs.append((kw, loc))
        return pairs

    @staticmethod
    def resolve_geo_id(location: str) -> str | None:
        """Map a config location string to LinkedIn geoId (country-level)."""
        loc = normalize_text(location)
        if not loc:
            return None
        if loc in LOCATION_GEO_IDS:
            return LOCATION_GEO_IDS[loc]
        # "Berlin, Germany" / "Amsterdam, Netherlands"
        if "," in loc:
            country = loc.rsplit(",", 1)[-1].strip()
            if country in LOCATION_GEO_IDS:
                return LOCATION_GEO_IDS[country]
        for key, geo in LOCATION_GEO_IDS.items():
            if key in loc or loc in key:
                return geo
        return None

    def build_search_url(
        self, keywords: str, location: str, easy_apply_only: bool | None = None
    ) -> str:
        f = self.config.search.filters
        params: dict[str, str] = {
            "keywords": keywords,
            "location": location,
        }
        geo_id = self.resolve_geo_id(location)
        if geo_id:
            params["geoId"] = geo_id
        use_easy_apply = f.easy_apply_only if easy_apply_only is None else easy_apply_only
        if use_easy_apply:
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

    def open_search(
        self, keywords: str, location: str, easy_apply_only: bool | None = None
    ) -> None:
        url = self.build_search_url(keywords, location, easy_apply_only=easy_apply_only)
        geo_id = self.resolve_geo_id(location)
        logger.info(
            "Opening search: %s | %s (geoId=%s)",
            keywords,
            location,
            geo_id or "none",
        )
        self.driver.get(url)
        # LinkedIn sometimes keeps the previous geo chip; force geoId once if dropped.
        try:
            short_delay(1500)
            current = self.driver.current_url or ""
            if geo_id and f"geoId={geo_id}" not in current and "geoId=" in current:
                logger.warning(
                    "LinkedIn replaced geoId — reloading with geoId=%s for %s",
                    geo_id,
                    location,
                )
                self.driver.get(url)
                short_delay(1500)
            elif geo_id and "geoId=" not in current:
                logger.warning(
                    "Search URL missing geoId after load — reloading for %s", location
                )
                self.driver.get(url)
        except Exception as exc:
            logger.debug("geoId verify skipped: %s", exc)

    def is_blacklisted(self, card: JobCard) -> str | None:
        bl = self.config.search.blacklists
        if contains_any(card.title, bl.title_keywords):
            return "title_keyword"
        if contains_any(card.company, bl.companies):
            return "company"
        return None

    def title_mismatch_reason(self, card: JobCard, query: str) -> str | None:
        """Why this card's title is off-role, or None if it may be applied to."""
        if not getattr(self.config.search, "require_title_match", True):
            return None
        title = card.title or ""
        if not title.strip():
            return "empty title"
        excluded = self._title_exclude_hit(title)
        if excluded:
            return f"excluded:{excluded}"
        positions = list(self.config.positions or [])
        if title_matches_target_roles(title, query, positions):
            return None
        return "title does not match role keywords"

    def location_mismatch_reason(self, card: JobCard, search_location: str) -> str | None:
        """Skip cards clearly posted in a different country than the search target.

        Catches the LinkedIn bug where France/NL searches still return Munich jobs
        because geoId was missing. Broad EU / Europe searches are never filtered.
        """
        if not getattr(self.config.search, "require_location_match", True):
            return None
        target = normalize_text(search_location)
        if not target or target in _EU_WIDE_LOCATIONS:
            return None
        # "Berlin, Germany" → germany
        if "," in target:
            target = target.rsplit(",", 1)[-1].strip()
        card_loc = normalize_text(card.location or "")
        if not card_loc:
            return None
        if any(marker in card_loc for marker in _EU_WIDE_LOCATIONS):
            return None

        target_aliases = LOCATION_ALIASES.get(target, (target,))
        if any(alias in card_loc for alias in target_aliases):
            return None

        for country, aliases in LOCATION_ALIASES.items():
            if country == target:
                continue
            # Only treat as wrong-country when a foreign marker is present AND
            # none of the target markers are (already checked above).
            if any(alias in card_loc for alias in aliases):
                return f"card location {card.location!r} not in {search_location}"
        return None

    def _title_exclude_hit(self, title: str) -> str | None:
        extra = list(getattr(self.config.search, "title_excludes", None) or [])
        needles = list(_DEFAULT_TITLE_EXCLUDES) + extra
        blob = _norm_title(title)
        for raw in needles:
            needle = _norm_title(raw)
            if needle and needle in blob:
                return needle
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
        seen_ids: set[str] = set()

        # 1) Try SDUI (2026 /jobs/search-results/) + scroll left pane to load cards
        try:
            sdui_raw = self.driver.execute_script(
                """
                const sel = '[role="button"][componentkey^="job-card-component-ref-"], '
                          + '[componentkey^="job-card-component-ref-"]';
                let els = Array.from(document.querySelectorAll(sel));
                if (els.length > 0) {
                    // Scroll the scrollable parent list a couple of times to hydrate lazy cards
                    let scroller = els[0].parentElement;
                    while (scroller && scroller !== document.body) {
                        const st = window.getComputedStyle(scroller);
                        if (
                            (st.overflowY === 'auto' || st.overflowY === 'scroll')
                            && scroller.scrollHeight > scroller.clientHeight + 40
                        ) {
                            scroller.scrollTop = scroller.scrollHeight;
                            break;
                        }
                        scroller = scroller.parentElement;
                    }
                }
                els = Array.from(document.querySelectorAll(sel));
                const byKey = new Map();
                for (const el of els) {
                    const k = el.getAttribute('componentkey') || '';
                    if (!k) continue;
                    if (!byKey.has(k) || el.getAttribute('role') === 'button') {
                        byKey.set(k, el);
                    }
                }
                return Array.from(byKey.values()).map(el => ({
                    key: el.getAttribute('componentkey') || '',
                    lines: (el.innerText || '')
                        .split(String.fromCharCode(10))
                        .map(s => s.trim())
                        .filter(Boolean)
                }));
                """
            )
        except Exception as exc:
            logger.debug("SDUI card scan failed: %s", exc)
            sdui_raw = []

        if isinstance(sdui_raw, list) and sdui_raw:
            for item in sdui_raw:
                try:
                    key = str(item.get("key") or "")
                    m = re.search(r"(\d{7,})", key)
                    if not m:
                        continue
                    job_id = m.group(1)
                    if job_id in seen_ids:
                        continue
                    raw_lines = [
                        str(ln).strip()
                        for ln in (item.get("lines") or [])
                        if str(ln).strip() and str(ln).strip() != "·"
                    ]
                    if not raw_lines:
                        continue

                    def _clean_title(s: str) -> str:
                        s = re.sub(r"^Selected,\s*", "", s, flags=re.IGNORECASE)
                        s = re.sub(r"\s*\(Verified job\)\s*$", "", s, flags=re.IGNORECASE)
                        return s.strip()

                    t0 = _clean_title(raw_lines[0])
                    t1 = _clean_title(raw_lines[1]) if len(raw_lines) > 1 else ""
                    if t1 and (t0.lower() == t1.lower() or t1.lower() in t0.lower()):
                        title = t1
                        rest = raw_lines[2:]
                    else:
                        title = t0
                        rest = raw_lines[1:]

                    company = rest[0] if len(rest) >= 1 else ""
                    location = rest[1] if len(rest) >= 2 else ""
                    blob_low = " ".join(raw_lines).lower()
                    is_ea = any(
                        marker in blob_low
                        for marker in ("easy apply", "einfach bewerben", "candidatura semplificata")
                    )
                    seen_ids.add(job_id)
                    cards.append(
                        JobCard(
                            job_id=job_id,
                            title=title,
                            company=company,
                            location=location,
                            link=f"https://www.linkedin.com/jobs/view/{job_id}/",
                            is_easy_apply=is_ea,
                        )
                    )
                except Exception as exc:
                    logger.debug("SDUI card parse skip: %s", exc)
            if cards:
                return cards

        # 2) Classic Voyager job cards fallback
        elements = self.driver.find_elements(
            By.CSS_SELECTOR, "li.scaffold-layout__list-item, div.job-card-container"
        )
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
                if job_id:
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)
                el_text_low = (el.text or "").lower()
                is_ea = any(
                    marker in el_text_low
                    for marker in ("easy apply", "einfach bewerben", "candidatura semplificata")
                )
                cards.append(
                    JobCard(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=location,
                        link=href or f"https://www.linkedin.com/jobs/view/{job_id}/",
                        is_easy_apply=is_ea,
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
            if els and els[0].text.strip():
                return els[0].text.strip()

        # SDUI /jobs/view/ and /jobs/search-results/ description extractor
        try:
            desc = self.driver.execute_script(
                """
                // Expand “…more” / “Show more” inside job description if present
                for (const btn of document.querySelectorAll('button')) {
                    const t = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    if (t === '…more' || t === '...more' || t === 'more' || t === 'show more') {
                        try { btn.click(); } catch (e) {}
                    }
                }
                const bodyText = (document.body && document.body.innerText) || '';
                const markers = ['About the job', 'Info zur Stelle', 'Über die Stelle'];
                for (const m of markers) {
                    const idx = bodyText.indexOf(m);
                    if (idx !== -1) {
                        let sub = bodyText.slice(idx + m.length).trim();
                        const endMarkers = [
                            'Set alert for similar jobs',
                            'About the company',
                            'Similar jobs',
                            'More jobs',
                            'Explore collaborative articles'
                        ];
                        for (const em of endMarkers) {
                            const eIdx = sub.indexOf(em);
                            if (eIdx > 80) {
                                sub = sub.slice(0, eIdx).trim();
                            }
                        }
                        if (sub.length > 40) return sub;
                    }
                }
                return '';
                """
            )
            if isinstance(desc, str) and desc.strip():
                return desc.strip()
        except Exception as exc:
            logger.debug("SDUI job_description_text fallback error: %s", exc)
        return ""
