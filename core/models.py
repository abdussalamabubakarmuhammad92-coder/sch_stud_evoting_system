"""
SUG E-Voting Platform — Data Models
Companion to: SUG_EVoting_Master_Blueprint.md
Built per: SUG evoting data model specification.md
"""
import hashlib
import secrets
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone


# ============================================================================
# 0. Custom User Model (supports all roles)
# ============================================================================
class User(AbstractUser):
    """
    Custom user model. All platform users (Platform Owner, Organization Admin,
    Election Officer, Voter) use this table. Role differentiation happens via
    related models (OrganizationAdmin, ElectionOfficer, StudentVoter).
    """
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, blank=True)
    is_platform_owner = models.BooleanField(default=False)

    # Track user type for quick checks (not authoritative — always check related tables)
    USER_TYPE_CHOICES = [
        ("PLATFORM_OWNER", "Platform Owner"),
        ("ORG_ADMIN", "Organization Admin"),
        ("ELECTION_OFFICER", "Election Officer"),
        ("VOTER", "Voter"),
        ("STAFF", "Staff"),
    ]
    user_type = models.CharField(
        max_length=20, choices=USER_TYPE_CHOICES, default="VOTER"
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    class Meta:
        db_table = "core_user"

    def __str__(self):
        return f"{self.email} ({self.get_user_type_display()})"


# ============================================================================
# 1. Organization [Blueprint §2]
# ============================================================================
class Organization(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    logo = models.ImageField(upload_to="org_logos/", blank=True, null=True)

    subscription_status = models.CharField(
        max_length=20,
        choices=[
            ("ACTIVE", "Active"),
            ("EXPIRED", "Expired"),
            ("GRACE", "Grace Period"),
        ],
        default="ACTIVE",
    )
    subscription_expires_at = models.DateTimeField()

    # Primary admin contact for communications
    primary_admin_contact = models.EmailField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def is_subscription_active(self):
        """Check if subscription is currently valid."""
        if self.subscription_status == "ACTIVE":
            return timezone.now() < self.subscription_expires_at
        return self.subscription_status == "GRACE"


# ============================================================================
# 2. OrganizationAdmin [Blueprint §7.1]
# ============================================================================
class OrganizationAdmin(models.Model):
    """
    Exactly three per Organization, one designated Primary.
    The 'exactly three' rule is enforced at the application/validation layer.
    """
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="admins"
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_primary=True),
                name="one_primary_admin_per_org",
            )
        ]

    def __str__(self):
        return f"{self.user.email} — Admin of {self.organization.name}"

    def clean(self):
        # Enforce exactly-three-admins at the model level as well
        if self.pk is None:  # Creating new
            current_count = OrganizationAdmin.objects.filter(
                organization=self.organization
            ).count()
            if current_count >= 3:
                raise ValidationError(
                    "This Organization already has the maximum of 3 admins."
                )
        super().clean()

# ============================================================================
# 3. ElectionOfficer ("Election Observer") [Blueprint §7, §10]
# ============================================================================
class ElectionOfficer(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="officers"
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    assigned_election = models.ForeignKey(
        "Election",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="officers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} — Officer at {self.organization.name}"


# ============================================================================
# 4. Election [Blueprint §3, §16]
# ============================================================================

# ============================================================================
# 4.5. ElectionCategory [New — Faculty / State Association / SUG tabs]
# ============================================================================
class ElectionCategory(models.Model):
    """
    Groups elections into categories for the voter dashboard tabs.
    """
    CATEGORY_TYPES = [
        ("FACULTY", "Faculty Election"),
        ("STATE_ASSOCIATION", "State Association Election"),
        ("SUG", "SUG Election"),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="election_categories"
    )
    category_type = models.CharField(max_length=20, choices=CATEGORY_TYPES)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_category_type_display()})"

