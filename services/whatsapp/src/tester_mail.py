"""
MEET_RUDI — transactional mail for the tester console (verification + password reset).

One seam, one sender. `tester_api` calls `send_verification()` / `send_reset()` and never touches
an SES client, so swapping the provider later is a change here and nowhere else.

Rules this module exists to keep:
- **EU residency.** SES is used in the same region as everything else (eu-central-1). No
  third-party mail service is introduced without a DPA (§0.3).
- **i18n-ready.** Every user-facing string is keyed by locale (`nl-BE`, `en`) — nothing is
  hard-coded in the caller (§3). English is the fallback for an unknown locale.
- **No PII in logs.** We log the message id and the locale, never the address or the link. A
  link in CloudWatch would be a working credential.
- **Never fatal.** A mail failure returns False and lets the caller decide. Registration still
  succeeds; the admin pane can re-issue the link.
"""

from __future__ import annotations

import os
import html
import email.utils
from typing import Any, Optional

# The sender is carried as TWO settings, not one "Name <addr>" string, for two reasons:
#   - the SES IAM condition matches on ses:FromAddress, which is the bare address; keeping them
#     separate makes the policy and the config obviously the same value.
#   - a combined string has to survive `sam deploy --parameter-overrides` on Windows, where the
#     angle brackets are cmd redirection operators and silently break the deploy.
MAIL_FROM_ADDRESS = os.environ.get("TESTER_MAIL_FROM", "")          # e.g. "support@meetrudi.eu"
MAIL_FROM_NAME = os.environ.get("TESTER_MAIL_FROM_NAME", "")        # e.g. "Rudi test"
# formataddr quotes the display name correctly, so a comma or a non-ASCII character in it can't
# produce a malformed header.
MAIL_FROM = (email.utils.formataddr((MAIL_FROM_NAME, MAIL_FROM_ADDRESS))
             if MAIL_FROM_ADDRESS else "")
MAIL_REPLY_TO = os.environ.get("TESTER_MAIL_REPLY_TO", "")
CONSOLE_BASE = os.environ.get("TESTER_CONSOLE_BASE", "").rstrip("/")
LINK_TTL_HOURS = int(os.environ.get("TESTER_LINK_TTL_HOURS", "24"))

# --------------------------------------------------------------------------- copy, per locale
# Placeholders: {name} {link} {hours} {support}
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "verify_subject": "Verify your email and set your password",
        "verify_heading": "Hello {name},",
        "verify_body": (
            "Thanks for joining the Rudi test. Click the button below to verify this address "
            "and choose a password. The link works once and expires in {hours} hours."
        ),
        "verify_cta": "Verify and set password",
        "verify_footer": (
            "If you signed up more than once, only the newest of these mails works — the older "
            "links stop working the moment a new one is sent. "
            "Didn't sign up? Ignore this mail — no account is created until you click. "
            "Questions: {support}"
        ),
        "reset_subject": "Reset your Rudi test password",
        "reset_heading": "Hello {name},",
        "reset_body": (
            "Someone asked to reset the password for your Rudi test account. Click below to "
            "choose a new one. The link works once and expires in {hours} hours."
        ),
        "reset_cta": "Choose a new password",
        "reset_footer": (
            "Didn't ask for this? Ignore this mail and your password stays as it is. "
            "Questions: {support}"
        ),
        "fallback": "If the button doesn't work, paste this address into your browser:",
        "signoff": "The Rudi test team",
    },
    "nl-BE": {
        "verify_subject": "Bevestig je e-mailadres en kies een wachtwoord",
        "verify_heading": "Dag {name},",
        "verify_body": (
            "Bedankt om mee te testen met Rudi. Klik op de knop hieronder om dit adres te "
            "bevestigen en een wachtwoord te kiezen. De link werkt één keer en vervalt na "
            "{hours} uur."
        ),
        "verify_cta": "Bevestigen en wachtwoord kiezen",
        "verify_footer": (
            "Schreef je je meer dan één keer in? Dan werkt alleen de nieuwste van deze mails — "
            "oudere links vervallen zodra er een nieuwe verstuurd wordt. "
            "Heb je je niet ingeschreven? Negeer deze mail — er wordt pas een account "
            "aangemaakt als je klikt. Vragen: {support}"
        ),
        "reset_subject": "Stel je wachtwoord voor de Rudi-test opnieuw in",
        "reset_heading": "Dag {name},",
        "reset_body": (
            "Iemand vroeg om het wachtwoord van je Rudi-testaccount opnieuw in te stellen. "
            "Klik hieronder om een nieuw wachtwoord te kiezen. De link werkt één keer en "
            "vervalt na {hours} uur."
        ),
        "reset_cta": "Nieuw wachtwoord kiezen",
        "reset_footer": (
            "Heb je dit niet gevraagd? Negeer deze mail, je wachtwoord blijft ongewijzigd. "
            "Vragen: {support}"
        ),
        "fallback": "Werkt de knop niet? Plak dan dit adres in je browser:",
        "signoff": "Het Rudi-testteam",
    },
}


