# CM pilot — consent & intake form

Standalone, self-contained consent+intake forms for the Meet Rudi × Christelijke Mutualiteit
pilot. Each file is one HTML document (inline CSS + JS, only external dep is the Nunito web font)
so it can be handed to an external page as-is.

- `consent-form-nl.html` — Dutch (the live form handed to CM).
- `consent-form-en.html` — English (reference / parity copy).

Both are 1:1 in structure (same field ids, validation, and submit flow); only the copy and the
enum option **values** differ by language. Values are submitted in the shown language with stable
**English keys**, per `services/registration/src/consent_form_fields.json`.

## Configure before use
Open the file and set, at the top of the `<script>`:
```js
var API_BASE = "https://<meetrudi-consent-intake-id>.lambda-url.eu-central-1.on.aws";
var PRIVACY_POLICY_VERSION = "…";   // once the versioned policy URL exists
var TERMS_VERSION = "…";
```
`deploy.py registration` prints the `API_BASE` URL. Also replace the `#privacy` / `#voorwaarden`
placeholder links with the real, versioned policy/terms URLs.

## How it works
1. Client-side validation mirrors the server (required fields, phone/email format, sliders must be
   *moved*, 1–2 help areas, mandatory health-data consent).
2. On submit, a **re-entry challenge** modal asks the user to re-type 2 of their text fields
   exactly (anti-bot + accuracy check).
3. It POSTs JSON to `API_BASE`; the `meetrudi-consent-intake` Lambda re-validates everything,
   normalizes the phone to E.164, computes the WhatsApp-linkable pseudonym `HMAC(phone)`, and
   writes a GDPR record to `s3://<data-bucket>/registrations/consent_documents/<HMAC>_<last4>_<ts>.json`.

## Payload contract (POST, application/json)
```json
{ "form_language": "nl", "form_version": "cm-2026-07",
  "privacy_policy_version": "", "terms_version": "",
  "answers": { "first_name": "…", "mobile_number": "…", "email": "…", "year_of_birth": 1975,
    "gender": "man", "municipality": "…", "cm_member_t2d": false, "help_areas": ["…"],
    "confidence_self_improve": 6, "energy_past_week": 4, "healthy_living_past_week": 5,
    "support_felt": 7, "active_days_past_week": 3, "prior_digital_tools": "…", "notes_for_rudi": "" },
  "consents": { "consent_health_data_processing": true, "consent_followup_contact": false },
  "consent_texts": { "consent_health_data_processing": "…verbatim…", "consent_followup_contact": "…" },
  "challenge": { "field": "first_name", "value": "…" },
  "hp_field": "" }
```
Responses: `201` ok · `422 {field}` validation · `400` bad json / challenge mismatch · `413` too large.

## CORS / external hosting
The Function URL CORS `AllowOrigins` defaults to `*` (POST + `content-type`), so the form works
from any external page. For production, set the `ConsentAllowOrigin` stack parameter to CM's exact
site origin to lock it down.
