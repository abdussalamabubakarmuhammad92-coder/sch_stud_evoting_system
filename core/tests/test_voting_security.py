from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import connection, close_old_connections
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Candidate,
    Election,
    ElectionCategory,
    Organization,
    Position,
    StudentVoter,
    User,
    Vote,
)
from core.utils import cast_vote


class VotingSecurityTests(TestCase):
    def setUp(self):
        expires = timezone.now() + timedelta(days=30)
        self.org = Organization.objects.create(
            name="Test University",
            slug="test-university",
            subscription_expires_at=expires,
        )
        self.other_org = Organization.objects.create(
            name="Other University",
            slug="other-university",
            subscription_expires_at=expires,
        )
        self.category = ElectionCategory.objects.create(
            organization=self.org,
            category_type="SUG",
            name="SUG",
            slug="test-sug",
        )
        self.election = Election.objects.create(
            organization=self.org,
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
            organization=self.org,
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

    def test_cross_organization_vote_is_rejected(self):
        other_user = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="StrongPassword123!",
        )
        other_voter = StudentVoter.objects.create(
            organization=self.other_org,
            user=other_user,
            matric_number="OTHER/001",
            email="other@example.com",
            phone_number="08111111111",
            is_activated=True,
        )
        success, message = cast_vote(other_voter, self.position, self.candidate)
        self.assertFalse(success)
        self.assertIn("organization", message)
        self.assertEqual(Vote.objects.count(), 0)

    def test_ballot_view_never_exposes_another_voters_candidate(self):
        cast_vote(self.voter, self.position, self.candidate)
        client = Client()
        client.force_login(self.voter.user)
        response = client.get(
            reverse(
                "ballot_view",
                kwargs={"slug": self.org.slug, "election_id": self.election.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "voted_candidate_id")
        self.assertContains(response, "already voted")


class OTPAndElectionStateTests(TestCase):
    def test_otp_is_time_limited_and_failed_attempts_lock(self):
        # This test is intentionally model-level; delivery is external infrastructure.
        expires = timezone.now() + timedelta(days=30)
        org = Organization.objects.create(
            name="OTP University",
            slug="otp-university",
            subscription_expires_at=expires,
        )
        user = User.objects.create_user(
            username="otp@example.com",
            email="otp@example.com",
            password="StrongPassword123!",
        )
        voter = StudentVoter.objects.create(
            organization=org,
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
        expires = timezone.now() + timedelta(days=30)
        org = Organization.objects.create(
            name="State University",
            slug="state-university",
            subscription_expires_at=expires,
        )
        category = ElectionCategory.objects.create(
            organization=org,
            category_type="SUG",
            name="SUG",
            slug="state-sug",
        )
        election = Election.objects.create(
            organization=org,
            category=category,
            title="State Election",
        )
        self.assertTrue(election.can_transition_to("LIVE"))
        self.assertFalse(election.can_transition_to("CLOSED"))
        election.state = "LIVE"
        self.assertTrue(election.can_transition_to("CLOSED"))
        self.assertFalse(election.can_transition_to("DRAFT"))


class ConcurrentVotingTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        expires = timezone.now() + timedelta(days=30)
        org = Organization.objects.create(
            name="Concurrency University",
            slug="concurrency-university",
            subscription_expires_at=expires,
        )
        category = ElectionCategory.objects.create(
            organization=org,
            category_type="SUG",
            name="SUG",
            slug="concurrency-sug",
        )
        election = Election.objects.create(
            organization=org,
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
            organization=org,
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
