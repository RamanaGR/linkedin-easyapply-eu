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
from src.ollama_client import OllamaClient, sanitize_human_text
from src.qa_intent import IntentMatcher
from src.qa_manager import QAMemoryManager, append_unanswered
from src.utils import notice_period_value, normalize_text, project_path, short_delay

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
        if not self._click_easy_apply():
            logger.warning("Easy Apply modal did not load for job %s", job.job_id)
            return "FAILED_EXCEPTION"
        try:
            for _step in range(12):
                if not self._wait_for_form_ready(timeout=FORM_READY_SECONDS):
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

                # After Next: either advance, or stay with validation errors to refill
                advanced = self._wait_for_step_change(before, timeout=STEP_CHANGE_SECONDS)
                errors = self._validation_errors()
                if errors:
                    logger.info("Validation after Next: %s — refilling", errors)
                    self._fill_current_page()
                    self._ensure_required_fields_filled()
                    self._fill_sdui_radio_groups()
                    if not self._handle_validation_errors_with_learning():
                        still = self._validation_errors()
                        if still and self.config.session.unattended_mode:
                            self._dump_and_discard(job, "; ".join(still))
                            return "FAILED_VALIDATION"
                elif not advanced:
                    logger.debug("Step fingerprint unchanged after Next; continuing fill loop")

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

    def _click_easy_apply(self) -> bool:
        """Click Easy Apply once, then wait for the form dialog — never re-click Easy Apply."""
        try:
            # Form already open from a previous step / slow paint
            if self._easy_apply_form_is_open():
                logger.info("Easy Apply form already open — skipping button click")
                return self._wait_for_form_ready(timeout=FORM_READY_SECONDS)

            btn = self._find_easy_apply_button()
            if btn is None:
                logger.warning("Easy Apply button not found")
                return False

            label = (btn.text or btn.get_attribute("aria-label") or "").strip()
            logger.info("Clicking Easy Apply button: %r", label[:80])
            self._safe_click(btn)

            if not self._wait_for_easy_apply_modal(timeout=MODAL_WAIT_SECONDS):
                # Dump once for DOM debugging — form may use a new wrapper class
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

    def _find_easy_apply_button(self):
        """Job-page Easy Apply only — ignore buttons inside an already-open dialog."""
        candidates = []
        for btn in self.driver.find_elements(By.CSS_SELECTOR, sel.EASY_APPLY_BUTTON):
            try:
                if not self._el_displayed(btn) or not btn.is_enabled():
                    continue
                # Skip controls inside a dialog (form footer / nested)
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
                blob = f"{text} {aria}"
                if any(
                    k in blob
                    for k in ("easy apply", "einfach bewerben", "candidatura semplificata")
                ):
                    candidates.insert(0, btn)
                elif "jobs-apply-button" in (btn.get_attribute("class") or "") and (
                    "apply" in blob and "save" not in blob and "external" not in blob
                ):
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
        return self._click_button_by_texts(sel.NEXT_BUTTON_TEXTS)

    def _click_submit(self) -> bool:
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
        """Fill empty required input/select in the open dialog by associated label."""
        modal = self._modal_root()
        if modal is None:
            return
        try:
            controls = modal.find_elements(
                By.CSS_SELECTOR,
                "input[required], select[required], textarea[required], "
                "input[aria-required='true'], select[aria-required='true'], textarea[aria-required='true']",
            )
            for el in controls:
                try:
                    tag = el.tag_name.lower()
                    if tag == "select":
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
                        continue
                    val = (el.get_attribute("value") or "").strip()
                    if val:
                        continue
                    label = self._label_for_control(modal, el)
                    low = normalize_text(label)
                    if el.get_attribute("type") == "number" or "salary" in low:
                        answer = self._salary_answer(label, numeric=True)
                    else:
                        answer = self._resolve_text_answer(label, el)
                    if answer:
                        self._set_input(el, answer)
                        logger.info("SDUI input '%s' -> %s", label[:60], str(answer)[:60])
                except Exception as exc:
                    logger.debug("SDUI control fill error: %s", exc)
        except Exception as exc:
            logger.debug("SDUI required scan failed: %s", exc)

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
                val = (el.get_attribute("value") or "").strip()
                if not val and (required or self._input_aria_required(el)):
                    pending.append(label or el.get_attribute("name") or "text input")
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

        # Typeahead / location
        if any(k in low for k in ("city", "location", "standort", "ort", "typeahead")):
            if text_inputs:
                existing = (text_inputs[0].get_attribute("value") or "").strip()
                if not existing:
                    self._fill_typeahead(text_inputs[0], self.config.profile.location_full)
                return

        if triggers and not text_inputs:
            btn_text = (triggers[0].text or "").strip()
            if btn_text and "select" not in normalize_text(btn_text):
                return
            self._fill_custom_dropdown(section, triggers[0], label or "dropdown")
            return

        if text_inputs:
            el = text_inputs[0]
            existing = (el.get_attribute("value") or "").strip()
            if existing:
                # Still override empty-looking cover letter placeholders if needed
                if not (
                    el.tag_name.lower() == "textarea"
                    and any(k in low for k in ("cover letter", "anschreiben", "motivation"))
                    and len(existing) < 20
                ):
                    return
            q = label or el.get_attribute("aria-label") or el.get_attribute("placeholder") or ""
            if el.get_attribute("type") == "number" or "salary" in low or "gehalt" in low:
                answer = self._salary_answer(q, numeric=True)
            else:
                answer = self._resolve_text_answer(q, el)
            if not answer:
                logger.warning("No answer for field '%s'", q[:100])
                append_unanswered(
                    q,
                    "empty_answer",
                    job_id=self._current_job.job_id if self._current_job else "",
                )
                return
            self._set_input(el, answer)
            if el.tag_name.lower() == "textarea" and any(
                k in low for k in ("cover letter", "anschreiben", "motivation")
            ):
                cl = build_cover_letter(
                    self.config,
                    self.ollama,
                    position=(self._current_job.title if self._current_job else ""),
                    company=(self._current_job.company if self._current_job else ""),
                    location=(self._current_job.location if self._current_job else ""),
                )
                if cl:
                    self._set_input(el, cl)

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
        self._set_input(el, value)
        short_delay(800)
        try:
            self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel.TYPEAHEAD_CONTENT))
            )
        except TimeoutException:
            el.send_keys(Keys.CONTROL + "a")
            el.send_keys(Keys.BACKSPACE)
            el.send_keys(self.config.profile.current_city)
            short_delay(800)
        el.send_keys(Keys.ARROW_DOWN)
        short_delay(200)
        el.send_keys(Keys.ENTER)
        short_delay(300)

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

    def _set_input(self, el, value: str) -> None:
        if value is None:
            return
        value = str(value)
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
            short_delay(100)
            try:
                el.click()
            except Exception:
                self.driver.execute_script("arguments[0].focus();", el)
            try:
                el.clear()
            except Exception:
                self.driver.execute_script("arguments[0].value = '';", el)
            el.send_keys(value)
            # Fire React-friendly events
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
        det = self._deterministic_answer(label)
        if det is not None and str(det).strip():
            return str(det)
        cached = self.qa.lookup(label)
        if cached:
            return cached
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
        try:
            answer = self.ollama.answer_question(label, context, extra=job_extra)
            logger.info("Ollama answered '%s' -> %s", label[:70], (answer or "")[:80])
        except Exception as exc:
            logger.warning("Ollama answer failed: %s", exc)
            answer = ""
        answer = sanitize_human_text(answer)
        if answer:
            self.qa.remember(label, answer)
        return answer

    def _deterministic_answer(self, question: str) -> str | None:
        q = normalize_text(question)
        p = self.config.profile
        e = self.config.eu_work_authorization
        c = self.config.compensation_eu

        if any(k in q for k in ("first name", "vorname")):
            return p.first_name
        if any(k in q for k in ("last name", "family name", "nachname")):
            return p.last_name
        if "email" in q or "e-mail" in q:
            return p.email
        if any(k in q for k in ("phone country", "country code", "ländervorwahl", "laendervorwahl")):
            return p.phone_country_code
        if "phone" in q or "mobil" in q or "telefon" in q:
            # Prefer digits-only for tel inputs
            return re.sub(r"[^\d+]", "", p.phone_number)
        if any(k in q for k in ("city", "location", "standort")) and "salary" not in q:
            return p.location_full
        if "linkedin" in q:
            return p.linkedin_url
        if "github" in q:
            return p.github_url

        # Visa free text
        if any(
            k in q
            for k in (
                "visa",
                "sponsorship",
                "work permit",
                "work authorization",
                "visum",
                "aufenthalt",
            )
        ):
            if any(k in q for k in ("sponsor", "sponsorship", "arbeitgeber")) and "own" not in q:
                return e.requires_visa_sponsorship
            if any(k in q for k in ("own", "provide", "eigen")):
                return e.can_provide_own_germany_visa
            return e.germany_visa_statement

        if any(k in q for k in ("notice", "kündigung", "kuendigung", "available", "start date", "ab wann")):
            return notice_period_value(
                question,
                e.notice_period_days,
                e.notice_period_weeks,
                e.notice_period_months,
            )

        if any(k in q for k in ("salary", "compensation", "gehalt", "vergütung", "verguetung", "ctc", "expect")):
            return self._salary_answer(question, numeric=False)

        if any(k in q for k in ("years of experience", "how many years", "berufserfahrung")):
            for skill, years in self.config.experience.skill_years.items():
                if normalize_text(skill) in q:
                    return str(years)
            return str(self.config.experience.total_experience_years)

        if "cover letter" in q or "anschreiben" in q:
            return build_cover_letter(
                self.config,
                self.ollama,
                position=(self._current_job.title if self._current_job else ""),
                company=(self._current_job.company if self._current_job else ""),
                location=(self._current_job.location if self._current_job else ""),
            )

        return None

    def _salary_answer(self, question: str, numeric: bool) -> str:
        q = normalize_text(question)
        c = self.config.compensation_eu
        if "lpa" in q or "lakhs" in q:
            return str(c.expected_ctc_lpa)
        if "inr" in q or "rupee" in q:
            return str(c.expected_ctc_lpa * 100000)
        if numeric or "whole number" in q or 'type="number"' in q:
            if "minimum" in q or "min" in q:
                return str(c.expected_salary_eur_min)
            return str(c.expected_salary_eur_target)
        if "range" in q or not numeric:
            if any(k in q for k in ("exact", "specific", "annual salary expect")):
                return str(c.expected_salary_eur_target)
            return c.expected_salary_range_text
        return str(c.expected_salary_eur_target)

    # ----- validation / recovery -----

    def _validation_errors(self) -> list[str]:
        found: list[str] = []
        els = self.driver.find_elements(By.CSS_SELECTOR, sel.VALIDATION_ERROR)
        for e in els:
            t = (e.text or "").strip()
            if t and self._el_displayed(e):
                found.append(t)
        # SDUI: plain <p>This field is required</p> with no error class
        modal = self._modal_root()
        root = modal if modal is not None else self.driver
        try:
            for p in root.find_elements(By.CSS_SELECTOR, "p, span, div"):
                t = (p.text or "").strip()
                if not t or len(t) > 120:
                    continue
                tn = normalize_text(t)
                if any(m == tn or tn.startswith(m) for m in sel.VALIDATION_TEXT_MARKERS):
                    if t not in found and self._el_displayed(p):
                        found.append(t)
        except Exception:
            pass
        return found

    def _handle_validation_errors_with_learning(self) -> bool:
        for round_i in range(3):
            errors = self._validation_errors()
            if not errors:
                return True
            logger.info("Validation round %s: %s", round_i + 1, errors)
            self._auto_fill_from_knowledge(errors)
            self._fill_current_page()
            short_delay(500)
        return not self._validation_errors()

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
                if "whole number" in e or m:
                    val = self._salary_answer(label + " " + err, numeric=True)
                    if m:
                        lo, hi = int(m.group(1)), int(m.group(2))
                        try:
                            n = int(re.sub(r"\D", "", val) or "0")
                            n = max(lo, min(hi, n))
                            val = str(n)
                        except ValueError:
                            val = str(lo)
                    self._set_input(inputs[0], val)
                    continue
                answer = self._resolve_text_answer(f"{label}. Error: {err}", inputs[0])
                if answer:
                    self._set_input(inputs[0], answer)

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
