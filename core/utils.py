"""
Utility functions for SUG E-Voting Platform.
"""
import string
import hashlib
import json
import secrets
import logging
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.core.mail import send_mail
import requests

from .models import AuditLog, OTPVerification

logger = logging.getLogger("core")


# ============================================================================
# OTP Generation
# ============================================================================
def generate_otp(length=6):
    """Generate a numeric OTP of specified length."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def create_otp_verification(voter, purpose):
    """
    Create a new OTP verification record.
    Returns the plaintext OTP (for immediate sending) and the OTPVerification object.
    """
    code = generate_otp(settings.OTP_LENGTH)
    code_hash = OTPVerification.hash_code(code)

    # Determine delivery method based on strict-match rule
    # Phone is primary anchor; email is fallback
    delivery_method = "EMAIL"
    try:
        from .models import VerifiedVoterRecord

        vvr = VerifiedVoterRecord.objects.filter(
            matric_number=voter.matric_number
        ).first()
        if vvr and vvr.official_phone:
            delivery_method = "SMS"
    except Exception:
        # Fallback to email if no verified record found (shouldn't happen in normal flow)
        delivery_method = "EMAIL"

    otp = OTPVerification.objects.create(
        voter=voter,
        purpose=purpose,
        delivery_method=delivery_method,
        code_hash=code_hash,
        expires_at=timezone.now() + timezone.timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
    )

    return code, otp


# ============================================================================
# SMS Delivery (Termii or equivalent)
# ============================================================================
def send_sms(phone_number, message):
    """Send SMS via configured provider."""
    if not settings.SMS_API_KEY:
        logger.warning("SMS API key not configured. SMS not sent.")
        return False

    try:
        payload = {
            "to": phone_number,
            "from": settings.SMS_SENDER_ID,
            "sms": message,
            "type": "plain",
            "channel": "generic",
            "api_key": settings.SMS_API_KEY,
        }
        response = requests.post(settings.SMS_API_URL, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"SMS sent to {phone_number}: {response.status_code}")
        return True
    except Exception as e:
        logger.error(f"SMS sending failed to {phone_number}: {e}")
        return False


# ============================================================================
# Email Delivery
# ============================================================================
def send_otp_email(email, otp_code, purpose):
    """Send OTP via email."""
    subject = "Your SUG E-Voting Verification Code"

    purpose_display = {
        "REGISTRATION": "account registration",
        "NEW_DEVICE_LOGIN": "new device login",
        "PASSWORD_RESET": "password reset",
    }.get(purpose, "verification")

    message = f"""Hello,

Your verification code for {purpose_display} is: {otp_code}

This code will expire in {settings.OTP_EXPIRY_MINUTES} minutes.

If you did not request this code, please ignore this email.

