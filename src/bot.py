"""Browser lifecycle, login / 2FA wait, captcha pause, run loop."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src import selectors as sel
from src.config import AppConfig, EnvSettings
from src.job_search import JobCard, JobSearch
from src.logger import log_job
from src.ollama_client import OllamaClient
from src.reporter import JobResult, Reporter
from src.utils import human_delay, normalize_text, project_path, short_delay

logger = logging.getLogger("easyapply")


class EasyApplyBot:
    def __init__(self, config: AppConfig, env: EnvSettings) -> None:
        self.config = config
        self.env = env
        self.driver = None
        self.ollama = OllamaClient(env)
        self.reporter = Reporter()
        self._stop = False
        self._applied_today = 0

    def start_browser(self) -> None:
        import undetected_chromedriver as uc

        from src.chrome_driver import (
            chrome_major_version,
            ensure_arm64_chromedriver,
            purge_bad_uc_cache,
        )

        purge_bad_uc_cache()
        driver_path = ensure_arm64_chromedriver()
        version_main = chrome_major_version()

        profile = str(project_path(self.env.chrome_profile_dir))
        project_path(self.env.chrome_profile_dir).mkdir(parents=True, exist_ok=True)
        options = uc.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-service-autorun")
        options.add_argument("--password-store=basic")

        kwargs: dict = {"options": options}
        if version_main:
            kwargs["version_main"] = version_main
        if driver_path is not None:
            kwargs["driver_executable_path"] = str(driver_path)
            logger.info("Using arm64 chromedriver: %s (Chrome major=%s)", driver_path, version_main)

        try:
            self.driver = uc.Chrome(**kwargs)
        except OSError as exc:
            # Retry once after purging a bad UC cache drop-in
            if getattr(exc, "errno", None) == 86:
                logger.warning("Bad CPU type on chromedriver — retrying with fresh arm64 binary")
                purge_bad_uc_cache()
                driver_path = ensure_arm64_chromedriver()
                if driver_path is not None:
                    kwargs["driver_executable_path"] = str(driver_path)
                self.driver = uc.Chrome(**kwargs)
            else:
                raise
        self.driver.set_page_load_timeout(60)
        logger.info("Chrome started with profile %s", profile)

    def close(self) -> None:
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        try:
            self.ollama.close()
        except Exception:
            pass

    def check_health(self) -> bool:
        ok = True
        try:
            models = self.ollama.ensure_models()
            logger.info("Ollama OK. Models: %s", models[:8])
        except Exception as exc:
            logger.error("Ollama check failed: %s", exc)
            ok = False
        resume = self.config.resume_path()
        if self.config.uploads.Resume and not resume.exists():
            logger.warning("Resume PDF missing at %s (upload will fail until you add it)", resume)
        if not self.env.linkedin_email or not self.env.linkedin_password:
            logger.warning("LINKEDIN_EMAIL / LINKEDIN_PASSWORD not set in .env")
        logger.info(
            "Config OK. dry_run=%s positions=%s tech=%s keywords=%s locations=%s",
            self.config.session.dry_run,
            len(self.config.positions),
            len(self.config.tech_keywords),
            len(self.config.search_keywords()),
            len(self.config.locations),
        )
        return ok

    def login(self) -> bool:
        assert self.driver
        email = (self.env.linkedin_email or "").strip()
        password = (self.env.linkedin_password or "").strip()

        self.driver.get("https://www.linkedin.com/feed/")
        short_delay(2500)
        self._dismiss_cookie_banners()
        if self._is_logged_in():
            logger.info("Already logged in via Chrome profile")
            return True

        if not email or not password:
            logger.warning(
                "No LINKEDIN_EMAIL/PASSWORD in .env — waiting for manual login in the browser..."
            )
            return self._wait_until_logged_in(manual_hint=True)

        logger.info(
            "Credentials loaded (email_len=%s password_len=%s). Current url=%s",
            len(email),
            len(password),
            self.driver.current_url,
        )

        # Feed often redirects to /login already — try current page first
        attempts = [
            None,  # current page
            "https://www.linkedin.com/login",
            "https://www.linkedin.com/uas/login",
        ]
        filled = False
        for login_url in attempts:
            if login_url:
                self.driver.get(login_url)
                short_delay(2500)
            self._dismiss_cookie_banners()
            if self._is_logged_in():
                logger.info("Logged in during login navigation")
                return True
            logger.info("Trying login form on url=%s", self.driver.current_url)
            if self._fill_login_form(email, password):
                filled = True
                break

        if not filled:
            logger.warning(
                "Could not fill login form (url=%s). "
                "Log in manually in the Chrome window; bot will wait.",
                self.driver.current_url,
            )
            self._dump_login_debug()
            return self._wait_until_logged_in(manual_hint=True)

        logger.info(
            "Login submitted. Waiting up to %s min for mobile/2FA if needed...",
            self.config.session.login_2fa_wait_minutes,
        )
        return self._wait_until_logged_in(manual_hint=False)

    def _find_visible(self, selectors: list[tuple]) -> object | None:
        assert self.driver
        for by, sel_str in selectors:
            try:
                for el in self.driver.find_elements(by, sel_str):
                    if el.is_displayed() and el.is_enabled():
                        return el
            except Exception:
                continue
        return None

    def _type_into(self, el, text: str) -> bool:
        """Type into LinkedIn React inputs; verify value stuck."""
        assert self.driver
        import platform

        from selenium.webdriver.common.action_chains import ActionChains

        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
            short_delay(200)
            try:
                el.click()
            except Exception:
                self.driver.execute_script("arguments[0].focus();", el)
            short_delay(150)

            # Clear existing value (chord must be one string: Keys.COMMAND + "a")
            mod = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
            el.send_keys(mod + "a")
            el.send_keys(Keys.DELETE)
            el.send_keys(Keys.BACKSPACE)
            try:
                el.clear()
            except Exception:
                pass
            short_delay(100)

            # Human-like typing (React listens to real key events)
            for ch in text:
                el.send_keys(ch)
                time.sleep(0.02)
            short_delay(250)
            current = el.get_attribute("value") or ""
            if current == text:
                return True

            logger.info(
                "send_keys incomplete (got_len=%s want_len=%s) — JS fallback",
                len(current),
                len(text),
            )
            # JS + native input value setter (bypasses React some of the time)
            self.driver.execute_script(
                """
                const el = arguments[0];
                const val = arguments[1];
                const proto = window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                el.focus();
                if (desc && desc.set) { desc.set.call(el, val); }
                else { el.value = val; }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                el,
                text,
            )
            short_delay(200)
            current = el.get_attribute("value") or ""
            if current == text:
                return True

            ActionChains(self.driver).click(el).pause(0.1).key_down(mod).send_keys(
                "a"
            ).key_up(mod).send_keys(Keys.DELETE).send_keys(text).perform()
            short_delay(200)
            current = el.get_attribute("value") or ""
            ok = current == text or (len(current) > 0 and len(text) > 0)
            if not ok:
                logger.warning("Field value still empty after all strategies")
            return ok
        except Exception as exc:
            logger.warning("Type into field failed: %s", exc)
            return False

    def _fill_login_form(self, email: str, password: str) -> bool:
        """Fill LinkedIn login — supports classic (#username) and new Voyager UI.

        New UI (from live DOM):
          - input[type=email] autocomplete="username webauthn"  (dynamic id)
          - input[type=password] autocomplete="current-password"
          - button[type=button] with visible text "Sign in" (NOT type=submit)
        """
        assert self.driver
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        # Prefer new UI selectors first (what user sees today)
        email_selectors = [
            (By.CSS_SELECTOR, "input[type='email']"),
            (By.CSS_SELECTOR, "input[autocomplete*='username']"),
            (By.XPATH, "//label[contains(., 'Email or phone')]/following::input[1]"),
            (By.XPATH, "//input[@type='email' or contains(@autocomplete,'username')]"),
            (By.ID, "username"),
            (By.NAME, "session_key"),
            (By.CSS_SELECTOR, "input#username"),
        ]
        password_selectors = [
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
            (By.CSS_SELECTOR, "input[autocomplete*='current-password']"),
            (By.XPATH, "//label[contains(., 'Password')]/following::input[@type='password'][1]"),
            (By.ID, "password"),
            (By.NAME, "session_password"),
            (By.CSS_SELECTOR, "input#password"),
        ]

        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "input[type='email'], input[type='password'], #username, #password",
                    )
                )
            )
        except Exception:
            logger.warning("No login inputs appeared on page")
            return False

        short_delay(800)
        email_el = self._find_visible(email_selectors)
        password_el = self._find_visible(password_selectors)
        if email_el is None or password_el is None:
            # Debug: list inputs on page
            try:
                inputs = self.driver.find_elements(By.CSS_SELECTOR, "input")
                summary = [
                    f"type={i.get_attribute('type')!r} auto={i.get_attribute('autocomplete')!r} "
                    f"id={i.get_attribute('id')!r} displayed={i.is_displayed()}"
                    for i in inputs[:12]
                ]
                logger.warning(
                    "Login fields not visible (email=%s password=%s). Inputs: %s",
                    email_el is not None,
                    password_el is not None,
                    summary,
                )
            except Exception:
                logger.warning(
                    "Login fields not visible (email=%s password=%s)",
                    email_el is not None,
                    password_el is not None,
                )
            return False

        logger.info(
            "Login fields found (email type=%s id=%s, password id=%s) — typing...",
            email_el.get_attribute("type"),
            email_el.get_attribute("id"),
            password_el.get_attribute("id"),
        )
        if not self._type_into(email_el, email):
            logger.error("Email was not accepted by the input")
            self._dump_login_debug()
            return False
        if not self._type_into(password_el, password):
            logger.error("Password was not accepted by the input")
            self._dump_login_debug()
            return False

        logger.info(
            "Typed OK (email_value_len=%s password_value_len=%s) — clicking Sign in",
            len(email_el.get_attribute("value") or ""),
            len(password_el.get_attribute("value") or ""),
        )

        if not self._click_sign_in_button():
            logger.warning("Sign in button click failed — sending ENTER")
            try:
                password_el.send_keys(Keys.RETURN)
            except Exception:
                pass
        short_delay(2500)
        return True

    def _click_sign_in_button(self) -> bool:
        """Click the new LinkedIn 'Sign in' button (type=button, not submit)."""
        assert self.driver
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            candidates = []
            for btn in buttons:
                try:
                    if not btn.is_displayed():
                        continue
                    label = " ".join((btn.text or "").split()).strip().lower()
                    if not label:
                        continue
                    if "apple" in label or "google" in label or "join" in label:
                        continue
                    # Exact primary CTA
                    if label == "sign in":
                        candidates.insert(0, btn)
                    elif label.startswith("sign in") and "with" not in label:
                        candidates.append(btn)
                except Exception:
                    continue
            for btn in candidates:
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    short_delay(150)
                    self.driver.execute_script("arguments[0].click();", btn)
                    logger.info("Clicked Sign in button (text=%r)", (btn.text or "").strip()[:40])
                    return True
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Sign in button scan failed: %s", exc)
        # XPath fallback
        for xp in (
            "//button[.//span[normalize-space()='Sign in']]",
            "//button[@type='submit']",
        ):
            try:
                for btn in self.driver.find_elements(By.XPATH, xp):
                    if btn.is_displayed():
                        self.driver.execute_script("arguments[0].click();", btn)
                        logger.info("Clicked Sign in via xpath")
                        return True
            except Exception:
                continue
        return False

    def _safe_current_url(self) -> str:
        try:
            if not self.driver:
                return ""
            url = self.driver.current_url
            return (url or "").lower()
        except Exception:
            return ""

    def _wait_until_logged_in(self, manual_hint: bool = False) -> bool:
        """Wait after Sign in. Human only needed for mobile/app approve (2FA)."""
        if manual_hint:
            logger.warning(
                "Complete LinkedIn login in the open Chrome window "
                "(approve the sign-in request on your phone if asked)."
            )
        else:
            logger.info(
                "If LinkedIn asks, approve the sign-in on your phone — "
                "that is the only human step. No captcha expected during search."
            )
        deadline = time.time() + self.config.session.login_2fa_wait_minutes * 60
        reminded = False
        while time.time() < deadline:
            try:
                self._dismiss_cookie_banners()
                url = self._safe_current_url()
                if self._login_verification_present(url) and not reminded:
                    logger.warning(
                        "Mobile / 2FA verification page detected (url=%s). "
                        "Approve on your phone, then wait — bot continues automatically.",
                        url[:120],
                    )
                    reminded = True
                if self._is_logged_in():
                    logger.info("Login successful")
                    return True
            except Exception as exc:
                logger.debug("Login wait poll error: %s", exc)
            time.sleep(3)
        logger.error(
            "Login timed out waiting for mobile approve / redirect (url=%s)",
            self._safe_current_url(),
        )
        self._dump_login_debug()
        return False

    def _dismiss_cookie_banners(self) -> None:
        assert self.driver
        texts = (
            "accept",
            "agree",
            "allow",
            "ok",
            "got it",
            "akzeptieren",
            "zustimmen",
            "alle akzeptieren",
        )
        try:
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "button"):
                label = (btn.text or btn.get_attribute("aria-label") or "").strip().lower()
                if label and any(t in label for t in texts) and btn.is_displayed():
                    try:
                        btn.click()
                        short_delay(400)
                        return
                    except Exception:
                        continue
            # LinkedIn-specific cookie action
            for css in (
                "button[action-type='ACCEPT']",
                "button[data-test-id='accept-btn']",
            ):
                for btn in self.driver.find_elements(By.CSS_SELECTOR, css):
                    if btn.is_displayed():
                        btn.click()
                        short_delay(400)
                        return
        except Exception:
            pass

    def _dump_login_debug(self) -> None:
        assert self.driver
        try:
            dump = project_path("crash_dumps")
            dump.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            png = dump / f"login_debug_{stamp}.png"
            html = dump / f"login_debug_{stamp}.html"
            self.driver.save_screenshot(str(png))
            html.write_text(self.driver.page_source, encoding="utf-8")
            logger.info("Login debug saved: %s url=%s", png, self.driver.current_url)
        except Exception as exc:
            logger.debug("Login debug dump failed: %s", exc)

    def _is_logged_in(self) -> bool:
        assert self.driver
        url = self._safe_current_url()
        if not url:
            return False
        # Guest / auth pages are NOT logged in
        if any(
            x in url
            for x in (
                "/login",
                "/uas/login",
                "/checkpoint/",
                "/authwall",
                "signup",
                "/guest/",
            )
        ):
            return False
        try:
            nav = self.driver.find_elements(
                By.CSS_SELECTOR,
                "global-nav, .global-nav, #global-nav, "
                "button.global-nav__primary-link-me-menu-trigger, "
                "img.global-nav__me-photo, .feed-identity-module",
            )
            if any(el.is_displayed() for el in nav):
                return True
        except Exception:
            pass
        # Logged-in app routes (feed OR jobs search/results)
        if any(
            p in url
            for p in (
                "linkedin.com/feed",
                "linkedin.com/jobs",
                "linkedin.com/in/",
                "linkedin.com/mynetwork",
            )
        ):
            return True
        return False

    def _login_verification_present(self, url: str | None = None) -> bool:
        """True only on post-signin mobile/email verification pages."""
        url = (url if url is not None else self._safe_current_url()) or ""
        return any(f in url for f in sel.LOGIN_VERIFY_URL_FRAGMENTS)

    def _checkpoint_present(self) -> bool:
        """Strict URL-only check. Never scan page HTML for 'captcha' (false positives)."""
        url = self._safe_current_url()
        if not url:
            return False
        return any(f in url for f in sel.CHECKPOINT_URL_FRAGMENTS)

    def run(self) -> Reporter:
        assert self.driver
        from src.application import ApplicationHandler
        from src.qa_intent import IntentMatcher
        from src.qa_manager import QAMemoryManager

        qa = QAMemoryManager(self.config, self.ollama)
        intent = IntentMatcher(self.config, self.ollama)
        search = JobSearch(self.driver, self.config)
        applier = ApplicationHandler(self.driver, self.config, self.ollama, qa, intent)

        start = datetime.now()
        deadline = start + timedelta(seconds=self.config.session.max_search_time)
        max_apps = self.config.session.max_applications_per_run
        max_day = self.config.session.max_applications_per_day
        attempted = 0

        queries = search.iter_queries()
        logger.info("Search pairs: %s", len(queries))

        seen_this_run: set[str] = set()

        for keywords, location in queries:
            if datetime.now() >= deadline or attempted >= max_apps or self._stop:
                break
            if self._applied_today >= max_day:
                logger.info("Daily cap reached (%s)", max_day)
                break

            try:
                search.open_search(keywords, location)
                human_delay(
                    self.config.session.min_delay_seconds,
                    self.config.session.max_delay_seconds,
                )
                short_delay(2000)
                # Daily-limit banner lives in the search right pane
                # (.jobs-search__job-details--container), not always on /jobs/view/.
                # LinkedIn auto-selects the first card — check before any navigation.
                try:
                    WebDriverWait(self.driver, 8).until(
                        EC.presence_of_element_located(
                            (
                                By.CSS_SELECTOR,
                                ".jobs-search__job-details--container, "
                                ".jobs-details__main-content, "
                                "#jobs-apply-button-id, "
                                ".artdeco-inline-feedback--error",
                            )
                        )
                    )
                    short_delay(1200)
                except Exception:
                    pass
                if applier.easy_apply_daily_limit_reached():
                    self._stop = True
                    logger.warning(
                        "Stopping run — LinkedIn Easy Apply daily limit on search "
                        "results (We limit daily submissions… apply tomorrow)"
                    )
                    break

                cards = search.parse_cards()
                self.reporter.scanned += len(cards)
                logger.info("Found %s cards for '%s' @ %s", len(cards), keywords, location)

                for card in cards:
                    if datetime.now() >= deadline or attempted >= max_apps:
                        break
                    if not card.job_id:
                        continue
                    if card.job_id in seen_this_run or card.job_id in self.reporter.seen_ids:
                        self._record(card, "SKIPPED_SEEN")
                        continue
                    seen_this_run.add(card.job_id)

                    reason = search.is_blacklisted(card)
                    if reason:
                        self._record(card, "SKIPPED_BLACKLIST", notes=reason)
                        continue

                    try:
                        self.driver.get(card.link)
                        human_delay(
                            self.config.session.min_delay_seconds,
                            self.config.session.max_delay_seconds,
                        )
                        desc = search.job_description_text()
                        if search.requires_strict_german_c1(desc + " " + card.title):
                            self._record(card, "SKIPPED_LANGUAGE", notes="strict German C1")
                            continue

                        page = normalize_text(self.driver.find_element(By.TAG_NAME, "body").text)
                        if "applied" in page and "easy apply" not in page:
                            self._record(card, "SKIPPED_ALREADY_APPLIED")
                            continue

                        # Red warning under grayed Easy Apply — stop whole run
                        if applier.easy_apply_daily_limit_reached():
                            self._record(
                                card,
                                "STOPPED_EASY_APPLY_LIMIT",
                                notes="LinkedIn daily Easy Apply limit",
                            )
                            self._stop = True
                            logger.warning(
                                "Stopping run — LinkedIn Easy Apply daily limit "
                                "(save job and apply tomorrow)"
                            )
                            break

                        status = applier.apply_to_job(card)
                        attempted += 1
                        # Button-not-found path used to return FAILED_EXCEPTION while
                        # the red limit banner was already on screen — re-check & stop.
                        if status != "STOPPED_EASY_APPLY_LIMIT" and (
                            applier.easy_apply_daily_limit_reached()
                        ):
                            status = "STOPPED_EASY_APPLY_LIMIT"
                        if status == "APPLIED":
                            self._applied_today += 1
                        self._record(card, status)
                        if status == "STOPPED_EASY_APPLY_LIMIT":
                            self._stop = True
                            logger.warning(
                                "Stopping run — LinkedIn Easy Apply daily limit reached"
                            )
                            break
                        human_delay(
                            self.config.session.min_delay_seconds,
                            self.config.session.max_delay_seconds,
                        )
                    except Exception as exc:
                        logger.exception("Job loop error: %s", exc)
                        self._record(card, "FAILED_EXCEPTION", notes=str(exc))
                        attempted += 1
                if self._stop:
                    break
            except Exception as exc:
                logger.exception("Search loop error: %s", exc)
                # Do not pause for "captcha" mid-run — continue next query
                continue

        return self.reporter

    def _record(self, card: JobCard, status: str, notes: str = "") -> None:
        log_job(
            logger,
            status,
            job_id=card.job_id,
            title=card.title,
            company=card.company,
            extra=notes,
        )
        self.reporter.record(
            JobResult(
                status=status,
                job_id=card.job_id,
                title=card.title,
                company=card.company,
                location=card.location,
                link=card.link,
                notes=notes,
            )
        )
