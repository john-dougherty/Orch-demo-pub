"""SMTP-based outbound email sender.

Config-gated: if SMTP_HOST/SMTP_USER/SMTP_PASSWORD are all set, emails go
out for real. Otherwise `send()` is a no-op that returns ok=True so the
existing SendEmail tool path stays unbroken.

Demo redirect: if EMAIL_DEMO_REDIRECT is set, every outbound email is
delivered to that address instead of the drafted to_address. A banner is
prepended to the body so it's clear the delivery was demo-routed.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from hermes.config import settings

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    ok: bool
    delivered: bool  # False when stubbed (SMTP not configured)
    message_id: str | None = None
    redirected_to: str | None = None
    error: str | None = None


class EmailSender:
    def __init__(self) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._user = settings.smtp_user
        self._password = settings.smtp_password
        self._from_address = settings.smtp_from_address or settings.smtp_user
        self._from_name = settings.smtp_from_name
        self._demo_redirect = settings.email_demo_redirect

    @property
    def configured(self) -> bool:
        return bool(self._host and self._user and self._password)

    def send(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
    ) -> SendResult:
        # Stub path — keep the demo working without any SMTP creds.
        if not self.configured:
            log.info("SMTP not configured; stubbing send to %s", to_address)
            return SendResult(ok=True, delivered=False, message_id=None)

        actual_to = self._demo_redirect or to_address
        redirected = bool(self._demo_redirect and self._demo_redirect != to_address)

        final_subject = f"[DEMO] {subject}" if redirected else subject
        final_body = (
            f"[DEMO — this email was originally addressed to: {to_address}]\n\n{body}"
            if redirected
            else body
        )

        msg = EmailMessage()
        msg["From"] = formataddr((self._from_name or "", self._from_address))
        msg["To"] = actual_to
        msg["Subject"] = final_subject
        # Keeping a "Reply-To" pointing at the sender inbox keeps replies
        # visible in the demo inbox instead of bouncing off fake addresses.
        msg["Reply-To"] = self._from_address
        msg["Message-ID"] = make_msgid(domain=self._from_address.split("@", 1)[-1] or "localhost")
        msg.set_content(final_body)

        try:
            with smtplib.SMTP(self._host, self._port, timeout=15) as s:
                s.starttls()
                s.login(self._user, self._password)
                s.send_message(msg)
        except Exception as e:
            log.exception("SMTP send failed")
            return SendResult(ok=False, delivered=False, error=str(e))

        return SendResult(
            ok=True,
            delivered=True,
            message_id=msg["Message-ID"],
            redirected_to=actual_to if redirected else None,
        )


email_sender = EmailSender()
