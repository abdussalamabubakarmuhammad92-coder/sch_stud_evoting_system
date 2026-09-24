"""
Utility functions for SUG E-Voting Platform.
"""
import string
import hashlib
import secrets
import logging
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
    try:
        vvr = voter.organization.verified_voters.get(matric_number=voter.matric_number)
        if vvr.official_phone:
            delivery_method = "SMS"
        else:
            delivery_method = "EMAIL"
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
    # Determine delivery method from the latest OTP record
    try:
        vvr = voter.organization.verified_voters.get(matric_number=voter.matric_number)
        if vvr.official_phone:
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
def log_action(organization, action_type, description, actor="SYSTEM", election=None, metadata=None):
    """Create an audit log entry."""
    try:
        AuditLog.objects.create(
            organization=organization,
            election=election,
            action_type=action_type,
            description=description,
            actor=str(actor),
            metadata=metadata or {},
        )
        logger.info(f"AUDIT: [{action_type}] {actor} — {description}")
    except Exception as e:
        logger.error(f"Failed to create audit log: {e}")


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
    """
    from django.db import transaction
    from .models import PostElectionReport

    if not election.can_transition_to(new_state):
        raise ValueError(
            f"Cannot transition from {election.state} to {new_state}"
        )

    with transaction.atomic():
        old_state = election.state
        election.state = new_state

        if new_state == "CLOSED":
            election.actual_closed_at = timezone.now()
            # Generate tally hash
            election.final_tally_hash = election.generate_tally_hash()
            election.results_published_at = timezone.now()

            # Create post-election report placeholder
            PostElectionReport.objects.get_or_create(election=election)

        election.save()

        log_action(
            organization=election.organization,
            action_type="ELECTION_STATE_CHANGE",
            description=f"Election '{election.title}' transitioned from {old_state} to {new_state}",
            actor=actor,
            election=election,
            metadata={"old_state": old_state, "new_state": new_state},
        )

    return election


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
    if voter.organization_id != election.organization_id:
        return False, "Voter is not a member of this election's organization."

    if election.state != "LIVE":
        return False, "This election is not currently open for voting."

    with transaction.atomic():
        # Lock the voter row for the duration of this vote. Because the Vote
        # table intentionally has no voter foreign key, the voter-position
        # participation record is the concurrency guard. On PostgreSQL this
        # serializes simultaneous vote attempts by the same voter without
        # linking ballot records back to the voter.
        locked_voter = type(voter).objects.select_for_update().get(pk=voter.pk)

        if locked_voter.has_voted_for_position(position):
            return False, "You have already voted for this position."

        Vote.objects.create(position=position, candidate=candidate)
        locked_voter.voted_positions.add(position)

        log_action(
            organization=election.organization,
            action_type="VOTE_CAST",
            description=f"Vote cast for position: {position.name}",
            actor="SYSTEM",
            election=election,
            metadata={"position": position.name},
        )

    return True, "Vote cast successfully."
