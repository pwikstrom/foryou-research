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
SENDER_EMAIL = "info@foryouresearch.net"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_email(value: str) -> bool:
    """Light sanity check that a username is an emailable address."""
    return bool(EMAIL_RE.match(str(value or "")))

def send_welcome_email_async(to_email):
    """Sends a welcome email in a background thread."""
    thread = threading.Thread(target=send_welcome_email, args=(to_email,))
    thread.start()

def send_welcome_email(to_email):
    """Sends a welcome email to the approved user."""
    password = os.environ.get("MAIL_PASSWORD")
    if not password:
        logger.warning("MAIL_PASSWORD not set. Cannot send welcome email.")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = to_email
        msg["Subject"] = "Welcome to ForYouResearch Data Hub"

        body = f"""
        <html>
          <body>
            <h2>Welcome!</h2>
            <p>Your account (<b>{to_email}</b>) has been approved to access the ForYouResearch Data Hub.</p>
            <p>You can now log in at <a href="http://foryouresearch.net">foryouresearch.net</a> (or your provided URL).</p>
            <br>
            <p>Best regards,<br>The ForYouResearch Team</p>
          </body>
        </html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, password)
            server.sendmail(SENDER_EMAIL, to_email, msg.as_string())
        
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
    password = os.environ.get("MAIL_PASSWORD")
    if not password:
        logger.warning("MAIL_PASSWORD not set. Cannot send invitation email.")
        return False

    task_label = ("preference-vote" if task_type == "vote" else "blind coding")
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = to_email
        msg["Subject"] = "Invitation: help evaluate video annotations on the For You Data Hub"

        body = f"""
        <html>
          <body>
            <h2>You have been invited to a {task_label} task</h2>
            <p><b>{inviter}</b> invited you (<b>{to_email}</b>) to contribute human
               input to an annotation test run on the ForYouResearch Data Hub:
               {n_items} videos, {n_variables} variables.</p>
            <p>Log in at <a href="http://foryouresearch.net">foryouresearch.net</a>
               and open <b>My stuff &rarr; My Tasks</b> to start. Your work is saved
               automatically, so you can pause and come back at any time.</p>
            <br>
            <p>Best regards,<br>The ForYouResearch Team</p>
          </body>
        </html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, password)
            server.sendmail(SENDER_EMAIL, to_email, msg.as_string())

        logger.info(f"Invitation email sent to {to_email} for {run_id}/{task_type}")
        return True
    except Exception as e:
        logger.error(f"Failed to send invitation email to {to_email}: {e}")
        return False
