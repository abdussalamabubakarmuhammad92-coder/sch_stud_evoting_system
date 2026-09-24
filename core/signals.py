"""
Django signals for SUG E-Voting Platform.
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Candidate, AuditLog
from .utils import log_action


@receiver(pre_save, sender=Candidate)
def track_candidate_status_change(sender, instance, **kwargs):
    """Track candidate status changes for audit log."""
    if instance.pk:
        try:
            old = Candidate.objects.get(pk=instance.pk)
            if old.status != instance.status:
                instance._status_changed = True
                instance._old_status = old.status
        except Candidate.DoesNotExist:
            pass


@receiver(post_save, sender=Candidate)
def log_candidate_status_change(sender, instance, created, **kwargs):
    """Log candidate approvals, rejections, and withdrawals."""
    if created:
        log_action(
            organization=instance.position.election.organization,
            action_type="CANDIDATE_CREATED",
            description=f"Candidate '{instance.name}' created for position '{instance.position.name}'",
            actor="SYSTEM",
            election=instance.position.election,
            metadata={"candidate": instance.name, "position": instance.position.name},
        )
    elif hasattr(instance, "_status_changed") and instance._status_changed:
        action_map = {
            "APPROVED": "CANDIDATE_APPROVED",
            "REJECTED": "CANDIDATE_REJECTED",
            "WITHDRAWN": "CANDIDATE_WITHDRAWN",
        }
        action_type = action_map.get(instance.status, "CANDIDATE_STATUS_CHANGED")
        log_action(
            organization=instance.position.election.organization,
            action_type=action_type,
            description=f"Candidate '{instance.name}' status changed to {instance.status}",
            actor="SYSTEM",
            election=instance.position.election,
            metadata={
                "candidate": instance.name,
                "old_status": getattr(instance, "_old_status", "UNKNOWN"),
                "new_status": instance.status,
            },
        )