class Election(models.Model):
    category = models.ForeignKey(
        ElectionCategory, on_delete=models.CASCADE, related_name="elections"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="elections", editable=False
    )
    title = models.CharField(max_length=200)  # e.g. "2026 SUG General Elections"

    state = models.CharField(
        max_length=20,
        choices=[
            ("DRAFT", "Draft"),
            ("LIVE", "Live"),
            ("CLOSED", "Closed"),
            ("ARCHIVED", "Archived"),
        ],
        default="DRAFT",
    )

    voting_opens_at = models.DateTimeField(null=True, blank=True)
    voting_closes_at = models.DateTimeField(null=True, blank=True)
    actual_closed_at = models.DateTimeField(
        null=True, blank=True
    )  # set on real CLOSED transition

    # Eligibility configuration [Blueprint §5]
    eligibility_type = models.CharField(
        max_length=30,
        choices=[
            ("ALL_STUDENTS", "All Registered Students"),
            ("LEVEL_FILTER", "Filtered by Level"),
            ("DEPARTMENT_FILTER", "Filtered by Department/Faculty"),
            ("UPLOADED_LIST", "Uploaded Eligible Voter List"),
        ],
        default="ALL_STUDENTS",
    )
    eligibility_filter_value = models.JSONField(
        blank=True, null=True
    )  # e.g. {"levels": [100, 200]}

    # Collation configuration [Blueprint §5 general note]
    collation_grouping_enabled = models.BooleanField(default=False)
    collation_grouping_field = models.CharField(
        max_length=50, blank=True, null=True
    )  # e.g. "faculty"

    # Results integrity [Blueprint §8]
    final_tally_hash = models.CharField(max_length=128, blank=True, null=True)
    results_published_at = models.DateTimeField(null=True, blank=True)

    # Downtime extension tracking [Blueprint §11.1]
    total_extension_seconds = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.organization.name})"

    def is_live(self):
        return self.state == "LIVE"

    def is_closed(self):
        return self.state == "CLOSED"

    def can_transition_to(self, new_state):
        """Valid state transitions per Blueprint §3."""
        transitions = {
            "DRAFT": ["LIVE"],
            "LIVE": ["CLOSED"],
            "CLOSED": ["ARCHIVED"],
            "ARCHIVED": [],
        }
        return new_state in transitions.get(self.state, [])

    def generate_tally_hash(self):
        """Generate SHA-256 hash of final tallies for all positions."""
        from django.db.models import Count

        tallies = []
        for position in self.positions.all():
            position_tally = {
                "position": position.name,
                "candidates": list(
                    position.candidates.filter(status="APPROVED").annotate(
                        vote_count=Count("votes")
                    ).values("name", "vote_count")
                ),
            }
            tallies.append(position_tally)

        tally_string = str(sorted(tallies, key=lambda x: x["position"]))
        return hashlib.sha256(tally_string.encode()).hexdigest()


# ============================================================================
# 5. Position [carried forward from demo, now scoped to Election]
# ============================================================================
class Position(models.Model):
    election = models.ForeignKey(
        Election, on_delete=models.CASCADE, related_name="positions"
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name


# ============================================================================
# 6. Candidate [Blueprint §6, §18]
# ============================================================================
class Candidate(models.Model):
    position = models.ForeignKey(
        Position, on_delete=models.CASCADE, related_name="candidates"
    )
    name = models.CharField(max_length=100)
    manifesto = models.TextField(blank=True)
    photo = models.ImageField(upload_to="candidates/", blank=True, null=True)

    status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "Pending Review"),
            ("APPROVED", "Approved"),
            ("REJECTED", "Rejected"),
            ("WITHDRAWN", "Withdrawn"),
        ],
        default="PENDING",
    )
    screening_notes = models.TextField(blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.status == "WITHDRAWN" and self.withdrawn_at is None:
            self.withdrawn_at = timezone.now()
        super().save(*args, **kwargs)


# ============================================================================
# 7. VerifiedVoterRecord [Blueprint §15.5 — ICT-supplied source of truth]
# ============================================================================
class VerifiedVoterRecord(models.Model):
    """
    The official student data supplied by the school's ICT/registry department.
    Registration is only possible where a matching VerifiedVoterRecord exists.
    """
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="verified_voters"
    )

    matric_number = models.CharField(max_length=30)
    official_email = models.EmailField(blank=True, null=True)
    official_phone = models.CharField(max_length=20, blank=True, null=True)

    level = models.PositiveIntegerField(blank=True, null=True)
    department = models.CharField(max_length=100, blank=True, null=True)
    faculty = models.CharField(max_length=100, blank=True, null=True)

    imported_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("organization", "matric_number")
        verbose_name = "Verified Voter Record"
        verbose_name_plural = "Verified Voter Records"

    def __str__(self):
        return f"{self.matric_number} ({self.organization.name})"

    def get_strict_match_field(self):
        """
        Returns which field is the strict-match anchor:
        'phone' if official_phone exists, else 'email'.
        Per Blueprint §15.5 / Data Model Section 8.
        """
        if self.official_phone:
            return "phone"
        return "email"


