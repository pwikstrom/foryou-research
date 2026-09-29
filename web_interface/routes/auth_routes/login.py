"""Sign-in and sign-up: login, signup (with the email-availability check), email verification and resend, and logout."""

import logging
import os
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from flask import flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from web_interface.auth import accounts, email_verification
from web_interface.auth.accounts import user_manager
from web_interface.integrations.mail_utils import (
    is_email,
    send_new_user_pending_email_async,
)
from web_interface.integrations.slack_service import get_recent_messages
from web_interface.services.admin_settings import (
    get_default_new_user_role,
    get_new_user_approval_required,
)

from ._blueprint import auth_bp

logger = logging.getLogger(__name__)


def _safe_next(target: str | None) -> str | None:
    """Restrict a ``?next=`` redirect target to same-site relative paths.

    An absolute URL (or a scheme-relative ``//host`` one) in ``next`` would let
    a crafted login link bounce a fresh session to an attacker's site.
    """
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Check if user exists first to distinguish between "Wrong password" and "Not approved"
        user_obj = user_manager.get_user(username)

        if user_obj:
            # Verify password first (Mitigates timing attacks by always checking password)
            if accounts.verify_password(user_obj.password_hash, password):
                if not user_obj.email_verified():
                    # The resend form on the login page keys off this category.
                    flash(
                        "Please verify your email address first: open the link "
                        "we emailed you when you signed up.",
                        "unverified",
                    )
                    session["unverified_username"] = user_obj.username
                elif not user_obj.approved:
                    flash("Your account is pending approval from an administrator.")
                else:
                    login_user(user_obj)
                    user_manager.update_last_login(user_obj.username)
                    # Lazy provisioning of the participant study pair: most
                    # donation-linked accounts never log in, so the pair is
                    # created on first login rather than for every owner.
                    # Idempotent and cheap; must run AFTER update_last_login
                    # (the dormancy gate reads it) and never fails the login.
                    from web_interface.services.participant_studies import ensure_on_login

                    ensure_on_login(user_obj.username)
                    session["login_time"] = datetime.now(timezone.utc).isoformat()
                    next_page = _safe_next(request.args.get("next"))
                    return redirect(next_page or url_for("index"))
            else:
                flash("Invalid username or password")
        else:
            # Timing attack mitigation: Perform dummy hash check
            # Use a dummy hash (random but consistent format)
            dummy_hash = (
                "77d9c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6" + "a" * 128
            )
            accounts.verify_password(dummy_hash, "dummy_password")
            flash("Invalid username or password")

    slack_configured = bool(os.environ.get("SLACK_BOT_TOKEN"))
    slack_messages = get_recent_messages() if slack_configured else []
    return render_template(
        "login.html", slack_messages=slack_messages, slack_configured=slack_configured
    )


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    # Kept through the whole signup → login round trip so a funnel entry
    # (e.g. the participation wizard's "?next=/participate/go-upload") still
    # lands where it intended after the account exists.
    next_target = _safe_next(request.form.get("next") or request.args.get("next"))

    if request.method == "POST":
        username = request.form.get("username")
        display_username = request.form.get("display_username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if not request.form.get("accept_terms"):
            flash("Please accept the terms of use to create an account")
            return render_template("signup.html", next_target=next_target)

        if password != confirm_password:
            flash("Passwords do not match")
            return render_template("signup.html", next_target=next_target)

        try:
            validate_email(username, check_deliverability=False)
        except EmailNotValidError as e:
            flash(f"Invalid email: {e!s}")
            return render_template("signup.html", next_target=next_target)

        cleaned_display, display_err = accounts.validate_display_username(display_username)
        if display_err:
            flash(display_err)
            return render_template("signup.html", next_target=next_target)

        # Admin-controlled flag (UI-toggleable, persisted in admin_settings.json).
        require_approval = get_new_user_approval_required()

        # If approval is required, approved=False. If not required, approved=True.
        is_approved = not require_approval

        # A participant account created from donation data has no password.
        # Signing up with that email claims it — the profile and linked
        # collections stay, the person gains a login — instead of bouncing
        # on "User already exists".
        terms_accepted_at = datetime.now(timezone.utc).isoformat()

        existing = user_manager.find_user_by_email(username)
        if existing is not None and existing.can_login() and not existing.email_verified():
            # The address already signed up but never opened its link (a
            # lost email, most often). Nothing about the account changes —
            # the password typed here is ignored — it just gets a new link.
            email_verification.send_verification_link(user_manager, existing, next_target)
            flash(VERIFY_FLASH)
            return redirect(
                url_for("auth_bp.login", next=next_target)
                if next_target
                else url_for("auth_bp.login")
            )
        if existing is not None and not existing.can_login() and not existing.placeholder:
            success, msg = user_manager.claim_participant_account(
                existing.username,
                password,
                cleaned_display,
                approved=is_approved,
                terms_accepted_at=terms_accepted_at,
            )
            username = existing.username
        else:
            success, msg = user_manager.add_user(
                username,
                password,
                get_default_new_user_role(),
                approved=is_approved,
                display_username=cleaned_display,
                origin={"source": "signup", "at": datetime.now(timezone.utc).isoformat()},
                terms_accepted_at=terms_accepted_at,
            )
        if success:
            if next_target and next_target.startswith("/participate"):
                # Funnel-origin signup: queue the guided tour for the first
                # visit to the app shell (index.html checks this setting).
                user_manager.update_user_settings(username, {"hub_tour_pending": True})
            skip = email_verification.skip_reason()
            if skip is None:
                # The address must be proven before this account can log in.
                # When approval gating is also on, the admin hears about the
                # account from the verify route, not from here — an address
                # nobody can open should never reach the approval list.
                new_user = user_manager.get_user(username)
                email_verification.send_verification_link(user_manager, new_user, next_target)
                flash(VERIFY_FLASH)
            else:
                if skip == accounts.EMAIL_VERIFIED_MAIL_UNCONFIGURED:
                    logger.warning(
                        f"Signup {username} admitted WITHOUT email verification: "
                        f"outgoing mail is not configured (MAIL_PASSWORD / mail sender)."
                    )
                user_manager.mark_email_verified(username, via=skip)
                if is_approved:
                    flash("Account created! You can now login.")
                else:
                    flash(
                        "Account created! Please wait for an administrator to approve your account."
                    )
                    # Approval gating is on: email the oldest admin so they know a
                    # request is waiting, and stamp the pending user once it sends.
                    _notify_admin_of_pending_signup(username, cleaned_display)
            return redirect(
                url_for("auth_bp.login", next=next_target)
                if next_target
                else url_for("auth_bp.login")
            )
        else:
            flash(msg)

    return render_template("signup.html", next_target=next_target)


@auth_bp.route("/api/signup/email-check")
def api_signup_email_check():
    """Tell the signup form whether an email can still register.

    Called on blur of the email field so a duplicate is caught before the
    visitor fills in the rest of the form. Three answers: ``available``
    (no account), ``claimable`` (a passwordless participant account exists and
    signing up will claim it, keeping its linked collections), ``taken``
    (an account that can already log in).
    """
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"status": "available"})
    existing = user_manager.find_user_by_email(email)
    if existing is None:
        return jsonify({"status": "available"})
    if not existing.can_login() and not existing.placeholder:
        return jsonify({"status": "claimable"})
    if existing.can_login() and not existing.email_verified():
        return jsonify({"status": "unverified"})
    return jsonify({"status": "taken"})


