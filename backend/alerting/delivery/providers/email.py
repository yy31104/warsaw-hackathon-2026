import asyncio
from email.message import EmailMessage
import os
import smtplib

from alerting.contracts import Channel, SendResult


class Email:
    channel = Channel.email

    async def send(self, msg, contact, idempotency_key):
        def transmit():
            mail = EmailMessage()
            mail["From"] = "alerts@localhost"
            mail["To"] = contact
            mail["Subject"] = " ".join(msg.title.splitlines())
            mail["X-Notification-Id"] = idempotency_key
            if msg.meta.get("gen_sent_at") is not None:
                mail["X-Gen-Sent-At"] = str(msg.meta["gen_sent_at"])
            mail.set_content("\n\n".join(filter(None, [msg.body, msg.runbook_excerpt, msg.link])))
            with smtplib.SMTP(os.getenv("SMTP_HOST", "localhost"), int(os.getenv("SMTP_PORT", "1025")), timeout=3) as smtp:
                smtp.send_message(mail)
        try:
            await asyncio.to_thread(transmit)
        except (TimeoutError, smtplib.SMTPServerDisconnected) as exc:
            return SendResult(outcome="unknown", error=str(exc))
        except smtplib.SMTPResponseException as exc:
            return SendResult(outcome="retryable" if exc.smtp_code < 500 else "permanent", error=str(exc))
        except (smtplib.SMTPRecipientsRefused, ValueError) as exc:
            return SendResult(outcome="permanent", error=str(exc))
        except (OSError, smtplib.SMTPException) as exc:
            return SendResult(outcome="retryable", error=str(exc))
        return SendResult(outcome="delivered", provider_ref=idempotency_key)