— SUG E-Voting Platform
"""

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        logger.info(f"OTP email sent to {email}")
        return True
    except Exception as e:
        logger.error(f"OTP email failed to {email}: {e}")
        return False


def send_otp(voter, otp_code, purpose):
    """
    Send OTP to voter via the appropriate channel.
    Returns True if at least one channel succeeded.
    """
    # Determine delivery method from the verified record
    try:
        from .models import VerifiedVoterRecord

        vvr = VerifiedVoterRecord.objects.filter(
            matric_number=voter.matric_number
        ).first()
        if vvr and vvr.official_phone:
            # Phone is primary — send SMS
            sms_sent = send_sms(voter.phone_number, f"Your SUG E-Voting code: {otp_code}. Expires in 10 mins.")
            # Also send email as backup if we have it
            email_sent = False
            if voter.email:
                email_sent = send_otp_email(voter.email, otp_code, purpose)
            return sms_sent or email_sent
        else:
            # Email is fallback / primary
            return send_otp_email(voter.email, otp_code, purpose)
    except Exception as e:
        logger.error(f"OTP delivery failed for {voter.matric_number}: {e}")
        return False


# ============================================================================
# Audit Logging
# ============================================================================
SEVERITY_INFO = "INFO"
SEVERITY_SECURITY = "SECURITY"
SEVERITY_CRITICAL = "CRITICAL"


def _canonical_metadata(metadata):
    """Stable string form of the metadata blob, used in the entry hash."""
    return json.dumps(metadata or {}, sort_keys=True, default=str)


def _compute_entry_hash(prev_hash, scope, severity, action_type, actor, election_id, metadata, description):
    payload = "|".join([
        prev_hash,
        scope,
        severity,
        action_type,
        str(actor),
        str(election_id or ""),
        _canonical_metadata(metadata),
        description,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log_action(action_type, description, actor="SYSTEM", election=None, metadata=None,
               severity=SEVERITY_INFO, critical=False):
    """
    Create an audit log entry.

    Every entry is bound into a hash chain (per election for election-scoped
    events, school-wide for the rest), so any later edit or deletion of a row
    is detectable via verify_audit_chain(). Election-scoped writers are
    serialized on the election row, keeping the chain fork-free.

    critical=True is required for security-relevant state changes recorded
    inside a transaction (vote casting, election state changes): the audit
    write then participates in that transaction, so a failure rolls the whole
    action back instead of leaving an unaudited change behind.
    """
    try:
        with transaction.atomic():
            if election is not None:
                # Serialize writers of this election's chain. Callers that
                # already hold this lock (cast_vote, transition_election_state)
                # simply re-acquire their own row lock in the same transaction.
                from .models import Election

                Election.objects.select_for_update().get(pk=election.pk)
                prev_hash = (
                    AuditLog.objects.filter(election_id=election.pk)
                    .order_by("-id")
                    .values_list("entry_hash", flat=True)
                    .first()
                    or ""
                )
                scope = f"election:{election.pk}"
                election_id = election.pk
            else:
                prev_hash = (
                    AuditLog.objects.filter(election__isnull=True)
                    .order_by("-id")
                    .values_list("entry_hash", flat=True)
                    .first()
                    or ""
                )
                scope = "school"
                election_id = None

            entry_hash = _compute_entry_hash(
                prev_hash, scope, severity, action_type, actor, election_id, metadata, description
            )
            AuditLog.objects.create(
                election=election,
                action_type=action_type,
                description=description,
                actor=str(actor),
                severity=severity,
                metadata=metadata or {},
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
        logger.info(f"AUDIT: [{severity}] [{action_type}] {actor} — {description}")
    except Exception:
        if critical:
            raise
        logger.exception("Audit log write failed for action '%s'; the action itself was not rolled back.", action_type)


def verify_audit_chain():
    """
    Re-walk every audit chain and recompute each entry hash.

    Returns a report dict: {"valid": bool, "checked": int, "issues": [
    {"id": ..., "reason": ...}, ...]}. A broken chain means an entry was
    edited or deleted after being written.
    """
    report = {"valid": True, "checked": 0, "issues": []}

    def _walk(entries, scope):
        prev_hash = ""
        for entry in entries:
            expected = _compute_entry_hash(
                prev_hash, scope, entry.severity, entry.action_type,
                entry.actor, entry.election_id, entry.metadata, entry.description,
            )
            if entry.prev_hash != prev_hash:
                report["valid"] = False
                report["issues"].append({"id": entry.id, "reason": "linked to wrong predecessor (row inserted/deleted?)"})
            elif entry.entry_hash != expected:
                report["valid"] = False
                report["issues"].append({"id": entry.id, "reason": "contents do not match recorded hash (row edited?)"})
            prev_hash = entry.entry_hash
            report["checked"] += 1

    election_ids = (
        AuditLog.objects.exclude(election=None)
        .values_list("election_id", flat=True)
        .distinct()
    )
    for election_id in election_ids:
        _walk(
            AuditLog.objects.filter(election_id=election_id).order_by("id"),
            f"election:{election_id}",
        )
    _walk(AuditLog.objects.filter(election__isnull=True).order_by("id"), "school")

    return report


# ============================================================================
# Device Fingerprinting
# ============================================================================
def generate_device_fingerprint(request):
    """Generate a hashed fingerprint from request metadata."""
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    accept_lang = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
    raw = f"{user_agent}|{accept_lang}|{request.META.get('REMOTE_ADDR', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()


def is_new_device(voter, request):
    """Check if this device/browser is recognized for the voter."""
    from .models import RecognizedDevice
    fingerprint = generate_device_fingerprint(request)
    return not RecognizedDevice.objects.filter(
        voter=voter, device_fingerprint=fingerprint
    ).exists()


def register_device(voter, request):
    """Register this device as recognized for the voter."""
    from .models import RecognizedDevice
    fingerprint = generate_device_fingerprint(request)
    RecognizedDevice.objects.get_or_create(
        voter=voter,
        device_fingerprint=fingerprint,
    )


# ============================================================================
# Election State Transition
# ============================================================================
def transition_election_state(election, new_state, actor="SYSTEM"):
    """
    Safely transition an election to a new state.
    Handles side effects (tally hash generation, audit logging, report generation).

    The election row is locked for the duration of the transition so state
    changes serialize against vote casting: a ballot either commits before
    the tally is frozen, or sees the election closed.
    """
    from django.db import transaction
    from .models import PostElectionReport

    with transaction.atomic():
        locked = type(election).objects.select_for_update().get(pk=election.pk)
        if not locked.can_transition_to(new_state):
            raise ValueError(
                f"Cannot transition from {locked.state} to {new_state}"
            )

        old_state = locked.state
        locked.state = new_state

        if new_state == "CLOSED":
            locked.actual_closed_at = timezone.now()
            # Generate tally hash
            locked.final_tally_hash = locked.generate_tally_hash()
            locked.results_published_at = timezone.now()

            # Create post-election report placeholder
            PostElectionReport.objects.get_or_create(election=locked)

        locked.save()

        log_action(
            action_type="ELECTION_STATE_CHANGE",
            description=f"Election '{locked.title}' transitioned from {old_state} to {new_state}",
            actor=actor,
            election=locked,
            metadata={"old_state": old_state, "new_state": new_state},
            critical=True,
        )

    return locked


# ============================================================================
# Vote Casting (Atomic)
# ============================================================================
def cast_vote(voter, position, candidate):
    """
    Cast a vote atomically. Enforces one-vote-per-position.
    Returns (success: bool, message: str).
    """
    from django.db import transaction
    from .models import Vote

    if candidate.status != "APPROVED":
        return False, "This candidate is not approved for voting."

    if candidate.position != position:
        return False, "Candidate does not belong to this position."

    election = position.election

    if election.state != "LIVE":
        return False, "This election is not currently open for voting."

    with transaction.atomic():
        # Lock the election row and re-validate its state INSIDE the
        # transaction. This serializes the vote against election closure:
        # either this ballot commits before the closing transaction takes the
        # election row lock (and is included in the frozen tally), or the
        # election is already closed and the vote is rejected here. No ballot
        # can be recorded after the final tally hash is computed.
        locked_election = type(election).objects.select_for_update().get(pk=election.pk)
        if locked_election.state != "LIVE":
            return False, "This election is not currently open for voting."

        # Lock the voter row for the duration of this vote. Because the Vote
        # table intentionally has no voter foreign key, the voter-position
        # participation record is the concurrency guard. On PostgreSQL this
        # serializes simultaneous vote attempts by the same voter without
        # linking ballot records back to the voter.
        locked_voter = type(voter).objects.select_for_update().get(pk=voter.pk)

        if locked_voter.has_voted_for_position(position):
            # Denial is part of the trail. The attempted candidate choice is
            # deliberately NOT recorded — participation, never preference.
            log_action(
                action_type="DOUBLE_VOTE_ATTEMPT",
                description=f"Rejected repeat vote attempt for position '{position.name}'.",
                actor=voter.matric_number,
                election=election,
                metadata={"position": position.name},
                severity=SEVERITY_CRITICAL,
            )
            return False, "You have already voted for this position."

        Vote.objects.create(position=position, candidate=candidate)
        locked_voter.voted_positions.add(position)

        log_action(
            action_type="VOTE_CAST",
            description=f"Vote cast for position: {position.name}",
            actor="SYSTEM",
            election=election,
            metadata={"position": position.name},
            critical=True,
        )

    return True, "Vote cast successfully."
