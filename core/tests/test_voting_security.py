from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import connection, close_old_connections
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditLog,
    Candidate,
    Election,
    ElectionCategory,
    Position,
    StudentVoter,
    User,
    Vote,
)
from core.utils import cast_vote, verify_audit_chain


class VotingSecurityTests(TestCase):
    def setUp(self):
        self.category = ElectionCategory.objects.create(
            category_type="SUG",
            name="SUG",
            slug="test-sug",
        )
        self.election = Election.objects.create(
            category=self.category,
            title="Test Election",
            state="LIVE",
        )
        self.position = Position.objects.create(
            election=self.election,
            name="President",
        )
        self.candidate = Candidate.objects.create(
            position=self.position,
            name="Candidate One",
            status="APPROVED",
        )
        self.other_candidate = Candidate.objects.create(
            position=self.position,
            name="Candidate Two",
            status="APPROVED",
        )
        user = User.objects.create_user(
            username="voter@example.com",
            email="voter@example.com",
            password="StrongPassword123!",
        )
        self.voter = StudentVoter.objects.create(
            user=user,
            matric_number="TEST/001",
            email="voter@example.com",
            phone_number="08000000000",
            is_activated=True,
            level=100,
        )

    def test_first_vote_succeeds_and_second_vote_is_rejected(self):
        success, _ = cast_vote(self.voter, self.position, self.candidate)
        self.assertTrue(success)
        success, message = cast_vote(self.voter, self.position, self.other_candidate)
        self.assertFalse(success)
        self.assertIn("already voted", message)
        self.assertEqual(Vote.objects.filter(position=self.position).count(), 1)

    def test_vote_in_non_live_election_is_rejected(self):
        self.election.state = "DRAFT"
        self.election.save()
        success, message = cast_vote(self.voter, self.position, self.candidate)
        self.assertFalse(success)
        self.assertIn("not currently open", message)
        self.assertEqual(Vote.objects.count(), 0)

    def test_ballot_view_never_exposes_another_voters_candidate(self):
        cast_vote(self.voter, self.position, self.candidate)
        client = Client()
        client.force_login(self.voter.user)
        response = client.get(
            reverse(
                "ballot_view",
                kwargs={"election_id": self.election.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "voted_candidate_id")
        self.assertContains(response, "already voted")


class OTPAndElectionStateTests(TestCase):
    def test_otp_is_time_limited_and_failed_attempts_lock(self):
        # This test is intentionally model-level; delivery is external infrastructure.
        user = User.objects.create_user(
            username="otp@example.com",
            email="otp@example.com",
            password="StrongPassword123!",
        )
        voter = StudentVoter.objects.create(
            user=user,
            matric_number="OTP/001",
            email="otp@example.com",
            phone_number="08000000000",
        )
        from core.models import OTPVerification

        otp = OTPVerification.objects.create(
            voter=voter,
            purpose="REGISTRATION",
            delivery_method="EMAIL",
            code_hash=OTPVerification.hash_code("123456"),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertTrue(otp.is_expired())
        self.assertFalse(otp.is_valid())

        otp.expires_at = timezone.now() + timedelta(minutes=10)
        otp.save(update_fields=["expires_at"])
        for _ in range(otp.MAX_ATTEMPTS):
            otp.register_failed_attempt()
        self.assertTrue(otp.is_locked())

    def test_election_state_transitions_are_explicit(self):
        category = ElectionCategory.objects.create(
            category_type="SUG",
            name="SUG",
            slug="state-sug",
        )
        election = Election.objects.create(
            category=category,
            title="State Election",
        )
        self.assertTrue(election.can_transition_to("LIVE"))
        self.assertFalse(election.can_transition_to("CLOSED"))
        election.state = "LIVE"
        self.assertTrue(election.can_transition_to("CLOSED"))
        self.assertFalse(election.can_transition_to("DRAFT"))


class AuditChainTests(TestCase):
    def _make_election_with_voter(self, state="LIVE"):
        category = ElectionCategory.objects.create(
            category_type="SUG", name="SUG", slug="audit-sug"
        )
        election = Election.objects.create(
            category=category, title="Audit Election", state=state
        )
        position = Position.objects.create(election=election, name="President")
        candidate = Candidate.objects.create(
            position=position, name="Candidate One", status="APPROVED"
        )
        user = User.objects.create_user(
            username="audit@example.com",
            email="audit@example.com",
            password="StrongPassword123!",
        )
        voter = StudentVoter.objects.create(
            user=user,
            matric_number="AUD/001",
            email="audit@example.com",
            phone_number="08000000000",
            is_activated=True,
        )
        return election, position, candidate, voter

    def test_chain_is_valid_after_normal_activity(self):
        election, position, candidate, voter = self._make_election_with_voter()
        cast_vote(voter, position, candidate)

        report = verify_audit_chain()
        self.assertTrue(report["valid"], report["issues"])
        self.assertGreater(report["checked"], 0)
        self.assertTrue(
            AuditLog.objects.filter(action_type="VOTE_CAST", election=election).exists()
        )

    def test_tampering_is_detected(self):
        election, position, candidate, voter = self._make_election_with_voter()
        cast_vote(voter, position, candidate)
        self.assertTrue(verify_audit_chain()["valid"])

        # Simulate a direct database edit bypassing the application
        AuditLog.objects.filter(action_type="VOTE_CAST").update(
            description="Tampered description"
        )
        report = verify_audit_chain()
        self.assertFalse(report["valid"])
        self.assertTrue(any("edited" in i["reason"] for i in report["issues"]))

    def test_double_vote_attempt_is_audited_without_ballot_leak(self):
        election, position, candidate, voter = self._make_election_with_voter()
        other = Candidate.objects.create(
            position=position, name="Candidate Two", status="APPROVED"
        )

        cast_vote(voter, position, candidate)
        ok, _msg = cast_vote(voter, position, other)
        self.assertFalse(ok)

        entry = AuditLog.objects.filter(action_type="DOUBLE_VOTE_ATTEMPT").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, voter.matric_number)
        # The attempted candidate choice must never appear in the register
        import json as _json
        self.assertNotIn(other.name, entry.description)
        self.assertNotIn(other.name, _json.dumps(entry.metadata))

    def test_admin_panel_changes_are_mirrored(self):
        from django.contrib.admin.models import ADDITION, LogEntry
        from django.contrib.contenttypes.models import ContentType

        user = User.objects.create_user(
            username="tech@example.com", email="tech@example.com", password="StrongPassword123!"
        )
        ct = ContentType.objects.get_for_model(Candidate)
        LogEntry.objects.create(
            user=user, content_type=ct, object_id="1",
            object_repr="Candidate One", action_flag=ADDITION,
        )

        entry = AuditLog.objects.filter(action_type="ADMIN_PANEL_ADDED").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, "tech@example.com")
        self.assertEqual(entry.severity, "SECURITY")


class ConcurrentVotingTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        category = ElectionCategory.objects.create(
            category_type="SUG",
            name="SUG",
            slug="concurrency-sug",
        )
        election = Election.objects.create(
            category=category,
            title="Concurrent Election",
            state="LIVE",
        )
        position = Position.objects.create(election=election, name="President")
        candidate = Candidate.objects.create(
            position=position, name="Candidate", status="APPROVED"
        )
        user = User.objects.create_user(
            username="concurrent@example.com",
            email="concurrent@example.com",
            password="StrongPassword123!",
        )
        voter = StudentVoter.objects.create(
            user=user,
            matric_number="CON/001",
            email="concurrent@example.com",
            phone_number="08000000000",
            is_activated=True,
        )
        self.voter_id = voter.id
        self.position_id = position.id
        self.candidate_id = candidate.id

    @skipUnlessDBFeature("supports_transactions")
    def test_concurrent_vote_attempts_allow_only_one_ballot(self):
        if connection.vendor != "postgresql":
            self.skipTest("Concurrent row-lock behavior is validated on PostgreSQL.")

        def attempt_vote():
            close_old_connections()
            try:
                voter = StudentVoter.objects.get(pk=self.voter_id)
                position = Position.objects.get(pk=self.position_id)
                candidate = Candidate.objects.get(pk=self.candidate_id)
                return cast_vote(voter, position, candidate)[0]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt_vote(), range(2)))

        self.assertEqual(sum(results), 1)
        self.assertEqual(Vote.objects.filter(position_id=self.position_id).count(), 1)
