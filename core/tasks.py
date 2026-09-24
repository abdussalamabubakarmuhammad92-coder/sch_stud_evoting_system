"""
Celery tasks for SUG E-Voting Platform.
"""
from celery import shared_task
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from .models import Election, Organization, StudentVoter
from .utils import transition_election_state, log_action


@shared_task
def check_election_transitions():
    """
    Periodic task to auto-transition elections based on time.
    - DRAFT -> LIVE when voting_opens_at is reached
    - LIVE -> CLOSED when voting_closes_at is reached
    """
    now = timezone.now()

    # Auto-open elections
    elections_to_open = Election.objects.filter(
        state="DRAFT",
        voting_opens_at__lte=now,
    )
    for election in elections_to_open:
        try:
            transition_election_state(election, "LIVE", actor="SYSTEM")
        except Exception as e:
            print(f"Failed to open election {election.id}: {e}")

    # Auto-close elections
    elections_to_close = Election.objects.filter(
        state="LIVE",
        voting_closes_at__lte=now,
    )
    for election in elections_to_close:
        try:
            adjusted_close = election.voting_closes_at + timezone.timedelta(
                seconds=election.total_extension_seconds
            )
            if now >= adjusted_close:
                transition_election_state(election, "CLOSED", actor="SYSTEM")
        except Exception as e:
            print(f"Failed to close election {election.id}: {e}")

    return f"Processed {elections_to_open.count()} opens, {elections_to_close.count()} closes"


@shared_task
def send_subscription_expiry_warnings():
    """Send warnings 7 days before subscription expiry."""
    warning_date = timezone.now() + timezone.timedelta(days=7)
    orgs = Organization.objects.filter(
        subscription_expires_at__date=warning_date.date(),
        subscription_status="ACTIVE",
    )

    for org in orgs:
        try:
            primary_admin = org.admins.get(is_primary=True)
            send_mail(
                subject="Subscription Expiry Warning — SUG E-Voting",
                message=f"Your subscription for {org.name} expires in 7 days. Please renew to avoid interruption.",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[primary_admin.user.email],
            )
        except Exception as e:
            print(f"Failed to send expiry warning to {org.name}: {e}")
