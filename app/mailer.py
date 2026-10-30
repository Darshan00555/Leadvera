"""SMTP delivery with retries and an email log. Works with Gmail/Google Workspace (App Password),
Microsoft 365, Zoho, Amazon SES SMTP, SendGrid SMTP, Mailgun SMTP, etc."""
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import appsettings
from .models import EmailLog

XLSX = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class MailError(Exception):
    pass


def smtp_configured(s):
    return bool(s.get("smtp_host") and s.get("smtp_from_email"))


def send(db, kind, to, subject, text, html_body, cc=(), attachments=(), run_id=None, retries=3):
    s = appsettings.get_all(db, include_secrets=True)
    if not smtp_configured(s):
        raise MailError("SMTP is not configured (Settings -> Email)")
    to = [a for a in to if a]
    if not to:
        raise MailError("No recipients configured (Settings -> Email)")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((s["smtp_from_name"] or "", s["smtp_from_email"]))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Message-ID"] = make_msgid(domain=s["smtp_from_email"].split("@")[-1])
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    for filename, data in attachments:
        msg.add_attachment(data, maintype=XLSX[0], subtype=XLSX[1], filename=filename)

    err, attempt = None, 0
    for attempt in range(1, retries + 1):
        try:
            _deliver(s, msg, list(to) + list(cc))
            err = None
            break
        except (smtplib.SMTPException, OSError) as e:
            err = f"{type(e).__name__}: {e}"
            if isinstance(e, smtplib.SMTPAuthenticationError):
                break  # retrying bad credentials only risks a lockout
            time.sleep(min(60, 5 * 2 ** attempt))
    db.add(EmailLog(run_id=run_id, kind=kind, recipients=", ".join(list(to) + list(cc)), subject=subject,
                    status="failed" if err else "sent", error=err, attempts=attempt))
    db.commit()
    if err:
        raise MailError(err)


def _deliver(s, msg, rcpts):
    host, port, sec = s["smtp_host"], int(s["smtp_port"] or 587), s.get("smtp_security", "starttls")
    ctx = ssl.create_default_context()
    if sec == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
    with server:
        server.ehlo()
        if sec == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if s.get("smtp_username"):
            server.login(s["smtp_username"], s.get("smtp_password") or "")
        server.send_message(msg, to_addrs=rcpts)