# ============================================================================
# 8. StudentVoter [Blueprint §15 — replaces demo's StudentProfile]
# ============================================================================
class StudentVoter(models.Model):
    """
    Represents the actual voter account created by a student.
    Distinct from VerifiedVoterRecord — this is 'the account a student has
    actually created' vs 'who the school confirms is a real student'.
    """
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="voters"
    )
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, null=True, blank=True
    )

    matric_number = models.CharField(max_length=30)
    email = models.EmailField()  # required — see §15.7
    phone_number = models.CharField(max_length=20)  # required — see §15.7

    # Populated from ICT-provided data BEFORE registration [§15.5]
    official_email_on_file = models.EmailField(blank=True, null=True)
    official_phone_on_file = models.CharField(max_length=20, blank=True, null=True)

    is_activated = models.BooleanField(default=False)
    level = models.PositiveIntegerField(blank=True, null=True)
    department = models.CharField(max_length=100, blank=True, null=True)
    faculty = models.CharField(max_length=100, blank=True, null=True)

    # Tracks WHICH positions voted, not WHO for
    # CRITICAL: Must NEVER be extended with a foreign key to Vote.
    voted_positions = models.ManyToManyField(Position, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("organization", "matric_number")
        verbose_name = "Student Voter"
        verbose_name_plural = "Student Voters"

    def __str__(self):
        return f"{self.matric_number} ({self.organization.name})"

    def has_voted_for_position(self, position):
        return self.voted_positions.filter(pk=position.pk).exists()


# ============================================================================
# 9. Vote [anonymity-preserving, carried forward from demo]
# ============================================================================
class Vote(models.Model):
    """
    Deliberately NO foreign key to StudentVoter or User — preserves anonymity.
    This is the core anonymity guarantee of the platform.
    """
    position = models.ForeignKey(
        Position, on_delete=models.CASCADE, related_name="votes"
    )
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="votes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Vote"
        verbose_name_plural = "Votes"

    def __str__(self):
        return f"Vote for {self.candidate.name} ({self.position.name})"


# ============================================================================
# 10. OTPVerification [Blueprint §15.6]
# ============================================================================
class OTPVerification(models.Model):
    voter = models.ForeignKey(
        StudentVoter, on_delete=models.CASCADE, related_name="otp_attempts"
    )
    purpose = models.CharField(
        max_length=30,
        choices=[
            ("REGISTRATION", "Registration"),
            ("NEW_DEVICE_LOGIN", "New Device Login"),
            ("PASSWORD_RESET", "Password Reset"),
        ],
    )
    delivery_method = models.CharField(
        max_length=10, choices=[("EMAIL", "Email"), ("SMS", "SMS")]
    )
    code_hash = models.CharField(max_length=128)  # store hashed, never plaintext
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    failed_attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "OTP Verification"
        verbose_name_plural = "OTP Verifications"

    def __str__(self):
        return f"OTP for {self.voter.matric_number} ({self.purpose})"

    def is_expired(self):
        return timezone.now() > self.expires_at

    def is_valid(self):
        return not self.used and not self.is_expired()

    @staticmethod
    def hash_code(code):
        """Hash an OTP code for secure storage."""
        return hashlib.sha256(code.encode()).hexdigest()

    def verify_code(self, code):
        """Check if provided code matches the stored hash."""
        return self.code_hash == self.hash_code(code)

    MAX_ATTEMPTS = 5

    def is_locked(self):
        return self.failed_attempts >= self.MAX_ATTEMPTS

    def register_failed_attempt(self):
        self.failed_attempts += 1
        self.save(update_fields=["failed_attempts"])


# ============================================================================
# 11.5. StateAssociationMembership [New — State association registration]
# ============================================================================
class StateAssociationMembership(models.Model):
    """
    Self-declared state of origin for state association elections.
    Locked once any relevant election goes LIVE.
    """
    NIGERIAN_STATES = [
        ("ABIA", "Abia"), ("ADAMAWA", "Adamawa"), ("AKWA IBOM", "Akwa Ibom"),
        ("ANAMBRA", "Anambra"), ("BAUCHI", "Bauchi"), ("BAYELSA", "Bayelsa"),
        ("BENUE", "Benue"), ("BORNO", "Borno"), ("CROSS RIVER", "Cross River"),
        ("DELTA", "Delta"), ("EBONYI", "Ebonyi"), ("EDO", "Edo"),
        ("EKITI", "Ekiti"), ("ENUGU", "Enugu"), ("FCT", "FCT - Abuja"),
        ("GOMBE", "Gombe"), ("IMO", "Imo"), ("JIGAWA", "Jigawa"),
        ("KADUNA", "Kaduna"), ("KANO", "Kano"), ("KATSINA", "Katsina"),
        ("KEBBI", "Kebbi"), ("KOGI", "Kogi"), ("KWARA", "Kwara"),
        ("LAGOS", "Lagos"), ("NASARAWA", "Nasarawa"), ("NIGER", "Niger"),
        ("OGUN", "Ogun"), ("ONDO", "Ondo"), ("OSUN", "Osun"),
        ("OYO", "Oyo"), ("PLATEAU", "Plateau"), ("RIVERS", "Rivers"),
        ("SOKOTO", "Sokoto"), ("TARABA", "Taraba"), ("YOBE", "Yobe"),
        ("ZAMFARA", "Zamfara"),
    ]

    voter = models.ForeignKey(
        StudentVoter, on_delete=models.CASCADE, related_name="state_memberships"
    )
    state = models.CharField(max_length=20, choices=NIGERIAN_STATES)
    registered_at = models.DateTimeField(auto_now_add=True)
    locked = models.BooleanField(default=False)

    class Meta:
        unique_together = ("voter", "state")
        verbose_name = "State Association Membership"
        verbose_name_plural = "State Association Memberships"

    def __str__(self):
        return f"{self.voter.matric_number} — {self.get_state_display()}"

# ============================================================================
# 11. RecognizedDevice [supports "new device login" OTP trigger, §15.6]
# ============================================================================
class RecognizedDevice(models.Model):
    voter = models.ForeignKey(
        StudentVoter, on_delete=models.CASCADE, related_name="devices"
    )
    device_fingerprint = models.CharField(
        max_length=255
    )  # hashed device/browser identifier
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("voter", "device_fingerprint")

    def __str__(self):
        return f"Device for {self.voter.matric_number}"


# ============================================================================
# 12. AuditLog [Blueprint §9 — used across nearly every section]
# ============================================================================
class AuditLog(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="audit_logs"
    )
    election = models.ForeignKey(
        Election,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )

    action_type = models.CharField(max_length=50)
    # e.g. "ELECTION_STATE_CHANGE", "CANDIDATE_APPROVED", "CSV_IMPORT",
    #      "TALLY_HASH_GENERATED", "DOWNTIME_EXTENSION", "PASSWORD_RESET",
    #      "TIE_DETECTED", "CANDIDATE_WITHDRAWN", "VOTE_CAST"

    description = models.TextField()
    actor = models.CharField(max_length=200, blank=True)
    # e.g. admin username, matric number, or "SYSTEM"

    metadata = models.JSONField(blank=True, null=True)

    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"

    def __str__(self):
        return f"[{self.action_type}] {self.actor} @ {self.timestamp}"

# ============================================================================
# 13.5. AdminInvitationCode [New — Admin registration via code]
# ============================================================================
class AdminInvitationCode(models.Model):
    """
    One-time use codes for org admin registration.
    Generated by Platform Owner, distributed by Stakeholder Lead.
    """
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="invitation_codes"
    )
    code = models.CharField(max_length=50, unique=True)
    invited_email = models.EmailField(blank=True, help_text="Email of the person this code was sent to")
    invited_name = models.CharField(max_length=200, blank=True, help_text="Name of the person this code was sent to")
    used_by = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_used(self):
        return self.used_by is not None

    def __str__(self):
        return f"{self.code} — {'USED' if self.is_used() else 'AVAILABLE'}"

# ============================================================================
# 13. PostElectionReport [Blueprint §12]
# ============================================================================
class PostElectionReport(models.Model):
    election = models.OneToOneField(
        Election, on_delete=models.CASCADE, related_name="report"
    )
    docx_file = models.FileField(upload_to="reports/docx/", blank=True, null=True)
    spreadsheet_file = models.FileField(
        upload_to="reports/spreadsheets/", blank=True, null=True
    )

    is_public = models.BooleanField(default=False)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Post-Election Report"
        verbose_name_plural = "Post-Election Reports"

    def __str__(self):
        return f"Report for {self.election.title}"

