"""
Forms for SUG E-Voting Platform.
"""
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.contrib.auth import authenticate

from .models import (
    Organization,
    ElectionCategory,
    Election,
    Position,
    Candidate,
    VerifiedVoterRecord,
    StudentVoter,
)


# ============================================================================
# Authentication Forms
# ============================================================================
class VoterLoginForm(forms.Form):
    """Login form using matric number + password."""
    matric_number = forms.CharField(max_length=30, label="Matric Number")
    password = forms.CharField(widget=forms.PasswordInput, label="Password")

    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)


class AdminLoginForm(AuthenticationForm):
    """Standard email + password login for admins/officers."""
    username = forms.EmailField(label="Email", widget=forms.EmailInput())

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise ValidationError("This account is inactive.", code="inactive")
        if user.user_type not in ["PLATFORM_OWNER", "ORG_ADMIN", "ELECTION_OFFICER"]:
            raise ValidationError(
                "This login portal is for administrators only.",
                code="invalid_login",
            )


# ============================================================================
# Voter Registration Forms
# ============================================================================
class VoterRegistrationForm(forms.Form):
    """
    Step 1: Student enters matric, email, phone.
    System checks against VerifiedVoterRecord.
    """
    matric_number = forms.CharField(max_length=30, label="Matric Number")
    email = forms.EmailField(label="Email Address")
    phone_number = forms.CharField(max_length=20, label="Phone Number")

    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)

    def clean_matric_number(self):
        matric = self.cleaned_data["matric_number"].strip().upper()

        if not self.organization:
            raise ValidationError("Organization context is missing.")

        # Check if matric exists in verified records
        try:
            self.verified_record = VerifiedVoterRecord.objects.get(
                organization=self.organization, matric_number=matric
            )
        except VerifiedVoterRecord.DoesNotExist:
            raise ValidationError(
                "Matric number not recognized. Please contact your school's ICT department."
            )

        # Check if already registered
        if StudentVoter.objects.filter(
            organization=self.organization, matric_number=matric
        ).exists():
            raise ValidationError(
                "This matric number is already registered. Please log in instead."
            )

        return matric

    def clean(self):
        cleaned_data = super().clean()
        matric = cleaned_data.get("matric_number")
        email = cleaned_data.get("email")
        phone = cleaned_data.get("phone_number")

        if not hasattr(self, "verified_record"):
            return cleaned_data

        vvr = self.verified_record

        # Strict-match rule: phone is primary anchor, email is fallback
        if vvr.official_phone:
            # Phone must exactly match
            if phone != vvr.official_phone:
                raise ValidationError(
                    "Phone number does not match the official record on file. "
                    "Please use the phone number registered with your school."
                )
        else:
            # Email becomes mandatory strict-match
            if email != vvr.official_email:
                raise ValidationError(
                    "Email does not match the official record on file. "
                    "Please use the email registered with your school."
                )

        return cleaned_data


class OTPVerificationForm(forms.Form):
    """Step 2: Enter OTP code."""
    otp_code = forms.CharField(
        max_length=6,
        min_length=6,
        label="Verification Code",
        widget=forms.TextInput(attrs={"placeholder": "Enter 6-digit code"}),
    )


class SetPasswordForm(forms.Form):
    """Step 3: Set password after OTP verification."""
    password = forms.CharField(
        widget=forms.PasswordInput,
        label="Password",
        min_length=8,
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput,
        label="Confirm Password",
    )

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("confirm_password")

        if password and confirm and password != confirm:
            raise ValidationError("Passwords do not match.")

        return cleaned_data


class PasswordResetRequestForm(forms.Form):
    """Request password reset via matric number."""
    matric_number = forms.CharField(max_length=30, label="Matric Number")

    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)


# ============================================================================
# Organization Admin Forms
# ============================================================================
class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "slug", "logo", "subscription_status", "subscription_expires_at", "primary_admin_contact"]
        widgets = {
            "subscription_expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

class ElectionCategoryForm(forms.ModelForm):
    slug = forms.CharField(
        widget=forms.TextInput(attrs={'readonly': True}),
        required=False
    )

    class Meta:
        model = ElectionCategory
        fields = ["category_type", "name", "slug", "is_active", "display_order"]

class ElectionForm(forms.ModelForm):
    class Meta:
        model = Election
        fields = [
            "category",
            "title",
            "voting_opens_at",
            "voting_closes_at",
            "eligibility_type",
            "eligibility_filter_value",
        ]
        widgets = {
            "voting_opens_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "voting_closes_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "eligibility_filter_value": forms.Textarea(attrs={"rows": 3, "placeholder": '{"levels": [100, 200]}'}),
        }


class PositionForm(forms.ModelForm):
    class Meta:
        model = Position
        fields = ["name", "description", "display_order"]


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = ["name", "manifesto", "photo", "status", "screening_notes"]


class CandidateStatusForm(forms.ModelForm):
    """Form for approving/rejecting/withdrawing candidates."""
    class Meta:
        model = Candidate
        fields = ["status", "screening_notes"]


class CSVImportForm(forms.Form):
    """Form for uploading CSV of verified voter records."""
    csv_file = forms.FileField(
        label="CSV File",
        help_text="Upload a CSV with columns: matric_number, official_email, official_phone, level, department, faculty",
    )

    def clean_csv_file(self):
        file = self.cleaned_data["csv_file"]
        if not file.name.endswith(".csv"):
            raise ValidationError("Please upload a valid CSV file.")
        return file
