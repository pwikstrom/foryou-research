import logging
import os
import re
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_email(value: str) -> bool:
    """Light sanity check that a username is an emailable address."""
    return bool(EMAIL_RE.match(str(value or "")))


def _site() -> dict:
    """Return the [site] config section (instance branding), never raising."""
    from fyp.fyp_config import get_config
    try:
        return get_config().get("site", {}) or {}
    except Exception:
        return {}


def _app_link() -> str:
    """HTML link to this instance ([site].app_url), or a neutral phrase."""
    app_url = str(_site().get("app_url", "") or "").strip()
    return f'<a href="{app_url}">{app_url}</a>' if app_url else "the Data Hub"


def _mail_credentials(context: str):
    """Return (sender, password) when mail is configured, else None.

    The sender comes from [site].mail_sender (settable via config.local.toml
    or the FYP_MAIL_SENDER env var); the password from MAIL_PASSWORD.

    Args:
        context: Short label for the warning log (e.g. "welcome email").
    """
    password = os.environ.get("MAIL_PASSWORD")
    if not password:
        logger.warning(f"MAIL_PASSWORD not set. Cannot send {context}.")
        return None
    sender = str(_site().get("mail_sender", "") or "").strip()
    if not sender:
        logger.warning(f"Mail sender not configured ([site].mail_sender / "
                       f"FYP_MAIL_SENDER). Cannot send {context}.")
        return None
    return sender, password

def send_welcome_email_async(to_email):
    """Sends a welcome email in a background thread."""
    thread = threading.Thread(target=send_welcome_email, args=(to_email,))
    thread.start()

def send_welcome_email(to_email):
    """Sends a welcome email to the approved user."""
    creds = _mail_credentials("welcome email")
    if not creds:
        return
    sender, password = creds

    try:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = to_email
        msg["Subject"] = "Welcome to the For You Data Hub"

        body = f"""
        <html>
          <body>
            <h2>Welcome!</h2>
            <p>Your account (<b>{to_email}</b>) has been approved to access the For You Data Hub.</p>
            <p>You can now log in at {_app_link()}.</p>
            <br>
            <p>Best regards,<br>The Data Hub team</p>
          </body>
        </html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, to_email, msg.as_string())

        logger.info(f"Welcome email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send welcome email to {to_email}: {e}")
        return False


def send_invitation_email_async(to_email, run_id, task_type, inviter,
                                n_items, n_variables, on_success=None):
    """Send a human-task invitation email in a background thread.

    Args:
        on_success: optional zero-arg callable invoked only when the send
            actually succeeded (used to persist the coder's ``notified`` flag).
    """
    def _worker():
        if send_invitation_email(to_email, run_id, task_type, inviter,
                                 n_items, n_variables) and on_success:
            try:
                on_success()
            except Exception as e:
                logger.error(f"Invitation on_success callback failed: {e}")

    thread = threading.Thread(target=_worker)
    thread.start()


def send_invitation_email(to_email, run_id, task_type, inviter,
                          n_items, n_variables) -> bool:
    """Send an invitation to contribute human input to an annotation test run.

    Returns:
        True when the email was actually sent; False when MAIL_PASSWORD is
        unset (local dev) or the send failed.
    """
    creds = _mail_credentials("invitation email")
    if not creds:
        return False
    sender, password = creds

    task_label = ("preference-vote" if task_type == "vote" else "blind coding")
    try:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = to_email
        msg["Subject"] = "Invitation: help evaluate video annotations on the For You Data Hub"

        body = f"""
        <html>
          <body>
            <h2>You have been invited to a {task_label} task</h2>
            <p><b>{inviter}</b> invited you (<b>{to_email}</b>) to contribute human
               input to an annotation test run on the For You Data Hub:
               {n_items} videos, {n_variables} variables.</p>
            <p>Log in at {_app_link()}
               and open <b>My stuff &rarr; My Tasks</b> to start. Your work is saved
               automatically, so you can pause and come back at any time.</p>
            <br>
            <p>Best regards,<br>The Data Hub team</p>
          </body>
        </html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, to_email, msg.as_string())

        logger.info(f"Invitation email sent to {to_email} for {run_id}/{task_type}")
        return True
    except Exception as e:
        logger.error(f"Failed to send invitation email to {to_email}: {e}")
        return False


def send_new_user_pending_email(to_email, new_user_email, new_user_display=None) -> bool:
    """Notify an admin that a new user signed up and is awaiting approval.

    Sent to the single administrator chosen by the caller (the oldest admin
    account) only when new-user approval gating is enabled.

    Args:
        to_email: The admin's email address.
        new_user_email: The email (account id) of the user awaiting approval.
        new_user_display: The new user's chosen display name, if any.

    Returns:
        True when actually sent; False on no-op (MAIL_PASSWORD unset) or failure.
        Never raises.
    """
    display = f" ({new_user_display})" if new_user_display else ""
    subject = "New user pending approval — For You Data Hub"
    body = f"""
    <html>
      <body>
        <h2>A new user is awaiting approval</h2>
        <p><b>{new_user_email}</b>{display} has requested an account on the
           For You Data Hub and is pending administrator approval.</p>
        <p>Log in at {_app_link()}
           and open <b>Admin &rarr; New Users</b> to review and approve.</p>
        <br>
        <p>Best regards,<br>The Data Hub team</p>
      </body>
    </html>
    """
    return _send_html_email(to_email, subject, body)


