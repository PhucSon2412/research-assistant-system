"""One SMTP send in a child process owned by ``SmtpDelivery`` (M8a, PR #66 fix).

The parent enforces the single deadline for the whole send (DNS, connect,
greeting, TLS, EHLO, AUTH, MAIL/RCPT/DATA and every reply) by killing and
reaping this process; socket timeouts here only make the child give up early
on its own. The job, including the SMTP password, arrives on stdin as one
JSON document; it never appears in argv, environment, files or output. The
only output is ``{"result": "ok"}`` or ``{"result": "<code>"}`` on stdout:
no exception text, reply, recipient or link is ever printed.

Standard library only, so the child starts fast and imports nothing else of
the application.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import smtplib
import socket
import ssl
import sys
import time

MAX_JOB_BYTES = 1 << 20


def _context(ca_file: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def transmit(job: dict) -> str:
    """Return ``"ok"`` once DATA is accepted, otherwise a stable error code."""
    deadline = time.monotonic() + float(job["total_timeout_seconds"])

    class Late(Exception):
        pass

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise Late
        return left

    try:
        context = _context(job.get("ca_file"))
    except (OSError, ssl.SSLError, ValueError):
        return "smtp_ca_unavailable"
    try:
        msg = email.message_from_bytes(base64.b64decode(job["message"]), policy=email.policy.SMTP)
    except (ValueError, TypeError):
        return "smtp_invalid_message"
    client = None
    try:
        connect = min(float(job["connect_timeout_seconds"]), remaining())
        if job["security"] == "implicit_tls":
            client = smtplib.SMTP_SSL(job["host"], int(job["port"]), local_hostname=job.get("helo_name"),
                                      timeout=connect, context=context)
        elif job["security"] == "starttls":
            client = smtplib.SMTP(job["host"], int(job["port"]), local_hostname=job.get("helo_name"), timeout=connect)
        else:  # never plaintext
            return "smtp_failed"

        def step() -> None:
            client.sock.settimeout(remaining())

        step()
        client.ehlo()
        if job["security"] == "starttls":
            if not client.has_extn("starttls"):
                return "smtp_starttls_unavailable"
            step()
            code, _ = client.starttls(context=context)
            if code != 220:
                return "smtp_starttls_unavailable"
            step()
            client.ehlo()
        if job.get("username"):
            step()
            client.login(job["username"], job["password"])
        step()
        refused = client.send_message(msg, from_addr=job["sender"], to_addrs=[job["recipient"]])
        if refused:
            return "smtp_rejected"
        try:  # DATA was accepted; a failed QUIT does not undo that
            client.sock.settimeout(max(0.1, min(1.0, deadline - time.monotonic())))
            client.quit()
        except Exception:  # noqa: BLE001
            pass
        return "ok"
    except Late:
        return "smtp_timeout"
    except ssl.SSLCertVerificationError:
        return "smtp_tls_verify_failed"
    except ssl.SSLError:
        return "smtp_tls_failed"
    except (socket.timeout, TimeoutError):
        return "smtp_timeout"
    except smtplib.SMTPAuthenticationError:
        return "smtp_auth_failed"
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
            smtplib.SMTPNotSupportedError, smtplib.SMTPHeloError):
        return "smtp_rejected"
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
        # smtplib turns a socket timeout while reading a reply into a disconnect.
        return "smtp_timeout" if isinstance(exc.__context__, (socket.timeout, TimeoutError)) else "smtp_connection_failed"
    except smtplib.SMTPException:  # before OSError: SMTPException derives from it
        return "smtp_failed"
    except OSError:
        return "smtp_connection_failed"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_JOB_BYTES + 1)
    try:
        if len(raw) > MAX_JOB_BYTES:
            raise ValueError
        job = json.loads(raw)
        result = transmit(job)
    except (ValueError, KeyError, TypeError):
        result = "smtp_failed"
    sys.stdout.write(json.dumps({"result": result}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
