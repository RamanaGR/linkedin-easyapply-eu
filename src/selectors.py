"""Central CSS / XPath selector registry for Easy Apply."""

# Classic Voyager Easy Apply (legacy)
FORM_SECTIONS_LEGACY = (
    ".jobs-easy-apply-form-section__grouping, .fb-dash-form-element, "
    ".jobs-easy-apply-modal .fb-dash-form-element, "
    "[data-test-form-element]"
)
# Keep alias used by application.py
FORM_SECTIONS = FORM_SECTIONS_LEGACY

TYPEAHEAD_CONTENT = (
    ".basic-typeahead__triggered-content, [role='listbox'], "
    ".search-basic-typeahead__triggered-content, "
    "[data-testid='typeahead-input']"
)
VALIDATION_ERROR = (
    ".artdeco-inline-feedback--error, "
    ".fb-dash-form-element__error-field, "
    "[data-test-form-element-error]"
)
# SDUI radios: outer clickable is div[role=radio]; option text is sibling <p>Yes</p>
RADIO_GROUP = 'fieldset[role="radiogroup"], [role="radiogroup"]'
RADIO_OPTION = '[role="radio"]'
CHECKBOX_INPUT = "input[type='checkbox']"
VALIDATION_TEXT_MARKERS = (
    "this field is required",
    "please enter a valid answer",
    "please make a selection",
    "please select an option",
    "invalid input",
    "pflichtfeld",
    "dies ist ein pflichtfeld",
    "erforderlich",
)
# SDUI character counters — NOT validation errors (e.g. "6/100", "1 of 20 characters")
CHAR_COUNTER_RE = r"^\d+\s*/\s*\d+$|^\d+\s+of\s+\d+\s+characters?$"
FOLLOW_COMPANY = "#follow-company-checkbox, input[type='checkbox'][id*='follow']"

# --- New LinkedIn SDUI Easy Apply (2026): native <dialog>, hashed class names ---
# Confirmed from live crash dump: dialog[data-testid=dialog][open]
# with data-sdui-screen="...jobs.easyapply.EasyApply"
EASY_APPLY_DIALOG = (
    'dialog[data-testid="dialog"][open], '
    'dialog[data-testid="dialog"], '
    "dialog[open], "
    '[data-sdui-screen*="easyapply"], '
    '[data-sdui-screen*="EasyApply"]'
)
EASY_APPLY_DIALOG_CONTENT = (
    '[data-testid="dialog-content"], '
    "#dialog-header, "
    '[data-sdui-screen*="easyapply"], '
    '[data-sdui-screen*="EasyApply"]'
)

# Legacy modal wrappers + new dialog
EASY_APPLY_MODAL = (
    f"{EASY_APPLY_DIALOG}, "
    ".jobs-easy-apply-modal, "
    "div.jobs-easy-apply-modal, "
    ".artdeco-modal.jobs-easy-apply-modal, "
    "div[data-test-modal].jobs-easy-apply-modal, "
    ".artdeco-modal[role='dialog'], "
    "div[role='dialog'].artdeco-modal, "
    "div[data-test-modal][role='dialog']"
)
EASY_APPLY_MODAL_CONTENT = (
    f"{EASY_APPLY_DIALOG_CONTENT}, "
    ".jobs-easy-apply-content, "
    ".jobs-easy-apply-content__footer, "
    ".fb-dash-form-element, "
    ".jobs-easy-apply-form-section__grouping, "
    "[data-test-form-element], "
    "footer.jobs-easy-apply-footer, "
    "[data-easy-apply-footer], "
    ".artdeco-modal__content"
)
EASY_APPLY_OPEN_SIGNALS = (
    f"{EASY_APPLY_DIALOG}, "
    '[data-sdui-screen*="EasyApply"], '
    ".jobs-easy-apply-content, "
    ".jobs-easy-apply-modal, "
    ".fb-dash-form-element"
)

DISMISS_BUTTON = (
    'dialog[data-testid="dialog"] button[aria-label="Dismiss"], '
    'dialog[open] button[aria-label="Dismiss"], '
    "button[aria-label='Dismiss'], button[aria-label='Schließen'], "
    ".jobs-easy-apply-modal button[aria-label*='Dismiss']"
)
DISCARD_CONFIRM = (
    "button[data-control-name='discard_application_confirm_btn'], "
    "button[data-test-dialog-primary-btn], "
    "button[data-testid='dialog-primary-button']"
)
FILE_INPUT = "input[type='file']"
JOBS_SEARCH_RESULTS = ".jobs-search-results-list, .scaffold-layout__list"
JOB_CARD = "li.scaffold-layout__list-item, div.job-card-container"
EASY_APPLY_BUTTON = (
    "button.jobs-apply-button, button#jobs-apply-button-id, "
    "button[aria-label*='Easy Apply'], button[aria-label*='Einfach bewerben'], "
    "button[data-live-test-job-apply-button]"
)
# Red daily-limit banner under grayed Easy Apply (job top card)
EASY_APPLY_LIMIT_FEEDBACK = (
    ".jobs-s-apply ~ .artdeco-inline-feedback--error, "
    ".artdeco-inline-feedback--error[role='alert'], "
    ".artdeco-inline-feedback--error .artdeco-inline-feedback__message, "
    ".jobs-apply-button--top-card ~ .artdeco-inline-feedback--error, "
    ".mt4 .artdeco-inline-feedback--error"
)
NEXT_BUTTON_TEXTS = (
    "Next",
    "Continue",
    "Continue to next step",
    "Review",
    "Review your application",
    "Weiter",
    "Fortfahren",
    "Prüfen",
    "Zur Überprüfung",
)
SUBMIT_BUTTON_TEXTS = (
    "Submit application",
    "Submit",
    "Bewerbung absenden",
    "Absenden",
)
DISCARD_TEXTS = ("Discard", "Verwerfen", "Discard application")
APPLIED_CONFIRM_TEXTS = (
    "Application sent",
    "Your application was sent",
    "Bewerbung wurde gesendet",
    "Application submitted",
)
REQUIRED_MARKERS = ("*", "required", "pflicht", "obligatoire", "obbligatorio")

# Only real LinkedIn auth/challenge URLs — do NOT scan page body for "captcha"
CHECKPOINT_URL_FRAGMENTS = (
    "/checkpoint/challenge",
    "/checkpoint/challenges",
    "/challenge/",
    "/authwall",
)
LOGIN_VERIFY_URL_FRAGMENTS = (
    "/checkpoint/lg/",
    "/checkpoint/challenge",
    "/two-step-verification",
    "/manage/verification",
)