def send_new_user_pending_email_async(to_email, new_user_email,
                                      new_user_display=None, on_success=None):
    """Send a new-user pending-approval notification in a background thread.

    Args:
        on_success: optional zero-arg callable invoked only when the send
            actually succeeded (used to persist the sent-at / sent-to marker on
            the pending user).
    """
    def _worker():
        if send_new_user_pending_email(to_email, new_user_email, new_user_display) and on_success:
            try:
                on_success()
            except Exception as e:
                logger.error(f"New-user-pending on_success callback failed: {e}")

    thread = threading.Thread(target=_worker)
    thread.start()


def _send_html_email(to_email, subject, body_html) -> bool:
    """Send one HTML email via the shared Gmail SMTP transport.

    Returns:
        True when the email was actually sent; False when MAIL_PASSWORD is
        unset (local dev) or the send failed. Never raises.
    """
    creds = _mail_credentials("email")
    if not creds:
        return False
    sender, password = creds
    try:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, to_email, msg.as_string())

        logger.info(f"Email '{subject}' sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email '{subject}' to {to_email}: {e}")
        return False


def _batch_annotation_email_content(kind: str, details: dict) -> tuple[str, str]:
    """Build the (subject, html_body) for a batch-annotation notification.

    Args:
        kind: One of ``submitted`` / ``batch_done`` / ``completed`` / ``failed``.
        details: Event-specific counts (n_items, ok, fail, requeued, remaining,
            total_ok, total_fail, chunk_index, job_name, error).
    """
    n_items = details.get("n_items")
    remaining = details.get("remaining")
    intro = {
        "submitted": "Your async annotation job has been submitted",
        "batch_done": "A batch of your async annotation job has completed",
        "completed": "Your async annotation job has finished",
        "failed": "Your async annotation job stopped with an error",
    }.get(kind, "Async annotation update")

    if kind == "submitted":
        subject = "Async annotation started"
        detail = (f"<p><b>{n_items:,}</b> videos were submitted to Google's Gemini "
                  f"batch service. Results typically arrive within a few hours; "
                  f"you'll get another email when they're in.</p>")
    elif kind == "batch_done":
        subject = "Async annotation — batch completed"
        detail = (f"<p>Batch ingested: <b>{details.get('ok', 0):,}</b> annotated, "
                  f"<b>{details.get('fail', 0):,}</b> failed, "
                  f"<b>{details.get('requeued', 0):,}</b> re-queued. "
                  f"<b>{remaining:,}</b> still queued.</p>"
                  if remaining is not None else
                  f"<p>Batch ingested: <b>{details.get('ok', 0):,}</b> annotated, "
                  f"<b>{details.get('fail', 0):,}</b> failed.</p>")
    elif kind == "completed":
        subject = "Async annotation complete"
        detail = (f"<p>All done. Total this run: "
                  f"<b>{details.get('total_ok', 0):,}</b> annotated, "
                  f"<b>{details.get('total_fail', 0):,}</b> failed.</p>")
    else:  # failed
        subject = "Async annotation failed"
        err = details.get("error", "unknown error")
        detail = (f"<p>The job stopped before finishing: <code>{err}</code></p>"
                  f"<p>Any reserved videos were returned to the queue and can be "
                  f"re-run.</p>")

    body = f"""
    <html>
      <body>
        <h2>{intro}</h2>
        {detail}
        <p>Open <b>Data Management &rarr; Scrape &amp; Annotate</b> on
           {_app_link()} to see the live log.</p>
        <br>
        <p>Best regards,<br>The Data Hub team</p>
      </body>
    </html>
    """
    return subject, body


def send_batch_annotation_email(to_email, kind, **details) -> bool:
    """Notify the launcher of a batch-annotation milestone.

    Args:
        to_email: The launching user's email (their username).
        kind: ``submitted`` / ``batch_done`` / ``completed`` / ``failed``.
        **details: Event-specific counts (see ``_batch_annotation_email_content``).

    Returns:
        True when actually sent; False on no-op (MAIL_PASSWORD unset), invalid
        address, or send failure. Never raises.
    """
    if not is_email(to_email):
        return False
    subject, body = _batch_annotation_email_content(kind, details)
    return _send_html_email(to_email, subject, body)


def send_batch_annotation_email_async(to_email, kind, **details) -> None:
    """Send a batch-annotation notification in a background thread."""
    thread = threading.Thread(
        target=send_batch_annotation_email, args=(to_email, kind), kwargs=details
    )
    thread.start()
