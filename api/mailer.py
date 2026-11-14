import logging
import smtplib
from email.message import EmailMessage

import httpx

from . import config

log = logging.getLogger("GitDrive")


def send_mail(to: str, subject: str, text: str, html: str) -> bool:
    """Best effort. Returns True when a provider accepted the message."""
    try:
        if config.EMAIL_PROVIDER == "resend" and config.RESEND_API_KEY:
            r = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                json={"from": config.EMAIL_FROM, "to": [to], "subject": subject, "text": text, "html": html},
                timeout=20,
            )
            if r.status_code >= 300:
                log.error("MAIL resend failed: %s %s", r.status_code, r.text[:200])
                return False
            return True
        if config.EMAIL_PROVIDER == "smtp" and config.SMTP_USER:
            msg = EmailMessage()
            msg["From"] = config.EMAIL_FROM if "<" in config.EMAIL_FROM and "resend.dev" not in config.EMAIL_FROM else f"GitDrive <{config.SMTP_USER}>"
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(text)
            msg.add_alternative(html, subtype="html")
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
                s.starttls()
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
            return True
    except Exception as exc:  # never let mail problems break the request
        log.error("MAIL failed: %s", exc)
        return False
    log.warning("MAIL provider not configured; not sending %r to %s", subject, to)
    return False
