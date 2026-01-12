import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import logging
import threading

logger = logging.getLogger(__name__)

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "info@foryouresearch.net"

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