@auth_bp.route("/verify-email/<token>")
def verify_email(token):
    """Open a signup verification link: prove the address, then gate onward.

    A bad, expired or superseded token lands on the login page with a hint
    to request a new link. A good one stamps the account and, when approval
    gating is on, THIS is where the admin is told a request is waiting.
    """
    parsed = email_verification.parse_token(token, user_manager.get_user)
    if parsed is None:
        flash("That verification link is invalid or has expired. Log in to request a new one.")
        return redirect(url_for("auth_bp.login"))
    user, next_target = parsed
    next_target = _safe_next(next_target)
    if not user.email_verified():
        user_manager.mark_email_verified(user.username, via=accounts.EMAIL_VERIFIED_LINK)
        if not user.approved:
            _notify_admin_of_pending_signup(user.username, user.display_username or None)
    if user.approved:
        flash("Email verified! You can now log in.")
    else:
        flash("Email verified! Please wait for an administrator to approve your account.")
    return redirect(
        url_for("auth_bp.login", next=next_target) if next_target else url_for("auth_bp.login")
    )


@auth_bp.route("/verify-email/resend", methods=["POST"])
def resend_verification():
    """Send a fresh verification link to an unverified account.

    Always answers with the same neutral message, whatever the address, so
    the form cannot be used to learn which emails hold accounts.
    """
    username = (request.form.get("username") or session.get("unverified_username") or "").strip()
    user = user_manager.find_user_by_email(username) if username else None
    if user is not None and user.can_login() and not user.email_verified():
        email_verification.send_verification_link(user_manager, user)
    flash("If that address has an unverified account, a new verification link is on its way.")
    return redirect(url_for("auth_bp.login"))


VERIFY_FLASH = (
    "Account created! Check your inbox for a verification link. "
    "You need to open it before you can log in."
)


def _notify_admin_of_pending_signup(new_username: str, new_display: str | None) -> None:
    """Email the oldest admin that ``new_username`` is awaiting approval.

    Fire-and-forget: the send runs in a background thread so signup stays
    responsive, and the sent-at / sent-to marker is recorded on the pending user
    only when the email actually goes out (accurate even when MAIL_PASSWORD is
    unset in local dev). A no-op if no emailable admin exists.

    Args:
        new_username: Email (account id) of the just-created pending user.
        new_display: The new user's chosen display name, if any.
    """
    admin = user_manager.get_oldest_admin()
    if admin is None or not is_email(admin.username):
        return
    admin_email = admin.username
    send_new_user_pending_email_async(
        to_email=admin_email,
        new_user_email=new_username,
        new_user_display=new_display,
        on_success=lambda: user_manager.record_approval_notification(
            new_username, sent_to=admin_email
        ),
    )


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    # Land on the home page: for a now-anonymous visitor, index() renders the
    # public landing page, which has the Log in item in its top menu.
    return redirect(url_for("index"))
