"""Easy Apply modal filling, EU rules, submit / discard."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from selenium.common.exceptions import ElementClickInterceptedException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from src import selectors as sel
from src.config import AppConfig, load_resume_json
from src.cover_letter import build_cover_letter
from src.job_search import JobCard
from src.ollama_client import OllamaClient, sanitize_human_text, truncate_at_word
from src.qa_intent import IntentMatcher
from src.qa_manager import QAMemoryManager, append_unanswered
from src.utils import (
    earliest_start_date_iso,
    earliest_start_date_mmddyyyy,
    fold_accents,
    notice_period_value,
    normalize_text,
    project_path,
    short_delay,
)

logger = logging.getLogger("easyapply")

MODAL_WAIT_SECONDS = 20
FORM_READY_SECONDS = 12
STEP_CHANGE_SECONDS = 10


class ApplicationHandler:
    def __init__(
        self,
        driver: WebDriver,
        config: AppConfig,
        ollama: OllamaClient,
        qa: QAMemoryManager,
        intent: IntentMatcher,
    ) -> None:
        self.driver = driver
        self.config = config
        self.ollama = ollama
        self.qa = qa
        self.intent = intent
        self.resume = load_resume_json()
        self.wait = WebDriverWait(driver, MODAL_WAIT_SECONDS)
        self._current_job: JobCard | None = None

    def apply_to_job(self, job: JobCard) -> str:
        """Return status string."""
        self._current_job = job
        # Job top-card (apply + limit banner) can lag after navigation
        short_delay(800)
        if self.easy_apply_daily_limit_reached():
            logger.warning(
                "LinkedIn Easy Apply daily limit reached — ending applications"
            )
            return "STOPPED_EASY_APPLY_LIMIT"
        if not self._click_easy_apply():
            short_delay(500)
            if self.easy_apply_daily_limit_reached():
                logger.warning(
                    "LinkedIn Easy Apply daily limit reached — ending applications"
                )
                return "STOPPED_EASY_APPLY_LIMIT"
            disabled = self._find_easy_apply_button(include_disabled=True)
            if disabled is not None and self._easy_apply_button_disabled(disabled):
                logger.warning(
                    "Easy Apply grayed out — treating as daily limit, stopping"
                )
                return "STOPPED_EASY_APPLY_LIMIT"
            logger.warning("Easy Apply modal did not load for job %s", job.job_id)
            return "FAILED_EXCEPTION"
        if self.easy_apply_daily_limit_reached():
            logger.warning(
                "LinkedIn Easy Apply daily limit reached — ending applications"
            )
            self._discard_modal()
            return "STOPPED_EASY_APPLY_LIMIT"
        try:
            for _step in range(12):
                if self.easy_apply_daily_limit_reached():
                    logger.warning(
                        "LinkedIn Easy Apply daily limit reached — ending applications"
                    )
                    self._discard_modal()
                    return "STOPPED_EASY_APPLY_LIMIT"
                if not self._wait_for_form_ready(timeout=FORM_READY_SECONDS):
                    if self.easy_apply_daily_limit_reached():
                        self._discard_modal()
                        return "STOPPED_EASY_APPLY_LIMIT"
                    self._dump_and_discard(job, "Easy Apply form content did not load")
                    return "FAILED_EXCEPTION"

                self._fill_current_page()
                self._ensure_required_fields_filled()

                if not self._handle_validation_errors_with_learning():
                    still = self._validation_errors()
                    if still and self.config.session.unattended_mode:
                        self._dump_and_discard(job, "; ".join(still))
                        return "FAILED_VALIDATION"

                if self._is_review_or_submit_step():
                    self._uncheck_follow_company()
                    if self.config.session.dry_run:
                        logger.info("DRY_RUN: stopping before submit for %s", job.job_id)
                        self._discard_modal()
                        return "DRY_RUN"
                    if self._click_submit():
                        if self._confirm_applied():
                            return "APPLIED"
                        return "FAILED_EXCEPTION"

                # Block Next/Review if radios/required still empty
                still_pending = self._collect_unfilled_fields()
                if still_pending:
                    logger.info(
                        "Not advancing — unfilled: %s",
                        [p[:70] for p in still_pending[:4]],
                    )
                    self._fill_current_page()
                    self._ensure_required_fields_filled()
                    still_pending = self._collect_unfilled_fields()
                    if still_pending:
                        # One more intent pass on radios specifically
                        self._fill_sdui_radio_groups()
                        self._fill_sdui_checkboxes()
                        still_pending = self._collect_unfilled_fields()
                    if still_pending and self.config.session.unattended_mode:
                        self._dump_and_discard(
                            job, "Unfilled: " + "; ".join(still_pending[:3])
                        )
                        return "FAILED_VALIDATION"

                before = self._step_fingerprint()
                if not self._click_next():
                    if self._click_submit():
                        if self.config.session.dry_run:
                            self._discard_modal()
                            return "DRY_RUN"
                        if self._confirm_applied():
                            return "APPLIED"
                    break

                # After Next: either advance, or stay with validation / unfilled fields
                advanced = self._wait_for_step_change(before, timeout=STEP_CHANGE_SECONDS)
                errors = self._validation_errors()
                pending = self._collect_unfilled_fields()
                if errors or (not advanced and pending):
                    logger.info(
                        "After Next — advanced=%s errors=%s pending=%s — recovering",
                        advanced,
                        errors[:4],
                        [p[:50] for p in pending[:4]],
                    )
                    err_blob = normalize_text(" ".join(errors))
                    if errors and (
                        "invalid" in err_blob
                        or "must be a number" in err_blob
                        or "whole number" in err_blob
                    ):
                        # Only rewrite the bad field(s) as numbers — do not refill the page
                        if not self._handle_validation_errors_with_learning():
                            still = self._validation_errors() or self._collect_unfilled_fields()
                            if still and self.config.session.unattended_mode:
                                self._dump_and_discard(job, "; ".join(still[:3]))
                                return "FAILED_VALIDATION"
                    else:
                        self._fill_current_page()
                        self._ensure_required_fields_filled()
                        self._fill_sdui_radio_groups()
                        if errors and not self._handle_validation_errors_with_learning():
                            still = self._validation_errors() or self._collect_unfilled_fields()
                            if still and self.config.session.unattended_mode:
                                self._dump_and_discard(job, "; ".join(still[:3]))
                                return "FAILED_VALIDATION"
                elif not advanced:
                    self._debug_fill("Step fingerprint unchanged after Next; continuing")

            if self.config.session.dry_run:
                self._discard_modal()
                return "DRY_RUN"
            self._dump_and_discard(job, "Could not complete Easy Apply steps")
            return "FAILED_VALIDATION"
        except Exception as exc:
            logger.exception("Apply failed: %s", exc)
            try:
                self._dump_and_discard(job, str(exc))
            except Exception:
                pass
            return "FAILED_EXCEPTION"

    # ----- navigation -----

    def easy_apply_daily_limit_reached(self) -> bool:
        """True when LinkedIn shows the red daily-limit alert under Easy Apply.

        Live DOM::

            button.jobs-apply-button.artdeco-button--disabled[disabled]
            + .artdeco-inline-feedback--error .artdeco-inline-feedback__message
              "We limit daily submissions … Save this job and apply tomorrow."
        """
        # Keep markers short — LinkedIn copy must contain at least one of these
        markers = (
            "save this job and apply tomorrow",
            "limit daily submissions",
            "we limit daily submissions",
            "prevent bots, helping each application",
            "maintain quality and prevent bots",
            "today's easy apply limit",
            "todays easy apply limit",
            "bewerben sie sich morgen",
        )
        try:
            # Fast path in the browser (most reliable; ignores Selenium displayed quirks)
            hit = self.driver.execute_script(
                """
                const markers = arguments[0];
                const norm = (s) => (s || '').toLowerCase();
                // 1) Red feedback message under Easy Apply
                const msgs = document.querySelectorAll(
                  '.artdeco-inline-feedback__message, '
                  + '.artdeco-inline-feedback--error, '
                  + '[role="alert"]'
                );
                for (const el of msgs) {
                  const t = norm(el.innerText || el.textContent);
                  if (!t) continue;
                  for (const m of markers) {
                    if (t.includes(m)) return true;
                  }
                }
                // 2) Apply card / search right-pane / top card text
                const roots = document.querySelectorAll(
                  '.jobs-search__job-details--container, '
                  + '.jobs-search__job-details--wrapper, '
                  + '.jobs-details__main-content, '
                  + '.jobs-s-apply, .jobs-apply-button--top-card, .mt4, '
                  + '.job-details-jobs-unified-top-card, .jobs-unified-top-card'
                );
                for (const el of roots) {
                  const t = norm(el.innerText || el.textContent);
                  for (const m of markers) {
                    if (t.includes(m)) return true;
                  }
                }
                // 3) Full page text / HTML
                const body = norm(document.body && document.body.innerText);
                const html = norm(document.documentElement
                  && document.documentElement.innerHTML);
                for (const m of markers) {
                  if (body.includes(m) || html.includes(m)) return true;
                }
                // 4) Grayed Easy Apply present + "apply tomorrow" anywhere
                const btn = document.querySelector(
                  'button.jobs-apply-button.artdeco-button--disabled, '
                  + 'button.jobs-apply-button[disabled], '
                  + '#jobs-apply-button-id[disabled]'
                );
                if (btn && (body.includes('apply tomorrow')
                    || body.includes('daily submissions')
                    || html.includes('apply tomorrow'))) {
                  return true;
                }
                return false;
                """,
                list(markers),
            )
            if hit:
                return True
        except Exception as exc:
            logger.debug("easy_apply_daily_limit JS probe failed: %s", exc)
        # Python fallback
        try:
            blob = normalize_text(self.driver.page_source or "")
            return any(m in blob for m in markers)
        except Exception:
            return False

    def _wait_for_easy_apply_limit_banner(self, timeout: float = 4.0) -> bool:
        """After clicking Easy Apply, wait for grayed button + red limit message."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.easy_apply_daily_limit_reached():
                return True
            short_delay(250)
        return self.easy_apply_daily_limit_reached()

    @staticmethod
    def _easy_apply_button_disabled(btn) -> bool:
        try:
            if not btn.is_enabled():
                return True
            if (btn.get_attribute("aria-disabled") or "").lower() == "true":
                return True
            # HTML: disabled="" on the button
            if btn.get_attribute("disabled") is not None:
                return True
            cls = (btn.get_attribute("class") or "").lower()
            if "artdeco-button--disabled" in cls:
                return True
        except Exception:
            return False
        return False

    def _click_easy_apply(self) -> bool:
        """Click Easy Apply once, then wait for the form dialog — never re-click Easy Apply."""
        try:
            # Form already open from a previous step / slow paint
            if self._easy_apply_form_is_open():
                logger.info("Easy Apply form already open — skipping button click")
                return self._wait_for_form_ready(timeout=FORM_READY_SECONDS)

            # Already grayed with red banner (limit hit earlier on this page)
            if self.easy_apply_daily_limit_reached():
                logger.warning("Easy Apply daily limit banner already visible")
                return False

            btn = self._find_easy_apply_button(include_disabled=True)
            if btn is None:
                short_delay(1000)
                if self.easy_apply_daily_limit_reached():
                    logger.warning(
                        "No Easy Apply button but daily-limit banner is on page — stop"
                    )
                    return False
                try:
                    src = (self.driver.page_source or "").lower()
                    logger.warning(
                        "Easy Apply button not found "
                        "(limit_text_in_html=%s, jobs_apply_in_html=%s)",
                        "save this job and apply tomorrow" in src
                        or "limit daily submissions" in src,
                        "jobs-apply-button" in src,
                    )
                except Exception:
                    logger.warning("Easy Apply button not found")
                return False

            if self._easy_apply_button_disabled(btn):
                # Disabled before click — wait briefly for feedback text to paint
                if self._wait_for_easy_apply_limit_banner(timeout=3.0):
                    logger.warning("Easy Apply grayed + daily limit message — stop")
                    return False
                logger.warning("Easy Apply button is grayed/disabled")
                return False

            label = (btn.text or btn.get_attribute("aria-label") or "").strip()
            logger.info("Clicking Easy Apply button: %r", label[:80])
            self._safe_click(btn)

            # Click → button gets artdeco-button--disabled + red alert below
            if self._wait_for_easy_apply_limit_banner(timeout=4.0):
                logger.warning(
                    "Easy Apply daily limit after click "
                    "(We limit daily submissions… apply tomorrow) — stop"
                )
                return False

            if not self._wait_for_easy_apply_modal(timeout=MODAL_WAIT_SECONDS):
                # Re-check limit — banner can lag behind modal timeout
                if self.easy_apply_daily_limit_reached():
                    logger.warning("Easy Apply daily limit detected after modal wait — stop")
                    return False
                self._dump_modal_debug("modal_not_detected")
                logger.warning(
                    "Easy Apply form did not become ready within %ss "
                    "(button clicked; form may use unrecognized markup)",
                    MODAL_WAIT_SECONDS,
                )
                return False
            logger.info("Easy Apply form is open and ready")
            return True
        except Exception as exc:
            logger.warning("Easy Apply click failed: %s", exc)
            return False

    def _find_easy_apply_button(self, include_disabled: bool = False):
        """Job-page Easy Apply only — ignore buttons inside an already-open dialog.

        Disabled (grayed) buttons are still returned when ``include_disabled`` so we
        can detect the daily-limit state.
        """
        candidates = []
        for btn in self.driver.find_elements(By.CSS_SELECTOR, sel.EASY_APPLY_BUTTON):
            try:
                # Do not require is_displayed for disabled Easy Apply — grayed controls
                # sometimes fail Selenium's displayed check.
                if not include_disabled:
                    if not self._el_displayed(btn):
                        continue
                    if self._easy_apply_button_disabled(btn):
                        continue
                in_dialog = self.driver.execute_script(
                    """
                    const el = arguments[0];
                    return !!(el.closest(
                      'dialog, .artdeco-modal, [role="dialog"], .jobs-easy-apply-modal'
                    ));
                    """,
                    btn,
                )
                if in_dialog:
                    continue
                text = normalize_text(btn.text or "")
                aria = normalize_text(btn.get_attribute("aria-label") or "")
                cls = (btn.get_attribute("class") or "").lower()
                blob = f"{text} {aria}"
                if "jobs-apply-button" in cls or btn.get_attribute("id") == "jobs-apply-button-id":
                    candidates.insert(0, btn)
                    continue
                if any(
                    k in blob
                    for k in ("easy apply", "einfach bewerben", "candidatura semplificata")
                ):
                    candidates.insert(0, btn)
                elif "apply" in blob and "save" not in blob and "external" not in blob:
                    candidates.append(btn)
            except Exception:
                continue
        return candidates[0] if candidates else None

    def _easy_apply_form_is_open(self) -> bool:
        """True when SDUI <dialog> or legacy Easy Apply modal is open."""
        try:
            # 2026 SDUI: native <dialog data-testid="dialog" open>
            for css in (
                'dialog[data-testid="dialog"][open]',
                "dialog[open]",
                'dialog[data-testid="dialog"]',
                '[data-sdui-screen*="EasyApply"]',
                '[data-sdui-screen*="easyapply"]',
            ):
                for el in self.driver.find_elements(By.CSS_SELECTOR, css):
                    if self._el_displayed(el) or self._dialog_is_open(el):
                        return True
            # Header text "Apply to ..." inside open dialog
            for h in self.driver.find_elements(By.CSS_SELECTOR, "#dialog-header, dialog h2"):
                if self._el_displayed(h) and "apply to" in normalize_text(h.text or ""):
                    return True
            # Legacy Voyager modal
            for css in (
                ".jobs-easy-apply-modal",
                ".jobs-easy-apply-content",
                ".fb-dash-form-element",
            ):
                for el in self.driver.find_elements(By.CSS_SELECTOR, css):
                    if self._el_displayed(el):
                        return True
        except Exception:
            pass
        return False

    def _dialog_is_open(self, el) -> bool:
        """Native <dialog> may report open via property even when CSS display is odd."""
        try:
            tag = (el.tag_name or "").lower()
            if tag == "dialog":
                if el.get_attribute("open") is not None:
                    return True
                return bool(
                    self.driver.execute_script("return !!arguments[0].open;", el)
                )
        except Exception:
            pass
        try:
            return el.is_displayed()
        except Exception:
            return False

    def _wait_for_easy_apply_modal(self, timeout: float = MODAL_WAIT_SECONDS) -> bool:
        """Wait until form dialog is open — never re-click Easy Apply."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._easy_apply_form_is_open():
                logger.info("Detected open Easy Apply dialog")
                return self._wait_for_form_ready(timeout=min(FORM_READY_SECONDS, 8))
            short_delay(250)
        return False

    def _wait_for_form_ready(self, timeout: float = FORM_READY_SECONDS) -> bool:
        """Wait until dialog has fields or Next/Submit."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._modal_has_interactive_content():
                short_delay(400)
                return True
            if self._easy_apply_form_is_open():
                # Dialog shell is enough to proceed (contact step often pre-filled)
                short_delay(300)
                return True
            short_delay(200)
        return self._easy_apply_form_is_open()

    def _modal_visible(self) -> bool:
        return self._easy_apply_form_is_open()

    def _modal_root(self):
        """Best container for the open Easy Apply dialog (SDUI first, then legacy)."""
        for css in (
            'dialog[data-testid="dialog"][open]',
            "dialog[open]",
            'dialog[data-testid="dialog"]',
            '[data-sdui-screen*="EasyApply"]',
            ".jobs-easy-apply-modal",
            ".jobs-easy-apply-content",
            "div[data-test-modal][role='dialog']",
            ".artdeco-modal[role='dialog']",
            "[role='dialog']",
        ):
            for m in self.driver.find_elements(By.CSS_SELECTOR, css):
                try:
                    if not (self._el_displayed(m) or self._dialog_is_open(m)):
                        continue
                    # Prefer dialog that hosts apply form controls
                    has_form = m.find_elements(
                        By.CSS_SELECTOR,
                        "input, textarea, select, button, label, "
                        ".fb-dash-form-element, [data-sdui-screen*='EasyApply']",
                    )
                    if has_form:
                        return m
                except Exception:
                    continue
        for m in self.driver.find_elements(By.CSS_SELECTOR, sel.EASY_APPLY_MODAL):
            try:
                if self._el_displayed(m) or self._dialog_is_open(m):
                    return m
            except Exception:
                continue
        return None

    def _modal_has_interactive_content(self) -> bool:
        modal = self._modal_root()
        if modal is None:
            return False
        try:
            inputs = modal.find_elements(
                By.CSS_SELECTOR,
                "input:not([type='hidden']), textarea, select, input[type='file']",
            )
            if inputs:
                return True
            # Footer Next / Submit inside dialog
            for btn in modal.find_elements(By.CSS_SELECTOR, "footer button, button"):
                t = normalize_text(btn.text or btn.get_attribute("aria-label") or "")
                if any(normalize_text(x) == t or normalize_text(x) in t for x in sel.NEXT_BUTTON_TEXTS):
                    return True
                if any(normalize_text(x) in t for x in sel.SUBMIT_BUTTON_TEXTS):
                    return True
            return False
        except Exception:
            return False

    def _dump_modal_debug(self, tag: str) -> None:
        try:
            dump_dir = project_path("crash_dumps")
            dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            job_id = self._current_job.job_id if self._current_job else "unknown"
            base = dump_dir / f"{tag}_{job_id}_{stamp}"
            self.driver.save_screenshot(str(base.with_suffix(".png")))
            base.with_suffix(".html").write_text(self.driver.page_source, encoding="utf-8")
            # Log a short DOM hint
            dialogs = self.driver.find_elements(
                By.CSS_SELECTOR, "[role='dialog'], .artdeco-modal, .jobs-easy-apply-modal"
            )
            hints = []
            for d in dialogs[:5]:
                hints.append(
                    f"displayed={self._el_displayed(d)} class={(d.get_attribute('class') or '')[:80]}"
                )
            logger.info("Modal debug saved %s dialogs=%s", base.name, hints)
        except Exception as exc:
            logger.debug("Modal debug dump failed: %s", exc)

    def _step_fingerprint(self) -> str:
        modal = self._modal_root()
        if modal is None:
            return ""
        try:
            text = normalize_text(modal.text)[:400]
            inputs = modal.find_elements(
                By.CSS_SELECTOR, "input:not([type='hidden']), textarea, select"
            )
            ids = ",".join(
                sorted(
                    (i.get_attribute("id") or i.get_attribute("name") or "")[:40]
                    for i in inputs[:20]
                )
            )
            return f"{text}|{ids}|{len(inputs)}"
        except Exception:
            return str(time.time())

    def _wait_for_step_change(self, before: str, timeout: float = STEP_CHANGE_SECONDS) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._validation_errors():
                return False
            after = self._step_fingerprint()
            if after and after != before:
                short_delay(400)
                return True
            if not self._modal_visible():
                return True
            short_delay(200)
        return False

    def _safe_click(self, el) -> None:
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
            short_delay(150)
            el.click()
        except ElementClickInterceptedException:
            self.driver.execute_script("arguments[0].click();", el)
        except Exception:
            self.driver.execute_script("arguments[0].click();", el)

    @staticmethod
    def _el_displayed(el) -> bool:
        try:
            return el.is_displayed()
        except Exception:
            return False

    def _click_next(self) -> bool:
        # Location typeahead dropdown can intercept Next — commit first option first
        self._ensure_typeahead_committed()
        return self._click_button_by_texts(sel.NEXT_BUTTON_TEXTS)

    def _click_submit(self) -> bool:
        self._ensure_typeahead_committed()
        return self._click_button_by_texts(sel.SUBMIT_BUTTON_TEXTS)

    def _click_button_by_texts(self, texts: tuple[str, ...]) -> bool:
        """Click Next/Submit inside the Easy Apply dialog — never carousel 'Next'."""
        root = self._modal_root() or self.driver
        # Prefer footer actions (SDUI puts Next in <footer>)
        button_groups = [
            root.find_elements(By.CSS_SELECTOR, "footer button"),
            root.find_elements(By.CSS_SELECTOR, "button"),
        ]
        seen = set()
        for buttons in button_groups:
            for btn in buttons:
                try:
                    id_key = btn.id
                    if id_key in seen:
                        continue
                    seen.add(id_key)
                except Exception:
                    pass
                # Skip job-page carousel / unrelated Next
                testid = (btn.get_attribute("data-testid") or "").lower()
                if "carousel" in testid:
                    continue
                visible_text = (btn.text or "").strip()
                aria = (btn.get_attribute("aria-label") or "").strip()
                t = normalize_text(f"{visible_text} {aria}")
                if not any(normalize_text(x) == normalize_text(visible_text) or normalize_text(x) in t for x in texts):
                    continue
                # Prefer exact footer label match (e.g. button text == "Next")
                try:
                    if not btn.is_enabled():
                        continue
                    # displayed check can flake on <dialog>; try click anyway if in modal
                    logger.info(
                        "Clicking form action: %r",
                        (visible_text or aria)[:60],
                    )
                    self._safe_click(btn)
                    short_delay(700)
                    return True
                except ElementClickInterceptedException:
                    self.driver.execute_script("arguments[0].click();", btn)
                    short_delay(700)
                    return True
                except Exception as exc:
                    logger.debug("Form action click failed: %s", exc)
                    try:
                        self.driver.execute_script("arguments[0].click();", btn)
                        short_delay(700)
                        return True
                    except Exception:
                        continue
        return False

    def _is_review_or_submit_step(self) -> bool:
        modal = self._modal_root()
        body = normalize_text((modal.text if modal is not None else "") or "")
        if not body:
            body = normalize_text(self.driver.find_element(By.TAG_NAME, "body").text)
        return any(
            k in body
            for k in (
                "review your application",
                "bewerbung prüfen",
                "bewerbung prufen",
                "submit application",
                "bewerbung absenden",
            )
        )

    def _uncheck_follow_company(self) -> None:
        if not self.config.gdpr_and_consents.uncheck_follow_company:
            return
        try:
            boxes = self.driver.find_elements(By.CSS_SELECTOR, sel.FOLLOW_COMPANY)
            for box in boxes:
                if box.is_selected():
                    self.driver.execute_script("arguments[0].click();", box)
        except Exception:
            pass

    def _confirm_applied(self) -> bool:
        short_delay(1000)
        body = normalize_text(self.driver.find_element(By.TAG_NAME, "body").text)
        if any(normalize_text(t) in body for t in sel.APPLIED_CONFIRM_TEXTS):
            self._dismiss_post_submit()
            return True
        modals = self.driver.find_elements(By.CSS_SELECTOR, sel.EASY_APPLY_MODAL)
        errors = self._validation_errors()
        if not modals and not errors:
            self._dismiss_post_submit()
            return True
        if modals and not any(m.is_displayed() for m in modals) and not errors:
            self._dismiss_post_submit()
            return True
        return False

    def _dismiss_post_submit(self) -> None:
        try:
            for btn in self.driver.find_elements(By.CSS_SELECTOR, sel.DISMISS_BUTTON):
                if btn.is_displayed():
                    btn.click()
                    short_delay(300)
                    break
        except Exception:
            pass

    # ----- fill -----

    def _form_sections(self) -> list:
        modal = self._modal_root()
        root = modal if modal is not None else self.driver
        sections = root.find_elements(By.CSS_SELECTOR, sel.FORM_SECTIONS)
        if sections:
            return [s for s in sections if self._el_displayed(s)]
        # SDUI: no .fb-dash-form-element — each label[for] parent is a field group
        sdui_sections = []
        for label in root.find_elements(By.CSS_SELECTOR, "label[for]"):
            try:
                if not (label.text or "").strip():
                    continue
                # Walk up to a small field wrapper containing the control
                wrapper = label.find_element(
                    By.XPATH,
                    "./ancestor::div[.//input or .//select or .//textarea][1]",
                )
                if wrapper not in sdui_sections:
                    sdui_sections.append(wrapper)
            except Exception:
                continue
        if sdui_sections:
            return sdui_sections
        return [
            s
            for s in root.find_elements(
                By.CSS_SELECTOR, ".jobs-easy-apply-modal .fb-dash-form-element"
            )
            if self._el_displayed(s)
        ]

    def _fill_current_page(self) -> None:
        sections = self._form_sections()
        logger.info("Filling Easy Apply page with %s form sections", len(sections))
        for section in sections:
            try:
                self._fill_section(section)
            except Exception as exc:
                logger.debug("Section fill error: %s", exc)
        # SDUI: radios/checkboxes are often outside label[for] sections
        self._fill_sdui_radio_groups()
        self._fill_sdui_checkboxes()
        # SDUI safety net: fill any remaining empty required controls in dialog
        self._fill_sdui_required_controls()

    def _fill_sdui_radio_groups(self) -> None:
        """Select Yes/No (etc.) in LinkedIn SDUI radiogroups.

        Markup (from live dump):
          <fieldset role="radiogroup">
            <div role="radio" aria-label="QUESTION..." aria-checked="false">
              <input type="radio" id="...">  <!-- label[for] is empty -->
              <p>Yes</p>                     <!-- real option text -->
            </div>
            <div role="radio" ...><p>No</p></div>
          </fieldset>
        """
        modal = self._modal_root()
        if modal is None:
            return
        groups = modal.find_elements(By.CSS_SELECTOR, sel.RADIO_GROUP)
        if not groups:
            # Orphan role=radio nodes — group by shared aria-label (question)
            options = modal.find_elements(By.CSS_SELECTOR, sel.RADIO_OPTION)
            by_q: dict[str, list] = {}
            for o in options:
                key = (o.get_attribute("aria-label") or "").strip() or "_default"
                by_q.setdefault(key, []).append(o)
            for opts in by_q.values():
                if len(opts) >= 2:
                    self._fill_radios_from_options(opts)
            return
        for group in groups:
            try:
                options_els = group.find_elements(By.CSS_SELECTOR, sel.RADIO_OPTION)
                if not options_els:
                    # Fallback: plain input[type=radio] inside fieldset
                    inputs = group.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                    if inputs:
                        self._fill_plain_radio_inputs(group, inputs)
                    continue
                self._fill_radios_from_options(options_els)
            except Exception as exc:
                logger.debug("SDUI radio group fill failed: %s", exc)

    def _fill_radios_from_options(self, options_els: list) -> None:
        """Pick and click one role=radio option; skip if already checked."""
        if not options_els:
            return
        # Already answered?
        for el in options_els:
            checked = (el.get_attribute("aria-checked") or "").lower()
            if checked == "true":
                return
            try:
                inp = el.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if inp and inp[0].is_selected():
                    return
            except Exception:
                pass

        question = (
            (options_els[0].get_attribute("aria-label") or "").strip()
            or self._question_near_element(options_els[0])
            or "Please select an option"
        )
        labels: list[str] = []
        for el in options_els:
            labels.append(self._radio_option_text(el))

        # Drop empty labels
        paired = [(el, lab) for el, lab in zip(options_els, labels) if lab]
        if not paired:
            logger.warning("Radio options had no readable labels for: %s", question[:80])
            return
        options_els = [p[0] for p in paired]
        labels = [p[1] for p in paired]

        preferred = self._deterministic_answer(question)
        choice = self.intent.pick(question, labels, preferred=preferred)
        logger.info(
            "Radio question '%s' options=%s -> %s",
            question[:90],
            labels,
            choice,
        )
        if self._click_sdui_radio_option(options_els, labels, choice):
            return
        # Last resort: click first option element
        try:
            self._safe_click(options_els[0])
        except Exception:
            self.driver.execute_script("arguments[0].click();", options_els[0])

    def _radio_option_text(self, role_radio_el) -> str:
        """Visible Yes/No text living under role=radio (not the empty <label>)."""
        try:
            for p in role_radio_el.find_elements(By.CSS_SELECTOR, "p, span"):
                t = (p.text or "").strip()
                if t and len(t) < 80:
                    return t
            # Whole node text minus long question echo
            full = (role_radio_el.text or "").strip()
            if full:
                # Prefer last short line
                lines = [ln.strip() for ln in full.split("\n") if ln.strip()]
                for ln in reversed(lines):
                    if len(ln) < 80:
                        return ln
            aria = (role_radio_el.get_attribute("aria-label") or "").strip()
            # aria-label is usually the QUESTION, not the option — skip if long
            if aria and len(aria) < 40:
                return aria
        except Exception:
            pass
        return ""

    def _click_sdui_radio_option(
        self, options_els: list, labels: list[str], choice: str
    ) -> bool:
        choice_n = normalize_text(choice)
        for el, lab in zip(options_els, labels):
            if normalize_text(lab) == choice_n or choice_n in normalize_text(lab):
                try:
                    self._safe_click(el)
                except Exception:
                    self.driver.execute_script("arguments[0].click();", el)
                short_delay(250)
                # Also click inner input if still unchecked
                try:
                    if (el.get_attribute("aria-checked") or "").lower() != "true":
                        inp = el.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                        if inp:
                            self.driver.execute_script("arguments[0].click();", inp[0])
                            short_delay(150)
                except Exception:
                    pass
                logger.info("Selected radio option: %s", lab)
                return True
        return False

    def _fill_plain_radio_inputs(self, root, inputs: list) -> None:
        if any(r.is_selected() for r in inputs):
            return
        question = self._question_near_element(inputs[0]) or "Please select"
        options = []
        for r in inputs:
            opt = ""
            # Sibling / parent visible text
            try:
                opt = self.driver.execute_script(
                    """
                    const r = arguments[0];
                    const wrap = r.closest('[role="radio"]') || r.parentElement;
                    if (!wrap) return '';
                    const p = wrap.querySelector('p, span');
                    return (p && p.textContent || wrap.textContent || '').trim();
                    """,
                    r,
                )
            except Exception:
                opt = ""
            if not opt:
                opt = (r.get_attribute("value") or "").strip()
            options.append(opt.split("\n")[-1].strip() if opt else "")
        preferred = self._deterministic_answer(question)
        choice = self.intent.pick(question, [o for o in options if o], preferred=preferred)
        logger.info("Plain radio '%s' -> %s", question[:80], choice)
        for r, opt in zip(inputs, options):
            if normalize_text(opt) == normalize_text(choice) or normalize_text(choice) in normalize_text(opt):
                # Prefer clicking role=radio wrapper
                try:
                    wrap = r.find_element(By.XPATH, './ancestor::*[@role="radio"][1]')
                    self._safe_click(wrap)
                except Exception:
                    self.driver.execute_script("arguments[0].click();", r)
                short_delay(200)
                return

    def _question_near_element(self, el) -> str:
        """Preceding paragraph / legend text for SDUI questions."""
        try:
            text = self.driver.execute_script(
                """
                const el = arguments[0];
                let node = el.closest('fieldset') || el.parentElement;
                for (let i = 0; i < 6 && node; i++) {
                  const prev = node.previousElementSibling;
                  if (prev) {
                    const t = (prev.textContent || '').trim();
                    if (t && t.length > 3 && t.length < 500
                        && !/^(yes|no|ja|nein)$/i.test(t)) {
                      return t;
                    }
                  }
                  // look for p before radios inside same parent
                  const ps = node.parentElement
                    ? node.parentElement.querySelectorAll('p') : [];
                  for (const p of ps) {
                    const t = (p.textContent || '').trim();
                    if (t && t.length > 10 && t.length < 500
                        && !/required/i.test(t)
                        && !/^(yes|no|ja|nein)$/i.test(t)) {
                      return t;
                    }
                  }
                  node = node.parentElement;
                }
                return '';
                """,
                el,
            )
            return (text or "").strip()
        except Exception:
            return ""

    def _fill_sdui_checkboxes(self) -> None:
        """Check consent / required checkboxes inside the Easy Apply dialog."""
        modal = self._modal_root()
        if modal is None:
            return
        try:
            boxes = modal.find_elements(By.CSS_SELECTOR, sel.CHECKBOX_INPUT)
            for cb in boxes:
                try:
                    if cb.is_selected():
                        continue
                    # Resolve nearby label / text
                    label = ""
                    cid = cb.get_attribute("id") or ""
                    if cid:
                        labs = modal.find_elements(By.CSS_SELECTOR, f'label[for="{cid}"]')
                        if labs:
                            label = (labs[0].text or "").strip()
                    if not label:
                        label = self._question_near_element(cb) or (
                            cb.get_attribute("aria-label") or ""
                        )
                    low = normalize_text(label)
                    # Always tick GDPR / privacy / terms; also tick if marked required
                    must = self._is_consent_label(label) or self._input_aria_required(cb)
                    # Skip "follow company" style boxes
                    if "follow" in low:
                        continue
                    if must:
                        self._click_checkbox_via_label(modal, cb)
                        logger.info("Checked checkbox: %s", (label or cid)[:80])
                except Exception as exc:
                    logger.debug("Checkbox fill error: %s", exc)
        except Exception as exc:
            logger.debug("SDUI checkbox scan failed: %s", exc)

    def _fill_sdui_required_controls(self) -> None:
        """Fill empty / invalid text inputs and selects in the open dialog.

        Not limited to [required] — SDUI years fields often omit the attribute
        but show a 0/20 counter and a red star in the label.
        """
        modal = self._modal_root()
        if modal is None:
            return
        try:
            selects = modal.find_elements(By.CSS_SELECTOR, "select")
            for el in selects:
                try:
                    if not self._el_displayed(el):
                        continue
                    select = Select(el)
                    try:
                        cur = select.first_selected_option.text.strip()
                    except Exception:
                        cur = ""
                    if cur and normalize_text(cur) not in ("", "select", "select an option"):
                        continue
                    label = self._label_for_control(modal, el)
                    options = [o.text for o in select.options if o.text.strip()]
                    preferred = self._deterministic_answer(label)
                    choice = self.intent.pick(label, options, preferred=preferred)
                    try:
                        select.select_by_visible_text(choice)
                    except Exception:
                        for o in select.options:
                            if normalize_text(choice) in normalize_text(o.text):
                                select.select_by_visible_text(o.text)
                                break
                    logger.info("SDUI select '%s' -> %s", label[:60], choice)
                except Exception as exc:
                    logger.debug("SDUI select fill error: %s", exc)

            controls = modal.find_elements(
                By.CSS_SELECTOR,
                "input[type='text'], input[type='number'], input[type='tel'], "
                "input:not([type]), textarea",
            )
            for el in controls:
                try:
                    if not self._el_displayed(el):
                        continue
                    label = self._label_for_control(modal, el) or self._question_near_element(el)
                    low = fold_accents(label)
                    if any(k in low for k in ("city", "location", "standort", "ort")):
                        existing = (el.get_attribute("value") or "").strip()
                        if not existing:
                            self._fill_typeahead(el, self.config.profile.location_full)
                        else:
                            self._confirm_location_typeahead(el, existing)
                        continue
                    if any(k in low for k in ("cover letter", "anschreiben", "motivation")):
                        cl = build_cover_letter(
                            self.config,
                            position=(self._current_job.title if self._current_job else ""),
                            company=(self._current_job.company if self._current_job else ""),
                            location=(self._current_job.location if self._current_job else ""),
                            adapt=False,
                        )
                        if cl:
                            self._set_input(el, cl, label=label)
                        continue
                    if self._input_needs_refill(el, label):
                        self._fill_input_with_retries(el, label)
                except Exception as exc:
                    logger.debug("SDUI control fill error: %s", exc)
        except Exception as exc:
            logger.debug("SDUI required scan failed: %s", exc)

    def _input_needs_refill(self, el, label: str) -> bool:
        """True if empty, over maxlength, or years/salary field has wrong type of value."""
        val = (el.get_attribute("value") or "").strip()
        max_len = self._field_max_length(el)
        digits = self._field_wants_digits_only(el, label)
        low = normalize_text(label)
        phoneish = any(k in low for k in ("phone", "mobil", "telefon", "mobile number"))
        if not val:
            return True
        # Keep a plausible phone number; do not clear it to paste "+91"
        if phoneish and len(re.sub(r"\D", "", val)) >= 8:
            if "country" in low or "code" in low:
                return False
            # Only refill if it is just a dial code / too short national number
            national = self._national_phone_number()
            if national and re.sub(r"\D", "", val) in (national, "91" + national):
                return False
            if re.fullmatch(r"\+?\d{1,4}", val):
                return True
            return False
        if max_len and len(val) > max_len:
            return True
        if digits and not re.fullmatch(r"\d+", val):
            return True
        if self._is_years_question(label) and not re.fullmatch(r"\d+", val):
            return True
        return False

    def _label_for_control(self, root, el) -> str:
        cid = el.get_attribute("id") or ""
        if cid:
            labs = root.find_elements(By.CSS_SELECTOR, f'label[for="{cid}"]')
            if labs and (labs[0].text or "").strip():
                return labs[0].text.strip()
        for attr in ("aria-label", "placeholder", "name"):
            v = (el.get_attribute(attr) or "").strip()
            if v:
                return v
        return "application question"

    def _ensure_required_fields_filled(self) -> None:
        """Re-scan and fill empty required / unanswered interactive fields."""
        for attempt in range(3):
            pending = self._collect_unfilled_fields()
            if not pending:
                return
            logger.info(
                "Attempt %s: %s unfilled field(s) — %s",
                attempt + 1,
                len(pending),
                [p[:60] for p in pending[:5]],
            )
            self._fill_current_page()
            short_delay(400)

    def _collect_unfilled_fields(self) -> list[str]:
        pending: list[str] = []
        modal = self._modal_root()
        # SDUI radiogroups first (often missing from label[for] sections)
        if modal is not None:
            for group in modal.find_elements(By.CSS_SELECTOR, sel.RADIO_GROUP):
                opts = group.find_elements(By.CSS_SELECTOR, sel.RADIO_OPTION)
                if not opts:
                    continue
                answered = any(
                    (o.get_attribute("aria-checked") or "").lower() == "true" for o in opts
                )
                if not answered:
                    q = (opts[0].get_attribute("aria-label") or "radio group").strip()
                    pending.append(q[:120])
            for cb in modal.find_elements(By.CSS_SELECTOR, sel.CHECKBOX_INPUT):
                try:
                    if cb.is_selected():
                        continue
                    label = self._label_for_control(modal, cb) or self._question_near_element(cb)
                    if self._is_consent_label(label) or self._input_aria_required(cb):
                        if "follow" not in normalize_text(label):
                            pending.append(label or "checkbox")
                except Exception:
                    continue

        for section in self._form_sections():
            label = self._section_label(section)
            required = self._section_looks_required(section, label)
            # Radios
            radios = [
                r
                for r in section.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if self._el_displayed(r)
            ]
            if radios:
                if not any(r.is_selected() for r in radios):
                    pending.append(label or "radio group")
                continue
            # Checkbox consents
            checkboxes = [
                c
                for c in section.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
                if self._el_displayed(c)
            ]
            if checkboxes and self._is_consent_label(label):
                if any(not c.is_selected() for c in checkboxes):
                    pending.append(label or "consent checkbox")
                continue
            # Selects
            selects = section.find_elements(By.CSS_SELECTOR, "select")
            if selects and self._el_displayed(selects[0]):
                try:
                    select = Select(selects[0])
                    val = select.first_selected_option.text.strip() if select.options else ""
                    if (not val or normalize_text(val) in ("select an option", "select", "wählen", "auswählen")) and required:
                        pending.append(label or "select")
                except Exception:
                    if required:
                        pending.append(label or "select")
                continue
            # Custom dropdown
            triggers = section.find_elements(
                By.CSS_SELECTOR,
                "button.artdeco-dropdown__trigger, button[aria-haspopup='listbox'], "
                "div[role='combobox'], button[data-test-text-entity-list-form-component]",
            )
            text_inputs = [
                t
                for t in section.find_elements(
                    By.CSS_SELECTOR,
                    "input[type='text'], input:not([type]), textarea, input[type='number'], "
                    "input[type='tel'], input[type='email']",
                )
                if self._el_displayed(t)
            ]
            if triggers and not text_inputs:
                aria = (triggers[0].get_attribute("aria-expanded") or "").lower()
                btn_text = (triggers[0].text or "").strip()
                if required and (not btn_text or "select" in normalize_text(btn_text)):
                    pending.append(label or "dropdown")
                continue
            for el in text_inputs:
                if self._input_needs_refill(el, label) and (
                    required
                    or self._input_aria_required(el)
                    or self._is_years_question(label)
                    or self._field_max_length(el)
                ):
                    pending.append(label or el.get_attribute("name") or "text input")

        # SDUI: years / constrained fields may sit outside label[for] sections
        if modal is not None:
            for el in modal.find_elements(
                By.CSS_SELECTOR,
                "input[type='text'], input[type='number'], input:not([type]), textarea",
            ):
                try:
                    if not self._el_displayed(el):
                        continue
                    lab = self._label_for_control(modal, el) or self._question_near_element(el)
                    if self._input_needs_refill(el, lab) and (
                        self._is_years_question(lab)
                        or "*" in (lab or "")
                        or self._input_aria_required(el)
                    ):
                        key = lab[:120] if lab else "text input"
                        if key not in pending:
                            pending.append(key)
                except Exception:
                    continue
        return pending

    def _section_looks_required(self, section, label: str) -> bool:
        blob = normalize_text((label or "") + " " + (section.text or "")[:200])
        if any(m in blob for m in ("*", "required", "pflicht", "mandatory")):
            return True
        for el in section.find_elements(By.CSS_SELECTOR, "input, textarea, select"):
            if self._input_aria_required(el):
                return True
        return False

    @staticmethod
    def _input_aria_required(el) -> bool:
        try:
            if (el.get_attribute("aria-required") or "").lower() == "true":
                return True
            required = el.get_attribute("required")
            if required is not None and str(required).lower() != "false":
                return True
        except Exception:
            pass
        return False

    def _section_label(self, section) -> str:
        for css in (
            "label",
            "span.fb-dash-form-element__label",
            "legend",
            ".artdeco-text-input--label",
            ".fb-dash-form-element__label",
            "span[aria-hidden='true']",
            ".t-14",
        ):
            els = section.find_elements(By.CSS_SELECTOR, css)
            for el in els:
                text = (el.text or "").strip()
                if text and len(text) < 300:
                    return text
        for inp in section.find_elements(
            By.CSS_SELECTOR, "input, textarea, select, button, [role='combobox']"
        ):
            for attr in ("aria-label", "placeholder", "name", "id"):
                v = (inp.get_attribute(attr) or "").strip()
                if v and normalize_text(v) not in ("on", "off", "true", "false"):
                    # Skip pure technical ids
                    if attr == "id" and re.match(r"^[\w-]+$", v) and " " not in v and len(v) > 40:
                        continue
                    if attr in ("aria-label", "placeholder") or " " in v or "-" in v:
                        return v
        text = (section.text or "").strip()
        if text:
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            for ln in lines:
                if any(m in ln.lower() for m in ("*", "required", "pflicht")):
                    return ln.replace("*", "").strip()
            return lines[0]
        return ""

    @staticmethod
    def _is_consent_label(label: str) -> bool:
        low = normalize_text(label)
        return any(
            k in low
            for k in (
                "privacy",
                "gdpr",
                "datenschutz",
                "consent",
                "terms",
                "talent",
                "einwilligung",
                "agree",
                "data storage",
                "speicherung",
            )
        )

    def _fill_section(self, section) -> None:
        label = self._section_label(section)
        low = normalize_text(label)

        # File uploads
        file_inputs = section.find_elements(By.CSS_SELECTOR, sel.FILE_INPUT)
        if file_inputs:
            self._handle_upload(section, label or "resume", file_inputs[0])
            return

        # Checkboxes (GDPR / consents)
        checkboxes = [
            c
            for c in section.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
            if self._el_displayed(c)
        ]
        if checkboxes and self._is_consent_label(label or section.text or ""):
            for cb in checkboxes:
                if not cb.is_selected():
                    self._click_checkbox_via_label(section, cb)
            return

        # Radio groups — prefer SDUI role=radio wrappers inside this section
        role_radios = section.find_elements(By.CSS_SELECTOR, sel.RADIO_OPTION)
        if role_radios:
            self._fill_radios_from_options(role_radios)
            return
        radios = [
            r
            for r in section.find_elements(By.CSS_SELECTOR, "input[type='radio']")
            if self._el_displayed(r)
        ]
        if radios:
            self._fill_plain_radio_inputs(section, radios)
            return

        # Native select
        selects = [
            s for s in section.find_elements(By.CSS_SELECTOR, "select") if self._el_displayed(s)
        ]
        if selects:
            select = Select(selects[0])
            current = ""
            try:
                current = select.first_selected_option.text.strip()
            except Exception:
                pass
            if current and normalize_text(current) not in (
                "select an option",
                "select",
                "wählen",
                "auswählen",
                "choose",
            ):
                return
            options = [o.text for o in select.options if o.text.strip()]
            q = label or "Please select"
            preferred = self._deterministic_answer(q)
            choice = self.intent.pick(q, options, preferred=preferred)
            try:
                select.select_by_visible_text(choice)
            except Exception:
                for o in select.options:
                    if normalize_text(choice) in normalize_text(o.text):
                        select.select_by_visible_text(o.text)
                        break
            return

        # Artdeco / custom dropdown button
        triggers = [
            t
            for t in section.find_elements(
                By.CSS_SELECTOR,
                "button.artdeco-dropdown__trigger, button[aria-haspopup='listbox'], "
                "div[role='combobox'], button[data-test-text-entity-list-form-component]",
            )
            if self._el_displayed(t)
        ]
        text_inputs = [
            t
            for t in section.find_elements(
                By.CSS_SELECTOR,
                "input[type='text'], input:not([type]), textarea, input[type='number'], "
                "input[type='tel'], input[type='email']",
            )
            if self._el_displayed(t)
        ]

        # Typeahead / location — must select a dropdown option or Next stays stuck
        if any(k in low for k in ("city", "location", "standort", "ort", "typeahead")):
            if text_inputs:
                existing = (text_inputs[0].get_attribute("value") or "").strip()
                if not existing:
                    self._fill_typeahead(text_inputs[0], self.config.profile.location_full)
                else:
                    self._confirm_location_typeahead(
                        text_inputs[0], existing or self.config.profile.location_full
                    )
                return

        if triggers and not text_inputs:
            btn_text = (triggers[0].text or "").strip()
            if btn_text and "select" not in normalize_text(btn_text):
                return
            self._fill_custom_dropdown(section, triggers[0], label or "dropdown")
            return

        if text_inputs:
            el = text_inputs[0]
            q = label or el.get_attribute("aria-label") or el.get_attribute("placeholder") or ""
            # Cover letter: instant template with this job's company/title — no Ollama
            if any(k in low for k in ("cover letter", "anschreiben", "motivation")):
                cl = build_cover_letter(
                    self.config,
                    position=(self._current_job.title if self._current_job else ""),
                    company=(self._current_job.company if self._current_job else ""),
                    location=(self._current_job.location if self._current_job else ""),
                    adapt=False,
                )
                if cl:
                    self._set_input(el, cl, label=q)
                    logger.info(
                        "Filled cover letter from template (%s @ %s, %s chars)",
                        (self._current_job.company if self._current_job else "")[:40],
                        (self._current_job.title if self._current_job else "")[:40],
                        len(cl),
                    )
                return
            existing = (el.get_attribute("value") or "").strip()
            if existing and not self._input_needs_refill(el, q):
                return
            if not self._fill_input_with_retries(el, q):
                logger.warning("No answer for field '%s'", q[:100])
                append_unanswered(
                    q,
                    "empty_answer",
                    job_id=self._current_job.job_id if self._current_job else "",
                )

    def _select_radio_choice(self, radios, options: list[str], choice: str) -> bool:
        choice_n = normalize_text(choice)
        if not choice_n:
            return False
        for r, opt in zip(radios, options):
            opt_n = normalize_text(opt)
            if opt_n == choice_n or choice_n in opt_n or opt_n in choice_n:
                self.driver.execute_script("arguments[0].click();", r)
                short_delay(150)
                return True
        return False

    def _handle_upload(self, section, label: str, file_input) -> None:
        low = normalize_text(label)
        # Skip if a file already attached (sibling text often shows filename)
        try:
            parent_text = normalize_text(section.text)
            if any(ext in parent_text for ext in (".pdf", ".doc", ".docx")) and "upload" not in low:
                # Likely already has a resume; still upload if empty input
                pass
        except Exception:
            pass
        if any(k in low for k in ("cover letter", "anschreiben")):
            pdf = self.config.cover_letter_pdf_path()
            if pdf and pdf.exists():
                file_input.send_keys(str(pdf.resolve()))
            return
        if any(k in low for k in ("resume", "cv", "lebenslauf", "curriculum")) or not low:
            path = self.config.resume_path()
            if path.exists():
                try:
                    file_input.send_keys(str(path.resolve()))
                    logger.info("Uploaded resume for '%s'", label[:60])
                except Exception as exc:
                    logger.warning("Resume upload failed: %s", exc)
            else:
                logger.warning("Resume not found at %s", path)

    def _click_checkbox_via_label(self, section, checkbox) -> None:
        cid = checkbox.get_attribute("id")
        if cid:
            labs = section.find_elements(By.CSS_SELECTOR, f"label[for='{cid}']")
            if labs:
                labs[0].click()
                return
        self.driver.execute_script("arguments[0].click();", checkbox)

    def _fill_typeahead(self, el, value: str) -> None:
        """Type location and commit the first dropdown suggestion (required before Next)."""
        self._set_input(el, value, label="Enter city or location")
        short_delay(700)
        if self._select_first_typeahead_option():
            return
        # Retry with city-only to trigger suggestions
        try:
            el.send_keys(Keys.CONTROL + "a")
            el.send_keys(Keys.BACKSPACE)
            el.send_keys(self.config.profile.current_city)
            short_delay(800)
        except Exception:
            pass
        if self._select_first_typeahead_option():
            return
        # Keyboard fallback
        try:
            el.send_keys(Keys.ARROW_DOWN)
            short_delay(200)
            el.send_keys(Keys.ENTER)
            short_delay(300)
        except Exception:
            pass

    def _confirm_location_typeahead(self, el, value: str) -> None:
        """If a suggestion list is open (or value not committed), pick the first option."""
        if self._select_first_typeahead_option():
            return
        try:
            el.click()
            short_delay(350)
            # Nudge to open suggestions without wiping a good value
            el.send_keys(Keys.ARROW_DOWN)
            short_delay(400)
            if self._select_first_typeahead_option():
                return
            # Last resort: re-type full location and select
            self._fill_typeahead(el, value or self.config.profile.location_full)
        except Exception as exc:
            logger.debug("confirm location typeahead: %s", exc)

    def _select_first_typeahead_option(self) -> bool:
        """Click the first visible typeahead / listbox option. Returns True if selected."""
        modal = self._modal_root()
        roots = [modal, self.driver] if modal is not None else [self.driver]
        css_list = (
            "[role='listbox'] [role='option']",
            ".basic-typeahead__triggered-content [role='option']",
            ".basic-typeahead__triggered-content li",
            ".search-basic-typeahead__triggered-content [role='option']",
            "[data-testid='typeahead-result']",
            "ul[role='listbox'] li",
        )
        for root in roots:
            if root is None:
                continue
            for css in css_list:
                try:
                    opts = root.find_elements(By.CSS_SELECTOR, css)
                except Exception:
                    continue
                for opt in opts:
                    try:
                        if not self._el_displayed(opt):
                            continue
                        text = (opt.text or "").strip()
                        if not text:
                            continue
                        self._safe_click(opt)
                        short_delay(350)
                        logger.info("Selected location typeahead option: %s", text[:80])
                        return True
                    except Exception:
                        continue
        return False

    def _ensure_typeahead_committed(self) -> None:
        """Before Next/Review: if a location dropdown is open, select the first option."""
        if self._select_first_typeahead_option():
            return
        # ESC closes an empty sticky dropdown so Next can be clicked
        try:
            open_list = False
            modal = self._modal_root() or self.driver
            for css in ("[role='listbox']", ".basic-typeahead__triggered-content"):
                for el in modal.find_elements(By.CSS_SELECTOR, css):
                    if self._el_displayed(el):
                        open_list = True
                        break
                if open_list:
                    break
            if open_list:
                from selenium.webdriver.common.action_chains import ActionChains

                ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                short_delay(200)
        except Exception:
            pass

    def _fill_custom_dropdown(self, section, trigger, label: str) -> None:
        try:
            self._safe_click(trigger)
            short_delay(500)
            options_els = self.driver.find_elements(
                By.CSS_SELECTOR,
                ".artdeco-dropdown__content li, [role='option'], "
                ".fb-dash-form-element [role='option'], ul.list-style-none li",
            )
            options = [o.text.strip() for o in options_els if o.text.strip()]
            if not options:
                logger.debug("No dropdown options for '%s'", label[:80])
                return
            preferred = self._deterministic_answer(label)
            choice = self.intent.pick(label, options, preferred=preferred)
            for o in options_els:
                if normalize_text(o.text) == normalize_text(choice) or normalize_text(
                    choice
                ) in normalize_text(o.text):
                    self._safe_click(o)
                    short_delay(200)
                    logger.debug("Dropdown '%s' -> %s", label[:60], choice)
                    return
            # Fallback first real option (skip placeholder)
            for o in options_els:
                t = normalize_text(o.text)
                if t and "select" not in t:
                    self._safe_click(o)
                    return
        except Exception as exc:
            logger.debug("Custom dropdown failed: %s", exc)

    def _debug_fill(self, msg: str, *args) -> None:
        """Verbose fill tracing when EASYAPPLY_DEBUG_FILL=true."""
        try:
            if getattr(self.ollama.env, "easyapply_debug_fill", False):
                logger.info("[FILL] " + msg, *args)
        except Exception:
            pass

    @staticmethod
    def _is_char_counter_text(text: str) -> bool:
        """True for SDUI counters like '6/100' or '6 of 100 characters' (not errors)."""
        t = (text or "").strip()
        if not t or len(t) > 80:
            return False
        # Multi-line blobs that are only counters / labels with counters
        lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
        if not lines:
            return False
        counterish = 0
        for ln in lines:
            ln_n = normalize_text(ln)
            if re.fullmatch(r"\d+\s*/\s*\d+", ln_n):
                counterish += 1
                continue
            if re.fullmatch(r"\d+\s+of\s+\d+\s+characters?", ln_n):
                counterish += 1
                continue
            # Label lines like "First name*" next to counters — whole blob still not an error
            if ln.endswith("*") or ln_n in ("first name", "last name", "email address"):
                counterish += 1
                continue
            return False
        return counterish > 0

    def _is_years_question(self, label: str) -> bool:
        q = fold_accents(label)
        if any(
            k in q
            for k in (
                "years of experience",
                "how many years",
                "years of work experience",
                "work experience do you have",
                "berufserfahrung",
                "jahre erfahrung",
                "wie viele jahre",
                "jahre hast",
                "anos de experiencia",
                "anos experiencia",
                "cuantos anos",
                "cuanto anos",
                "cuantas anos",
                "experiencia tienes con",
                "jaar werkervaring",
                "hoeveel jaar",
                "jaren ervaring",
                "werkervaring hebt u",
                "werkervaring heb u",
            )
        ):
            return True
        # Spanish / EN: "años … experiencia …" / "years … with …" even with typos
        if re.search(r"\b(anos|years|jahre|jaar)\b", q) and any(
            k in q for k in ("experiencia", "experience", "erfahrung", "werkervaring")
        ):
            return True
        return False

    def _is_count_question(self, label: str) -> bool:
        """How-many / cuantos proyectos — expects a whole number, not a sentence."""
        q = fold_accents(label)
        if re.search(r"\b(cuant[oa]s?|how many|wie viele|hoeveel|number of|anzahl)\b", q):
            return True
        if "proyectos" in q or "projects" in q:
            return True
        return False

    def _is_identity_text_field(self, label: str) -> bool:
        q = fold_accents(label)
        return any(
            k in q
            for k in (
                "first name",
                "last name",
                "vorname",
                "nachname",
                "email",
                "e-mail",
                "phone",
                "mobil",
                "telefon",
                "city",
                "location",
                "standort",
                "linkedin",
                "github",
                "website",
                "url",
                "http",
                "cover letter",
                "anschreiben",
                "address",
                "street",
            )
        )

    def _is_start_or_notice_question(self, label: str) -> bool:
        """Earliest start / notice / Startdatum — never treat as experience years."""
        q = fold_accents(label)
        return any(
            k in q
            for k in (
                "startdatum",
                "starttermin",
                "start date",
                "earliest start",
                "fruehestmoglich",
                "fruhstmoglich",
                "eintritt",
                "verfugbar",
                "available from",
                "ab wann",
                "notice period",
                "notice",
                "kundigung",
                "kuendigung",
                "kundigungsfrist",
                "wann ware ihr",
                "wann ware",
            )
        ) or ("start" in q and any(k in q for k in ("wann", "when", "datum", "date")))

    def _is_date_field(self, el, label: str = "") -> bool:
        """True for date text boxes (optionally with a calendar button beside them)."""
        if self._is_start_or_notice_question(label):
            q = fold_accents(label)
            if any(k in q for k in ("date", "datum", "startdatum", "starttermin", "available from", "ab wann", "fruehestmoglich", "wann ware")):
                return True
        try:
            typ = (el.get_attribute("type") or "").lower()
            if typ == "date":
                return True
            ph = fold_accents(el.get_attribute("placeholder") or "")
            if any(k in ph for k in ("mm/dd", "dd/mm", "yyyy", "datum", "date")):
                return True
            # Sibling / nearby picker button — we still fill the text input, never click it
            has_picker = self.driver.execute_script(
                """
                const el = arguments[0];
                const root = el.closest('label, div, fieldset, section') || el.parentElement;
                if (!root) return false;
                const btns = root.querySelectorAll('button, [role="button"]');
                for (const b of btns) {
                  const a = ((b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '')).toLowerCase();
                  if (/calendar|date|datum|datepicker|choose date|pick/i.test(a)) return true;
                }
                return false;
                """,
                el,
            )
            if has_picker and self._is_start_or_notice_question(label):
                return True
        except Exception:
            pass
        return False

    def _start_date_answer(self, el, label: str) -> str:
        """MM/DD/YYYY for text date boxes; ISO for native type=date. Never open the picker."""
        days = self.config.eu_work_authorization.notice_period_days
        try:
            typ = (el.get_attribute("type") or "").lower()
            if typ == "date":
                return earliest_start_date_iso(days)
        except Exception:
            pass
        # Prefer deterministic compact value from notice helper
        return notice_period_value(
            label,
            days,
            self.config.eu_work_authorization.notice_period_weeks,
            self.config.eu_work_authorization.notice_period_months,
        ) or earliest_start_date_mmddyyyy(days)

    def _years_answer_for_question(self, label: str) -> str:
        """Return skill years as a plain integer string (never a sentence)."""
        q = fold_accents(label)
        # Longest skill name first so "spring boot" beats "spring"
        skills = sorted(
            self.config.experience.skill_years.items(),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )
        aliases = {
            "c++": ["c++", "cpp", "c plus plus"],
            "c#": ["c#", "csharp", "c sharp"],
            "spring boot": ["spring boot", "springboot"],
            "asp.net": ["asp.net", "aspnet", "asp net"],
            "typescript": ["typescript", "type script"],
            "pytorch": ["pytorch", "py torch"],
            "deep learning": ["deep learning", "aprendizaje profundo", "deeplearning"],
            "python": ["python"],
            "java": ["java"],
            "gcp": ["gcp", "google cloud", "google cloud platform"],
            "firebase": ["firebase", "firestore"],
            "git": ["git", "github", "gitlab"],
            "node.js": ["node.js", "nodejs", "node js"],
            "ci/cd": ["ci/cd", "cicd", "continuous integration", "jenkins", "github actions"],
        }
        for skill, years in skills:
            sk = fold_accents(skill)
            if sk and sk in q:
                return str(years)
        for skill, keys in aliases.items():
            if any(k in q for k in keys):
                # Prefer config value when present
                for cfg_skill, years in skills:
                    cfg_n = fold_accents(cfg_skill)
                    if cfg_n == skill or skill in cfg_n or fold_accents(skill) == cfg_n:
                        return str(years)
                # Unknown specific tech → 0 (honest) rather than a long Ollama essay
                if skill in ("c++", "asp.net", "pytorch", "typescript", "c#"):
                    # TypeScript/React overlap; C#/Java not listed
                    if skill == "typescript":
                        return str(self.config.experience.skill_years.get("React.js", 2))
                    if skill == "c#":
                        return str(self.config.experience.skill_years.get("Java", 3))
                    return "0"
                if skill == "deep learning":
                    return str(self.config.experience.skill_years.get("Generative AI", 2))
                if skill == "python":
                    return str(self.config.experience.skill_years.get("Python", 8))
                if skill == "java":
                    return str(self.config.experience.skill_years.get("Java", 5))
                if skill == "spring boot":
                    return str(self.config.experience.skill_years.get("Spring Boot", 4))
                if skill == "gcp":
                    return str(self.config.experience.skill_years.get("GCP", 3))
                if skill == "firebase":
                    return str(self.config.experience.skill_years.get("Firebase", 0) or 0)
                if skill == "git":
                    return str(
                        self.config.experience.skill_years.get("Git", 0)
                        or self.config.experience.total_experience_years
                    )
                if skill == "node.js":
                    return str(self.config.experience.skill_years.get("Node.js", 0) or 0)
                if skill == "ci/cd":
                    return str(
                        self.config.experience.skill_years.get("CI/CD", 0)
                        or self.config.experience.skill_years.get("DevOps", 0)
                        or 0
                    )
                return "0"
        # Skill-specific "years with X" and no match → 0 (honest), not total career years
        if re.search(
            r"\b(con|with|en)\b",
            fold_accents(label),
        ) and not any(
            k in fold_accents(label)
            for k in ("total", "overall", "work experience do you have", "experiencia laboral")
        ):
            return "0"
        return str(self.config.experience.total_experience_years)

    def _field_max_length(self, el) -> int | None:
        """Read maxlength / field-local char counters (e.g. 0/20 under the input).

        Never treat dialog progress like ``1/4 pages`` / ``2/5`` in the header as a
        character limit — that was truncating answers to 4–5 chars.
        """
        for attr in ("maxlength", "maxLength", "aria-maxlength", "aria-maxLength"):
            raw = el.get_attribute(attr)
            if raw and str(raw).isdigit():
                n = int(raw)
                if 0 < n < 100000:
                    self._debug_fill("maxlength attr=%s", n)
                    return n
        try:
            blob = self.driver.execute_script(
                """
                const el = arguments[0];
                const isProgress = (t) =>
                  /\\b(pages?|seiten?|pagina|pasos?|steps?|étapes?|etapes?)\\b/i.test(t || '');
                const inHeader = (node) => {
                  if (!node || !node.closest) return false;
                  return !!node.closest(
                    '#dialog-header, [data-testid="dialog-title"], [data-testid="dialog-header"]'
                  );
                };
                const parseMax = (t, allowBare) => {
                  if (!t) return null;
                  t = String(t).trim();
                  if (!t || t.length > 48 || isProgress(t)) return null;
                  let m = t.match(/(\\d+)\\s+of\\s+(\\d+)\\s+characters?/i);
                  if (m) return parseInt(m[2], 10);
                  m = t.match(/(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:characters?|chars?|zeichen)\\b/i);
                  if (m) return parseInt(m[2], 10);
                  m = t.match(/^(\\d+)\\s*\\/\\s*(\\d+)$/);
                  if (m && allowBare) {
                    const max = parseInt(m[2], 10);
                    if (max > 0 && max <= 10000) return max;
                  }
                  return null;
                };
                const ownText = (node) => {
                  // Prefer direct text so parent blobs like "Apply… 1/4 pages" are ignored
                  let s = '';
                  for (const n of node.childNodes || []) {
                    if (n.nodeType === 3) s += n.textContent || '';
                  }
                  s = s.trim();
                  return s || (node.textContent || '').trim();
                };
                // 1) Following siblings of the input (counter sits bottom-right of the box)
                let sib = el.nextElementSibling;
                for (let i = 0; i < 6 && sib; i++) {
                  if (!inHeader(sib)) {
                    const max = parseMax(ownText(sib), true) || parseMax((sib.textContent || '').trim(), true);
                    if (max) return max;
                    const nested = sib.querySelectorAll('span, p, small, div');
                    for (const c of nested) {
                      if (inHeader(c)) continue;
                      const maxN = parseMax(ownText(c), true);
                      if (maxN) return maxN;
                    }
                  }
                  sib = sib.nextElementSibling;
                }
                // 2) Walk a few parents; only accept counters AFTER the input in document order
                let node = el.parentElement;
                for (let depth = 0; depth < 4 && node; depth++) {
                  if (inHeader(node)) break;
                  if (node.tagName === 'DIALOG' || node.getAttribute('data-testid') === 'dialog') break;
                  // Too many controls ⇒ left the field group (risk of page progress)
                  const controls = node.querySelectorAll('input, textarea, select');
                  if (controls.length > 4) break;
                  const kids = node.querySelectorAll('span, p, small, label, div');
                  for (const c of kids) {
                    if (inHeader(c)) continue;
                    if (!(el.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
                    if (el.contains(c)) continue;
                    const t = ownText(c);
                    if (!t || t.length > 40) continue;
                    // Bare N/M only in tight field wrappers (depth <= 2)
                    const max = parseMax(t, depth <= 2);
                    if (max) return max;
                  }
                  node = node.parentElement;
                }
                return null;
                """,
                el,
            )
            if blob is not None:
                self._debug_fill("maxlength from counter=%s", int(blob))
                return int(blob)
        except Exception as exc:
            self._debug_fill("maxlength probe failed: %s", exc)
        return None

    def _field_wants_digits_only(self, el, label: str = "") -> bool:
        """True when the control only accepts a whole number.

        LinkedIn Easy Apply often shows ``0/20`` under year/count boxes. Those
        reject sentences with ``Invalid input`` — treat max≤20 as numeric unless
        the label is identity, start/notice/date, or salary-with-text elsewhere.
        """
        if self._is_identity_text_field(label) or self._is_start_or_notice_question(label):
            return False
        if self._is_date_field(el, label):
            return False
        if self._is_years_question(label) or self._is_count_question(label):
            return True
        try:
            typ = (el.get_attribute("type") or "").lower()
            if typ == "number":
                return True
            if typ == "date":
                return False
            mode = (el.get_attribute("inputmode") or "").lower()
            if mode in ("numeric", "decimal"):
                return True
            pattern = (el.get_attribute("pattern") or "").strip()
            digit_patterns = {
                r"\d+",
                r"\d*",
                r"[0-9]+",
                r"[0-9]*",
                "[0-9]+",
                "[0-9]*",
                "\\d+",
                "\\d*",
            }
            if pattern in digit_patterns:
                return True
        except Exception:
            pass
        q = fold_accents(label)
        if any(
            k in q
            for k in (
                "whole number",
                "enter a number",
                "only numbers",
                "numeric",
                "must be a number",
                "digits only",
                "integer",
            )
        ):
            return True
        # Counter under the box is 0/20 → number field (not dates / identity)
        max_len = self._field_max_length(el)
        if max_len is not None and max_len <= 20 and not self._is_identity_text_field(label):
            if self._is_start_or_notice_question(label):
                return False
            return True
        return False

    def _numeric_answer_for_field(self, el, label: str) -> str:
        """Whole-number answer for year/count/0–20 boxes (never a sentence)."""
        if self._is_identity_text_field(label):
            return ""
        if self._is_date_field(el, label) or self._is_start_or_notice_question(label):
            # Date boxes get MM/DD/YYYY; pure notice-duration get day count via helper
            return self._start_date_answer(el, label)
        if self._is_years_question(label) or self._is_count_question(label):
            return self._years_answer_for_question(label)
        low = fold_accents(label)
        if any(
            k in low
            for k in ("salary", "compensation", "gehalt", "ctc", "hourly", "annual")
        ) or re.search(r"\brate\b", low):
            return self._salary_answer(label, numeric=True)
        # Short experience boxes ("Cuál es tu experiencia con X") → skill years or 0
        return self._years_answer_for_question(label)

    def _shape_answer_for_field(self, el, label: str, answer: str) -> str:
        """Enforce digits-only + maxlength before typing into the control."""
        if answer is None:
            return ""
        out = sanitize_human_text(str(answer))
        digits = self._field_wants_digits_only(el, label)
        low = fold_accents(label)
        salaryish = any(
            k in low
            for k in (
                "salary",
                "compensation",
                "gehalt",
                "gehaltsvorstellungen",
                "ctc",
                "hourly",
                "annual",
                "vergutung",
                "verguetung",
            )
        ) or bool(re.search(r"\brate\b", low))
        yearsish = self._is_years_question(label) or self._is_count_question(label)
        if self._is_date_field(el, label) or self._is_start_or_notice_question(label):
            out = self._start_date_answer(el, label)
        elif yearsish or (digits and not self._is_identity_text_field(label)):
            out = self._numeric_answer_for_field(el, label)
        max_len = self._field_max_length(el)
        # If free-text salary into a tiny box, force numeric target
        if salaryish and max_len and max_len <= 12 and not out.isdigit():
            out = self._salary_answer(label, numeric=True)
        if max_len:
            before = out
            out = truncate_at_word(out, max_len)
            if before != out:
                logger.info(
                    "Truncated answer for '%s' to %s chars (was %s)",
                    (label or "")[:50],
                    max_len,
                    len(before),
                )
        self._debug_fill(
            "shape label=%r digits=%s years=%s salary=%s max=%s -> %r",
            (label or "")[:60],
            digits,
            yearsish,
            salaryish,
            max_len,
            (out or "")[:80],
        )
        return out

    def _clear_input(self, el) -> None:
        try:
            el.click()
        except Exception:
            try:
                self.driver.execute_script("arguments[0].focus();", el)
            except Exception:
                pass
        try:
            el.clear()
        except Exception:
            pass
        try:
            self.driver.execute_script(
                """
                const el = arguments[0];
                const proto = el.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) { desc.set.call(el, ''); }
                else { el.value = ''; }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                el,
            )
        except Exception:
            pass

    def _fill_input_with_retries(self, el, label: str) -> bool:
        """Try fill strategies until value fits constraints (bounded, no infinite loop).

        Order for numeric / 0–20 boxes: digits first. Otherwise profile/Ollama then truncate.
        """
        max_len = self._field_max_length(el)
        yearsish = self._is_years_question(label) or self._is_count_question(label)
        wants_digits = self._field_wants_digits_only(el, label)
        dateish = self._is_date_field(el, label) or self._is_start_or_notice_question(label)
        salaryish = any(
            k in fold_accents(label)
            for k in ("salary", "gehalt", "gehaltsvorstellungen", "compensation", "ctc")
        )
        strategies: list[tuple[str, str]] = []

        # Date / Startdatum: type MM/DD/YYYY (or ISO) — never Ollama essay, never calendar click
        if dateish:
            strategies.append(("start_date", self._start_date_answer(el, label)))
            strategies.append(
                ("start_date_mmddyyyy", earliest_start_date_mmddyyyy(
                    self.config.eu_work_authorization.notice_period_days
                ))
            )
            strategies.append(
                ("start_date_iso", earliest_start_date_iso(
                    self.config.eu_work_authorization.notice_period_days
                ))
            )
        # Numeric boxes: never lead with an Ollama sentence that will just fail validation
        elif wants_digits or yearsish:
            strategies.append(("numeric", self._numeric_answer_for_field(el, label)))
            if salaryish:
                strategies.append(("salary_digits", self._salary_answer(label, numeric=True)))
            strategies.append(("fallback_zero", "0"))
        else:
            primary = self._resolve_text_answer(label, el)
            if primary:
                strategies.append(("primary", primary))
            if primary and max_len:
                strategies.append(("truncate", truncate_at_word(primary, max_len)))
            if salaryish:
                strategies.append(
                    ("fallback_salary", str(self.config.compensation_eu.expected_salary_eur_target))
                )

        seen_vals: set[str] = set()
        for name, raw in strategies[:6]:  # hard cap attempts
            shaped = self._shape_answer_for_field(el, label, raw)
            if not shaped or shaped in seen_vals:
                continue
            seen_vals.add(shaped)
            if max_len and len(shaped) > max_len:
                shaped = truncate_at_word(shaped, max_len)
            self._debug_fill(
                "retry strategy=%s label=%r value=%r max=%s",
                name,
                (label or "")[:50],
                shaped[:60],
                max_len,
            )
            self._clear_input(el)
            self._set_input(el, shaped, label=label)
            short_delay(200)
            cur = (el.get_attribute("value") or "").strip()
            if not cur:
                continue
            if max_len and len(cur) > max_len:
                continue
            if dateish:
                # Accept MM/DD/YYYY or YYYY-MM-DD; do not require pure digits
                if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", cur) and not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", cur
                ):
                    # Some locales keep a partial typed value — still OK if non-empty and not years "8"
                    if cur.isdigit() and len(cur) <= 2:
                        continue
            elif (yearsish or wants_digits) and not re.fullmatch(r"\d+", cur):
                continue
            logger.info(
                "Filled '%s' via %s -> %s (len=%s max=%s)",
                (label or "")[:50],
                name,
                cur[:60],
                len(cur),
                max_len,
            )
            return True
        logger.warning(
            "Could not fill '%s' within constraints (max=%s)",
            (label or "")[:60],
            max_len,
        )
        return False

    def _set_input(self, el, value: str, label: str = "") -> None:
        if value is None:
            return
        if not label:
            label = (
                el.get_attribute("aria-label")
                or el.get_attribute("placeholder")
                or el.get_attribute("name")
                or ""
            )
        value = self._shape_answer_for_field(el, label, str(value))
        if not value:
            return
        # Final hard safety: never type more than maxlength
        max_len = self._field_max_length(el)
        if max_len and len(value) > max_len:
            value = truncate_at_word(value, max_len)
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
            short_delay(100)
            self._clear_input(el)
            el.send_keys(value)
            self.driver.execute_script(
                """
                const el = arguments[0];
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                el,
            )
            short_delay(150)
            current = el.get_attribute("value") or ""
            if current != value:
                self.driver.execute_script(
                    """
                    const el = arguments[0];
                    const val = arguments[1];
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    el.focus();
                    if (desc && desc.set) { desc.set.call(el, val); }
                    else { el.value = val; }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    """,
                    el,
                    value,
                )
            final = el.get_attribute("value") or ""
            self._debug_fill(
                "set_input label=%r typed_len=%s actual_len=%s max=%s value=%r",
                (label or "")[:50],
                len(value),
                len(final),
                max_len,
                final[:60],
            )
        except Exception as exc:
            logger.debug("Set input failed: %s", exc)

    # ----- answers -----

    def _resolve_text_answer(self, label: str, el) -> str:
        if not label:
            label = (
                el.get_attribute("aria-label")
                or el.get_attribute("placeholder")
                or el.get_attribute("name")
                or "application question"
            )
        max_len = self._field_max_length(el)
        digits = self._field_wants_digits_only(el, label)

        # Years questions: never call Ollama for a sentence — digits only
        if self._is_years_question(label):
            return self._years_answer_for_question(label)

        det = self._deterministic_answer(label)
        if det is not None and str(det).strip():
            return self._shape_answer_for_field(el, label, str(det))

        cached = self.qa.lookup(label)
        if cached:
            shaped = self._shape_answer_for_field(el, label, cached)
            if shaped and (not digits or shaped.isdigit()):
                if not max_len or len(shaped) <= max_len:
                    return shaped

        context = self.config.context_blob()
        if self.resume:
            context += f"\nResume JSON summary: {self.resume.get('summary_note', '')}\n"
            context += f"Skills: {', '.join(self.resume.get('skills', [])[:40])}\n"
            header = self.resume.get("header") or {}
            if header:
                context += (
                    f"Title: {header.get('title', '')}. Focus: {header.get('focus', '')}\n"
                )
        job_extra = ""
        if self._current_job:
            job_extra = (
                f"Applying for: {self._current_job.title} at {self._current_job.company} "
                f"({self._current_job.location})"
            )
        # Ask Ollama with hard char budget (prefer under counter)
        ollama_max = max_len
        if ollama_max and ollama_max > 0:
            job_extra = (
                f"{job_extra}\nIMPORTANT: answer must be at most {ollama_max} characters."
            ).strip()
        try:
            answer = self.ollama.answer_question(
                label,
                context,
                extra=job_extra,
                max_chars=ollama_max,
                digits_only=digits,
            )
            logger.info("Ollama answered '%s' -> %s", label[:70], (answer or "")[:80])
        except Exception as exc:
            logger.warning("Ollama answer failed: %s", exc)
            answer = ""
        answer = self._shape_answer_for_field(el, label, sanitize_human_text(answer))
        if answer and (not max_len or len(answer) <= max_len):
            self.qa.remember(label, answer)
        return answer

    def _national_phone_number(self) -> str:
        """Digits for the mobile/tel field — strip spaces and leading country dial code."""
        raw = self.config.profile.phone_number or ""
        digits = re.sub(r"\D", "", raw)
        if not digits:
            return ""
        # From "India (+91)" → "91"
        cc_digits = re.sub(r"\D", "", self.config.profile.phone_country_code or "")
        # Prefer the parenthetical dial code when present
        m = re.search(r"\(\s*\+?\s*(\d+)\s*\)", self.config.profile.phone_country_code or "")
        if m:
            cc_digits = m.group(1)
        if cc_digits and digits.startswith(cc_digits) and len(digits) > len(cc_digits) + 6:
            digits = digits[len(cc_digits) :]
        # Common India storage: 91XXXXXXXXXX
        elif digits.startswith("91") and len(digits) >= 12:
            digits = digits[2:]
        return digits

    def _deterministic_answer(self, question: str) -> str | None:
        q = fold_accents(question)
        p = self.config.profile
        e = self.config.eu_work_authorization

        if any(k in q for k in ("first name", "vorname")):
            return p.first_name
        if any(k in q for k in ("last name", "family name", "nachname")):
            return p.last_name
        if "email" in q or "e-mail" in q:
            return p.email
        if any(
            k in q
            for k in (
                "phone country",
                "country code",
                "calling code",
                "dial code",
                "landervorwahl",
                "laendervorwahl",
            )
        ):
            return p.phone_country_code
        if "phone" in q or "mobil" in q or "telefon" in q or "mobile number" in q:
            # National number only — never paste "+91" into the mobile field
            return self._national_phone_number()
        # Country of residence / nationality (not phone country code)
        if any(
            k in q
            for k in (
                "country of residence",
                "current country",
                "country/region",
                "country region",
                "which country",
                "select a country",
                "select country",
                "nationality",
                "citizenship",
                "land",
            )
        ) or (re.search(r"\bcountry\b", q) and "phone" not in q and "code" not in q):
            return p.current_country
        if any(k in q for k in ("city", "location", "standort")) and "salary" not in q:
            return p.location_full
        if "linkedin" in q:
            return p.linkedin_url
        if "github" in q:
            return p.github_url

        if any(
            k in q
            for k in (
                "visa",
                "sponsorship",
                "work permit",
                "work authorization",
                "visum",
                "aufenthalt",
                "arbeitserlaubnis",
            )
        ):
            if any(k in q for k in ("sponsor", "sponsorship", "arbeitgeber")) and "own" not in q:
                return e.requires_visa_sponsorship
            if any(k in q for k in ("own", "provide", "eigen")):
                return e.can_provide_own_germany_visa
            return e.germany_visa_statement

        if self._is_start_or_notice_question(question):
            return notice_period_value(
                question,
                e.notice_period_days,
                e.notice_period_weeks,
                e.notice_period_months,
            )

        if any(
            k in q
            for k in (
                "salary",
                "compensation",
                "gehalt",
                "gehaltsvorstellungen",
                "vergütung",
                "verguetung",
                "ctc",
                "expect",
            )
        ):
            return self._salary_answer(question, numeric=False)

        if self._is_years_question(question):
            return self._years_answer_for_question(question)

        if "cover letter" in q or "anschreiben" in q:
            return build_cover_letter(
                self.config,
                position=(self._current_job.title if self._current_job else ""),
                company=(self._current_job.company if self._current_job else ""),
                location=(self._current_job.location if self._current_job else ""),
                adapt=False,
            )

        return None

    def _salary_answer(self, question: str, numeric: bool) -> str:
        q = normalize_text(question)
        c = self.config.compensation_eu
        if "lpa" in q or "lakhs" in q:
            return str(c.expected_ctc_lpa)
        if "inr" in q or "rupee" in q:
            return str(c.expected_ctc_lpa * 100000)
        # Numeric / digits-only fields must never get "75000 - 95000 EUR"
        if numeric or "whole number" in q or 'type="number"' in q:
            if "minimum" in q or re.search(r"\bmin\b", q):
                return str(c.expected_salary_eur_min)
            if "maximum" in q or re.search(r"\bmax\b", q):
                return str(c.expected_salary_eur_max)
            return str(c.expected_salary_eur_target)
        if any(k in q for k in ("exact", "specific", "single number", "annual salary expect")):
            return str(c.expected_salary_eur_target)
        if "range" in q or not numeric:
            return c.expected_salary_range_text
        return str(c.expected_salary_eur_target)

    # ----- validation / recovery -----

    def _validation_errors(self) -> list[str]:
        """Real form errors only — never SDUI '6/100' character counters."""
        found: list[str] = []
        els = self.driver.find_elements(By.CSS_SELECTOR, sel.VALIDATION_ERROR)
        for e in els:
            t = (e.text or "").strip()
            if t and self._el_displayed(e) and not self._is_char_counter_text(t):
                found.append(t)
        modal = self._modal_root()
        root = modal if modal is not None else self.driver
        try:
            for p in root.find_elements(By.CSS_SELECTOR, "p, span"):
                t = (p.text or "").strip()
                if not t or len(t) > 120:
                    continue
                if not self._el_displayed(p):
                    continue
                if self._is_char_counter_text(t):
                    continue
                tn = normalize_text(t)
                # Exact / prefix markers only — do NOT match bare "character"
                if any(m == tn or tn.startswith(m) for m in sel.VALIDATION_TEXT_MARKERS):
                    if t not in found:
                        found.append(t)
                        continue
                if tn == "invalid input" or tn.startswith("invalid input"):
                    if t not in found:
                        found.append(t)
                        continue
                if any(
                    k in tn
                    for k in (
                        "must be a number",
                        "enter a whole number",
                        "only numbers",
                        "please enter a valid",
                    )
                ):
                    if t not in found:
                        found.append(t)
        except Exception:
            pass
        self._debug_fill("validation_errors=%s", found[:8])
        return found

    def _handle_validation_errors_with_learning(self) -> bool:
        """Fix only fields that show errors — never re-fill the whole page in a loop."""
        prev_sigs: list[str] = []
        for round_i in range(3):
            errors = self._validation_errors()
            if not errors:
                return True
            logger.info("Validation round %s: %s", round_i + 1, errors)
            err_blob = normalize_text(" ".join(errors))
            if "invalid" in err_blob or "must be a number" in err_blob or "whole number" in err_blob:
                self._fix_invalid_input_fields_only()
            elif "required" in err_blob:
                self._auto_fill_from_knowledge(errors)
                self._ensure_required_fields_filled()
            else:
                self._fix_invalid_input_fields_only()
            short_delay(500)
            sig = self._page_input_signature()
            if sig and sig in prev_sigs:
                logger.warning(
                    "Validation stuck with same field values after round %s — stopping refill loop",
                    round_i + 1,
                )
                break
            if sig:
                prev_sigs.append(sig)
        return not self._validation_errors()

    def _page_input_signature(self) -> str:
        """Snapshot of visible input values to detect no-progress validation loops."""
        modal = self._modal_root()
        if modal is None:
            return ""
        parts: list[str] = []
        try:
            for el in modal.find_elements(
                By.CSS_SELECTOR,
                "input[type='number'], input[type='text'], textarea, input[type='tel'], "
                "input:not([type])",
            ):
                if not self._el_displayed(el):
                    continue
                parts.append((el.get_attribute("value") or "").strip())
        except Exception:
            return ""
        return "||".join(parts)

    def _inputs_with_nearby_errors(self) -> list:
        """Inputs that have Invalid input / number errors shown next to them."""
        modal = self._modal_root()
        if modal is None:
            return []
        try:
            return self.driver.execute_script(
                """
                const root = arguments[0];
                const marker = /invalid input|must be a number|enter a whole number|only numbers|please enter a valid/i;
                const out = [];
                const seen = new Set();
                for (const node of root.querySelectorAll('p, span, div, label')) {
                  const t = (node.textContent || '').trim();
                  if (!t || t.length > 100) continue;
                  if (!marker.test(t)) continue;
                  // Prefer error text that is short (not a whole section blob)
                  if (t.length > 60 && !/^invalid input/i.test(t)) continue;
                  let p = node.parentElement;
                  for (let i = 0; i < 6 && p; i++) {
                    const inp = p.querySelector(
                      "input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea"
                    );
                    if (inp && !seen.has(inp)) {
                      seen.add(inp);
                      out.push(inp);
                      break;
                    }
                    p = p.parentElement;
                  }
                }
                return out;
                """,
                modal,
            ) or []
        except Exception as exc:
            self._debug_fill("inputs_with_nearby_errors failed: %s", exc)
            return []

    def _fix_invalid_input_fields_only(self) -> None:
        """Replace only errored (or non-digit short) fields with a number — no page-wide refill."""
        modal = self._modal_root()
        if modal is None:
            return
        targets = list(self._inputs_with_nearby_errors())
        if not targets:
            # Fallback: short-cap fields that still hold non-digit text
            try:
                for el in modal.find_elements(
                    By.CSS_SELECTOR,
                    "input[type='number'], input[type='text'], textarea, input:not([type])",
                ):
                    if not self._el_displayed(el):
                        continue
                    label = self._label_for_control(modal, el) or self._question_near_element(el)
                    cur = (el.get_attribute("value") or "").strip()
                    max_len = self._field_max_length(el)
                    if self._is_identity_text_field(label) or self._is_start_or_notice_question(label):
                        continue
                    if (max_len and max_len <= 20 and cur and not re.fullmatch(r"\d+", cur)) or (
                        self._field_wants_digits_only(el, label) and cur and not re.fullmatch(r"\d+", cur)
                    ):
                        targets.append(el)
            except Exception:
                pass

        fixed = 0
        for el in targets:
            try:
                if not self._el_displayed(el):
                    continue
                label = self._label_for_control(modal, el) or self._question_near_element(el)
                cur = (el.get_attribute("value") or "").strip()
                if self._is_date_field(el, label) or self._is_start_or_notice_question(label):
                    fix = self._start_date_answer(el, label)
                else:
                    fix = self._numeric_answer_for_field(el, label)
                    if cur == fix:
                        if cur == "0":
                            continue
                        fix = "0"
                if cur == fix:
                    continue
                self._debug_fill(
                    "invalid-only fix label=%r was=%r -> %r",
                    (label or "")[:50],
                    cur[:40],
                    fix,
                )
                self._clear_input(el)
                self._set_input(el, fix, label=label)
                fixed += 1
                logger.info(
                    "Fixed invalid field '%s' -> %s (was %r)",
                    (label or "")[:50],
                    fix,
                    cur[:40],
                )
            except Exception as exc:
                logger.debug("Invalid-field fix failed: %s", exc)
        if fixed:
            logger.info("Corrected %s invalid input field(s) with numbers", fixed)

    def _reshape_invalid_inputs(self, errors: list[str]) -> None:
        """Back-compat wrapper — prefer targeted numeric fixes only."""
        del errors  # errors listed for callers; DOM proximity is the source of truth
        self._fix_invalid_input_fields_only()

    def _auto_fill_from_knowledge(self, errors: list[str]) -> None:
        for err in errors:
            e = normalize_text(err)
            m = re.search(r"between\s+(\d+)\s+and\s+(\d+)", e)
            sections = self._form_sections()
            for section in sections:
                label = self._section_label(section)
                # Prefer radios/selects tied to error text
                label_n = normalize_text(label)
                related = bool(label_n) and (
                    label_n in e
                    or any(len(w) > 3 and w in e for w in label_n.split()[:4])
                )
                if related:
                    try:
                        self._fill_section(section)
                    except Exception:
                        pass
                inputs = section.find_elements(
                    By.CSS_SELECTOR,
                    "input[type='number'], input[type='text'], textarea, input[type='tel'], "
                    "input[type='email']",
                )
                if not inputs:
                    continue
                el = inputs[0]
                if "whole number" in e or m or self._field_wants_digits_only(el, label + " " + err):
                    val = self._salary_answer(label + " " + err, numeric=True)
                    if self._is_years_question(label):
                        val = self._years_answer_for_question(label)
                    if m:
                        lo, hi = int(m.group(1)), int(m.group(2))
                        try:
                            n = int(re.sub(r"\D", "", val) or "0")
                            n = max(lo, min(hi, n))
                            val = str(n)
                        except ValueError:
                            val = str(lo)
                    self._clear_input(el)
                    self._set_input(el, val, label=label)
                    continue
                self._fill_input_with_retries(el, label)

    def _dump_and_discard(self, job: JobCard, reason: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_dir = project_path("crash_dumps")
        dump_dir.mkdir(parents=True, exist_ok=True)
        base = dump_dir / f"job_{job.job_id or 'unknown'}_{stamp}"
        try:
            self.driver.save_screenshot(str(base.with_suffix(".png")))
        except Exception:
            pass
        try:
            base.with_suffix(".html").write_text(self.driver.page_source, encoding="utf-8")
        except Exception:
            pass
        index = dump_dir / "index.csv"
        exists = index.exists()
        with index.open("a", encoding="utf-8") as f:
            if not exists:
                f.write("job_id,timestamp,link,reason,dump\n")
            f.write(f"{job.job_id},{stamp},{job.link},{reason.replace(',', ';')},{base.name}\n")
        append_unanswered(reason, reason, job_id=job.job_id)
        self._discard_modal()

    def _discard_modal(self) -> None:
        try:
            for btn in self.driver.find_elements(By.CSS_SELECTOR, sel.DISMISS_BUTTON):
                if btn.is_displayed():
                    btn.click()
                    short_delay(500)
                    break
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "button"):
                t = (btn.text or "").strip()
                if any(x.lower() == t.lower() or x.lower() in t.lower() for x in sel.DISCARD_TEXTS):
                    if btn.is_displayed():
                        btn.click()
                        short_delay(400)
                        return
            for btn in self.driver.find_elements(By.CSS_SELECTOR, sel.DISCARD_CONFIRM):
                if btn.is_displayed():
                    btn.click()
                    short_delay(400)
                    return
        except Exception as exc:
            logger.debug("Discard failed: %s", exc)
