"""Signup email verification: signed links, the effective policy, sending.

A self-service signup receives a link carrying a signed, time-limited token.
Opening it proves the mailbox exists and is the signer-upper's, and only then
may the account log in (and, when approval gating is on, only then is the
admin told about it).

The token is stateless — ``itsdangerous.URLSafeTimedSerializer`` keyed on the
Flask secret — so there is no token store to keep or prune. Its payload binds
the username to a fragment of the current password hash, which retires every
earlier link the moment the password changes or the account is re-claimed.
"""

import hashlib
import logging
from datetime import UTC, datetime

from flask import current_app, url_for
from itsdangerous import BadData, URLSafeTimedSerializer

from .admin_settings import get_signup_email_verification_required
from .mail_utils import mail_configured, send_verification_email_async

logger = logging.getLogger(__name__)

TOKEN_SALT = "email-verify"
TOKEN_MAX_AGE_S = 48 * 3600
TOKEN_MAX_AGE_HOURS = TOKEN_MAX_AGE_S // 3600
# A second link is not sent within this many seconds of the previous one, so
# a double-click or a re-signup burst cannot turn the sender into a spammer.
RESEND_COOLDOWN_S = 60


def verification_required() -> bool:
    """Whether a signup right now must verify its address before logging in.

    True only when the admin setting is on AND outgoing mail is configured;
    a link nobody can send must not gate anyone.
    """
    return get_signup_email_verification_required() and mail_configured()


def skip_reason() -> str | None:
    """Why verification is being skipped for signups right now, or None.

    Returns one of the ``EMAIL_VERIFIED_*`` values that
    :func:`verification_required` would stamp instead of gating:
    ``"setting_off"`` or ``"mail_unconfigured"``.
    """
    from .auth import EMAIL_VERIFIED_MAIL_UNCONFIGURED, EMAIL_VERIFIED_SETTING_OFF
    if not get_signup_email_verification_required():
        return EMAIL_VERIFIED_SETTING_OFF
    if not mail_configured():
        return EMAIL_VERIFIED_MAIL_UNCONFIGURED
    return None


def _hash_fragment(password_hash) -> str:
    return hashlib.sha256(str(password_hash or "").encode("utf-8")).hexdigest()[:16]


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=TOKEN_SALT)


def make_token(user, next_target: str | None = None) -> str:
    """Sign a verification token for ``user`` (a :class:`auth.User`)."""
    payload = {"u": user.username, "h": _hash_fragment(user.password_hash)}
    if next_target:
        payload["n"] = next_target
    return _serializer().dumps(payload)


def parse_token(token: str, get_user):
    """Validate ``token`` and return ``(user, next_target)`` or ``None``.

    Args:
        token: The value from the verify URL.
        get_user: Callable mapping a username to a :class:`auth.User` or None
            (``user_manager.get_user``).

    Returns:
        None when the signature is bad, the token has expired, the account no
        longer exists, or the password has changed since the link was made.
    """
    try:
        payload = _serializer().loads(token, max_age=TOKEN_MAX_AGE_S)
    except BadData:
        return None
    if not isinstance(payload, dict):
        return None
    user = get_user(payload.get("u"))
    if user is None:
        return None
    if payload.get("h") != _hash_fragment(user.password_hash):
        return None
    return user, payload.get("n")


def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()


def send_verification_link(user_manager, user, next_target: str | None = None,
                           force: bool = False) -> bool:
    """Email ``user`` a fresh verification link (background thread).

    Honours :data:`RESEND_COOLDOWN_S` unless ``force`` (an admin's explicit
    resend). The ``email_verification_sent_at`` stamp is written only when
    the send actually succeeds.

    Returns:
        True when a send was started, False when the cooldown suppressed it.
    """
    since = _seconds_since(user.email_verification_sent_at)
    if not force and since is not None and since < RESEND_COOLDOWN_S:
        logger.info(f"Verification link to {user.username} suppressed by cooldown ({since:.0f}s)")
        return False
    verify_url = _absolute_verify_url(make_token(user, next_target))
    send_verification_email_async(
        to_email=user.username,
        verify_url=verify_url,
        expires_hours=TOKEN_MAX_AGE_HOURS,
        on_success=lambda: user_manager.record_verification_sent(user.username),
    )
    return True


def _absolute_verify_url(token: str) -> str:
    """The verify route as an absolute URL.

    Prefers ``[site].app_url`` (the public hostname; on Cloud Run the request
    host may be the internal run.app one) and falls back to the request host.
    """
    from .mail_utils import _site
    path = url_for("auth_bp.verify_email", token=token)
    app_url = str(_site().get("app_url", "") or "").strip().rstrip("/")
    if app_url:
        return f"{app_url}{path}"
    return url_for("auth_bp.verify_email", token=token, _external=True)
