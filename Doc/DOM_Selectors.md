# LinkedIn Easy Apply — DOM Selector Registry

Living document. Update when LinkedIn UI breaks.

| Purpose | Selector / strategy | Notes | Last verified |
|---|---|---|---|
| Login email (new UI) | `input[type=email]`, `input[autocomplete*=username]` | Dynamic ids; NOT `#username` | 2026-10-16 |
| Login password (new UI) | `input[type=password]`, `autocomplete=current-password` | Dynamic ids; NOT `#password` | 2026-10-16 |
| Login submit (new UI) | `button` with span text `Sign in` (`type=button`) | Not `type=submit`; skip Apple/Google | 2026-10-16 |
| **Easy Apply dialog (SDUI 2026)** | `dialog[data-testid="dialog"][open]` | Native `<dialog>`, NOT `.jobs-easy-apply-modal` | 2026-10-16 |
| SDUI screen marker | `[data-sdui-screen*="EasyApply"]` | `com.linkedin.sdui...easyapply.EasyApply` | 2026-10-16 |
| Dialog header | `#dialog-header` / `h2` text `Apply to …` | Progress text e.g. `1/4 pages` | 2026-10-16 |
| Dialog content | `[data-testid="dialog-content"]` | Hashed CSS class names (`iii…`) — do not rely on them | 2026-10-16 |
| Form fields (SDUI) | `label[for]` + `#id` input/select, `[required]` | No `.fb-dash-form-element` in SDUI | 2026-10-16 |
| Max length (SDUI) | `maxlength` / counter under the input (`0/20`) | Field-local only; ignore header progress `1/4 pages` | 2026-10-18 |
| Short counter ≤20 | `0/20` under input (not identity / start-date) | Years/salary → number-only; **Startdatum excluded** | 2026-10-18 |
| Start / date fields | text or `type=date` (+ optional calendar button) | Type `MM/DD/YYYY` (ISO for native date); **never click picker** | 2026-10-18 |
| Invalid input recovery | Error text next to the field | Fix **that** field with a digit; do not re-fill whole page | 2026-10-18 |
| Years experience fields | EN/DE/ES/NL “how many years / años / jaar” | Digits only (e.g. `8`), never a sentence; accent-insensitive | 2026-10-18 |
| Numeric-only fields | `type=number`, `inputmode=numeric`, digit `pattern` | Salary → `85000` not range text | 2026-10-18 |
| Debug fill logs | `EASYAPPLY_DEBUG_FILL=true` in `.env` | Logs maxlength / retry strategies | 2026-10-18 |
| Next (SDUI) | `dialog footer button` text `Next` | Ignore `data-testid=carousel-inline-right-button` | 2026-10-16 |
| Dismiss | `dialog button[aria-label=Dismiss]` | | 2026-10-16 |
| Easy Apply button (job page) | `button[aria-label*='Easy Apply']` | Skip if inside `dialog` | 2026-10-16 |
| Form sections (legacy) | `.jobs-easy-apply-form-section__grouping, .fb-dash-form-element` | Old Voyager UI only | TBD |
| Typeahead listbox | `.basic-typeahead__triggered-content`, `[role="listbox"] [role=option]` | Must click **first option** before Next or page sticks | 2026-10-18 |
| Cover letter | config `cover_letter.template` | Instant `{company}`/`{position}` fill — no Ollama | 2026-10-18 |
| Validation errors | `.artdeco-inline-feedback--error` | Legacy; SDUI may differ | TBD |
| Follow company | `#follow-company-checkbox` | Uncheck before submit | TBD |
| Discard confirm | text Discard/Verwerfen | | TBD |
| Captcha / checkpoint | URL contains `/checkpoint/` | Pause for human | TBD |
| Easy Apply daily limit | Search right pane: `#jobs-apply-button-id[disabled]` + `.artdeco-inline-feedback--error .artdeco-inline-feedback__message` (“We limit daily submissions… Save this job and apply tomorrow.”) | Check **immediately after `open_search`** (two-pane details), then again on job page — `STOPPED_EASY_APPLY_LIMIT` | 2026-10-18 |