def t(key: str, locale: str, **kw: Any) -> str:
    table = STRINGS.get(locale) or STRINGS["en"]
    return table.get(key, STRINGS["en"].get(key, key)).format(**kw)


# --------------------------------------------------------------------------- rendering
def _html(locale: str, heading: str, body: str, cta: str, link: str, footer: str) -> str:
    """Brand-coloured, table-free, inline-styled — the shape mail clients actually render.

    Everything interpolated is escaped: a tester's own first name reaches this template, and a
    name is untrusted input like any other.
    """
    navy, accent = "#253e7f", "#eb23c5"
    safe_link = html.escape(link, quote=True)
    return (
        '<div style="margin:0;padding:24px;background:#efefef;'
        'font-family:Nunito,Segoe UI,Arial,sans-serif;color:#253e7f;line-height:1.5">'
        '<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;padding:28px">'
        '<div style="width:48px;height:48px;border-radius:50%%;background:%s;color:#fff;'
        'text-align:center;line-height:48px;font-weight:800;font-size:20px">R</div>'
        '<h1 style="font-size:20px;margin:18px 0 10px">%s</h1>'
        '<p style="margin:0 0 18px;font-size:15px">%s</p>'
        '<p style="margin:0 0 20px"><a href="%s" style="display:inline-block;background:%s;'
        'color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:12px 22px;'
        'border-radius:8px">%s</a></p>'
        '<p style="margin:0 0 6px;font-size:12px;color:#6b7280">%s</p>'
        '<p style="margin:0 0 20px;font-size:12px;word-break:break-all">'
        '<a href="%s" style="color:%s">%s</a></p>'
        '<p style="margin:0 0 4px;font-size:12px;color:#6b7280">%s</p>'
        '<p style="margin:0;font-size:13px;color:#6b7280">%s</p>'
        '</div></div>'
    ) % (navy, html.escape(heading), html.escape(body), safe_link, navy, html.escape(cta),
         html.escape(t("fallback", locale)), safe_link, accent, safe_link,
         html.escape(footer), html.escape(t("signoff", locale)))


def _text(heading: str, body: str, link: str, footer: str, locale: str) -> str:
    return "%s\n\n%s\n\n%s\n\n%s\n\n%s\n" % (
        heading, body, link, footer, t("signoff", locale))


# --------------------------------------------------------------------------- sending
def _send(ses: Any, to_addr: str, subject: str, html_body: str, text_body: str,
          locale: str, kind: str) -> bool:
    if not (MAIL_FROM and ses):
        # Unconfigured is a setup problem, not a crash. Registration still succeeds and the admin
        # pane can re-issue the link once the sender identity exists.
        print("WARN tester-mail not configured (kind=%s locale=%s)" % (kind, locale))
        return False
    kw: dict[str, Any] = {
        "Source": MAIL_FROM,
        "Destination": {"ToAddresses": [to_addr]},
        "Message": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"},
                     "Text": {"Data": text_body, "Charset": "UTF-8"}},
        },
    }
    if MAIL_REPLY_TO:
        kw["ReplyToAddresses"] = [MAIL_REPLY_TO]
    try:
        resp = ses.send_email(**kw)
    except Exception as e:  # noqa: BLE001 - a bounce or a throttle must not fail the request
        print("ERROR tester-mail send failed kind=%s locale=%s: %s" % (kind, locale, type(e).__name__))
        return False
    print("TESTER mail sent kind=%s locale=%s id=%s" % (kind, locale, resp.get("MessageId", "")))
    return True


def _deliver(ses: Any, kind: str, to_addr: str, name: str, link: str,
             locale: str, support: str) -> bool:
    loc = locale if locale in STRINGS else "en"
    heading = t("%s_heading" % kind, loc, name=name or "")
    body = t("%s_body" % kind, loc, hours=LINK_TTL_HOURS)
    cta = t("%s_cta" % kind, loc)
    footer = t("%s_footer" % kind, loc, support=support or "")
    return _send(ses, to_addr, t("%s_subject" % kind, loc),
                 _html(loc, heading, body, cta, link, footer),
                 _text(heading, body, link, footer, loc), loc, kind)


def verify_link(token: str) -> str:
    return "%s/set-password.html?t=%s" % (CONSOLE_BASE, token)


def reset_link(token: str) -> str:
    return "%s/set-password.html?t=%s&mode=reset" % (CONSOLE_BASE, token)


def send_verification(ses: Any, to_addr: str, name: str, token: str,
                      locale: str = "en", support: str = "") -> bool:
    return _deliver(ses, "verify", to_addr, name, verify_link(token), locale, support)


def send_reset(ses: Any, to_addr: str, name: str, token: str,
               locale: str = "en", support: str = "") -> bool:
    return _deliver(ses, "reset", to_addr, name, reset_link(token), locale, support)
