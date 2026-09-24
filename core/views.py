"""
Views for SUG E-Voting Platform.
Organized by user role and functionality.
"""
import csv
import io
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponseForbidden, Http404, HttpResponse
from django.views.decorators.http import require_POST, require_http_methods
from django.db.models import Count, Q
from django.utils import timezone
from datetime import datetime
from django.conf import settings


from .models import (
    User,
    Organization,
    OrganizationAdmin,
    ElectionOfficer,
    Election,
    Position,
    Candidate,
    VerifiedVoterRecord,
    StudentVoter,
    Vote,
    OTPVerification,
    AuditLog,
    PostElectionReport,
    ElectionCategory,
    AdminInvitationCode,
)
from .forms import (
    VoterLoginForm,
    AdminLoginForm,
    VoterRegistrationForm,
    OTPVerificationForm,
    SetPasswordForm,
    PasswordResetRequestForm,
    ElectionForm,
    PositionForm,
    CandidateForm,
    CandidateStatusForm,
    CSVImportForm,
    OrganizationForm,
    ElectionCategoryForm
)
from .utils import (
    create_otp_verification,
    send_otp,
    log_action,
    cast_vote,
    transition_election_state,
    is_new_device,
    register_device,
    generate_device_fingerprint,
)


# ============================================================================
# Mixins & Decorators
# ============================================================================
def org_admin_required(view_func):
    """Decorator ensuring user is an Organization Admin."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not hasattr(request.user, "organizationadmin"):
            messages.error(request, "Access denied. Organization Admin required.")
            return redirect("home")
        return view_func(request, *args, **kwargs)
    return wrapper


def election_officer_required(view_func):
    """Decorator ensuring user is an Election Officer."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not hasattr(request.user, "electionofficer"):
            messages.error(request, "Access denied. Election Officer required.")
            return redirect("home")
        return view_func(request, *args, **kwargs)
    return wrapper


def platform_owner_required(view_func):
    """Decorator ensuring user is a Platform Owner."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_platform_owner:
            messages.error(request, "Access denied. Platform Owner required.")
            return redirect("home")
        return view_func(request, *args, **kwargs)
    return wrapper


def get_org_from_request(request):
    """Get the organization from the current user's context."""
    if hasattr(request.user, "organizationadmin"):
        return request.user.organizationadmin.organization
    elif hasattr(request.user, "electionofficer"):
        return request.user.electionofficer.organization
    elif hasattr(request.user, "studentvoter"):
        return request.user.studentvoter.organization
    return None


# ============================================================================
# Public Views
# ============================================================================
def home(request):
    """Landing page showing all subscribed organizations."""
    organizations = Organization.objects.filter(
        subscription_status__in=["ACTIVE", "GRACE"]
    ).order_by("name")
    return render(request, "core/home.html", {"organizations": organizations})


def organization_landing(request, slug):
    """Organization-specific landing page."""
    org = get_object_or_404(Organization, slug=slug)
    request.session["organization_slug"] = slug

    # Past elections for the collapsible results section
    past_elections = Election.objects.filter(
        organization=org,
        state__in=["CLOSED", "ARCHIVED"],
    ).order_by("-actual_closed_at")

    return render(request, "core/organization_landing.html", {
        "organization": org,
        "past_elections": past_elections,
    })

    return render(request, "core/organization_landing.html", {
        "organization": org,
        "elections": elections,
    })


# ============================================================================
# Authentication Views
# ============================================================================
def voter_login(request, slug):
    """Voter login using matric number + password."""
    org = get_object_or_404(Organization, slug=slug)

    if request.method == "POST":
        form = VoterLoginForm(request.POST, organization=org)
        if form.is_valid():
            matric = form.cleaned_data["matric_number"]
            password = form.cleaned_data["password"]

            try:
                voter = StudentVoter.objects.get(
                    organization=org, matric_number=matric.upper()
                )
                user = authenticate(request, username=voter.user.email, password=password)

                if user is not None:
                    # Check for new device
                    if is_new_device(voter, request):
                        # Store in session, redirect to OTP
                        request.session["pending_login_voter_id"] = voter.id
                        request.session["pending_login_redirect"] = request.POST.get("next", "voter_dashboard")

                        code, otp = create_otp_verification(voter, "NEW_DEVICE_LOGIN")
                        send_otp(voter, code, "NEW_DEVICE_LOGIN")

                        messages.info(request, "New device detected. Please verify with the OTP sent to your registered contact.")
                        return redirect("new_device_otp", slug=slug)

                    login(request, user)
                    register_device(voter, request)
                    messages.success(request, f"Welcome back, {voter.matric_number}!")
                    return redirect("voter_dashboard", slug=slug)
                else:
                    messages.error(request, "Invalid password.")
            except StudentVoter.DoesNotExist:
                messages.error(request, "Matric number not found.")
    else:
        form = VoterLoginForm(organization=org)

    return render(request, "core/voter_login.html", {"form": form, "organization": org})

def admin_register(request):
    """Admin registration using invitation code."""
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()
        email = request.POST.get("email", "").strip()
        name = request.POST.get("name", "").strip()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")

        errors = []

        if not code or not email or not name or not password:
            errors.append("All fields are required.")
        
        if password != confirm_password:
            errors.append("Passwords do not match.")
        
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")

        if not errors:
            try:
                invitation = AdminInvitationCode.objects.select_related("organization").get(code=code)
            except AdminInvitationCode.DoesNotExist:
                errors.append("Invalid invitation code.")
            else:
                if invitation.is_used():
                    errors.append("This code has already been used.")
                elif User.objects.filter(email=email).exists():
                    errors.append("An account with this email already exists.")
                else:
                    # Check if org already has 3 admins
                    admin_count = OrganizationAdmin.objects.filter(organization=invitation.organization).count()
                    if admin_count >= 3:
                        errors.append("This organization already has 3 admins.")
                    else:
                        # Determine if this person will be primary
                        is_primary = (admin_count == 0)

                        # Create user
                        user = User.objects.create_user(
                            username=email,
                            email=email,
                            password=password,
                            user_type="ORG_ADMIN",
                        )

                        # Create org admin
                        OrganizationAdmin.objects.create(
                            organization=invitation.organization,
                            user=user,
                            is_primary=is_primary,
                        )

                        # Mark code as used
                        invitation.used_by = user
                        invitation.used_at = timezone.now()
                        invitation.save()

                        # Update org primary contact if this is primary admin
                        if is_primary:
                            invitation.organization.primary_admin_contact = email
                            invitation.organization.save()

                        # Log
                        log_action(
                            organization=invitation.organization,
                            action_type="ADMIN_REGISTERED",
                            description=f"Admin '{name}' ({email}) registered via invitation code",
                            actor=email,
                        )

                        messages.success(request, f"Registration successful! You can now log in.")
                        return redirect("admin_login")

        if errors:
            for error in errors:
                messages.error(request, error)

    return render(request, "core/admin_register.html")

def admin_login(request):
    """Admin/Officer login using email + password."""
    if request.method == "POST":
        form = AdminLoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)

            if user.is_platform_owner:
                return redirect("platform_dashboard")
            elif hasattr(user, "organizationadmin"):
                return redirect("admin_dashboard")
            elif hasattr(user, "electionofficer"):
                return redirect("observer_dashboard")
            else:
                messages.error(request, "Unauthorized access.")
                return redirect("home")
    else:
        form = AdminLoginForm()

    return render(request, "core/admin_login.html", {"form": form})


def logout_view(request):
    """Logout all user types."""
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("home")


# ============================================================================
# New Device OTP Verification
# ============================================================================
def new_device_otp(request, slug):
    """Verify OTP for new device login."""
    org = get_object_or_404(Organization, slug=slug)
    voter_id = request.session.get("pending_login_voter_id")

    if not voter_id:
        messages.error(request, "Session expired. Please log in again.")
        return redirect("voter_login", slug=slug)

    voter = get_object_or_404(StudentVoter, id=voter_id, organization=org)

    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]

            try:
                otp = OTPVerification.objects.filter(
                    voter=voter,
                    purpose="NEW_DEVICE_LOGIN",
                    used=False,
                ).latest("created_at")

                if otp.is_expired():
                    messages.error(request, "OTP has expired. Please log in again.")
                    del request.session["pending_login_voter_id"]
                    return redirect("voter_login", slug=slug)
            

                if otp.is_locked():
                    messages.error(request, "Too many incorrect attempts")
                    del request.session["pending_login_voter_id"]
                    return redirect("voter_login", slug=slug)

                if otp.verify_code(code):
                    otp.used = True
                    otp.save()

                    register_device(voter, request)
                    login(request, voter.user)

                    del request.session["pending_login_voter_id"]
                    redirect_url = request.session.pop("pending_login_redirect", "voter_dashboard")

                    log_action(
                        organization=org,
                        action_type="NEW_DEVICE_LOGIN",
                        description=f"New device verified for {voter.matric_number}",
                        actor=voter.matric_number,
                    )

                    messages.success(request, "Device verified successfully!")
                    return redirect(redirect_url, slug=slug)
                else:
                    otp.register_failed_attempt()
                    messages.error(request, "Invalid OTP code.")
            except OTPVerification.DoesNotExist:
                messages.error(request, "No pending OTP found. Please log in again.")
                return redirect("voter_login", slug=slug)
    else:
        form = OTPVerificationForm()

    return render(request, "core/otp_verify.html", {
        "form": form,
        "organization": org,
        "purpose": "new_device",
    })


# ============================================================================
# Voter Registration Flow (3 Steps)
# ============================================================================
def voter_register_step1(request, slug):
    """
    Step 1: Student enters matric, email, phone.
    System validates against VerifiedVoterRecord with strict-match rule.
    """
    org = get_object_or_404(Organization, slug=slug)

    if request.method == "POST":
        form = VoterRegistrationForm(request.POST, organization=org)
        if form.is_valid():
            matric = form.cleaned_data["matric_number"]
            email = form.cleaned_data["email"]
            phone = form.cleaned_data["phone_number"]

            # Get verified record
            vvr = VerifiedVoterRecord.objects.get(
                organization=org, matric_number=matric
            )

            # Create unactivated StudentVoter
            voter = StudentVoter.objects.create(
                organization=org,
                matric_number=matric,
                email=email,
                phone_number=phone,
                official_email_on_file=vvr.official_email,
                official_phone_on_file=vvr.official_phone,
                level=vvr.level,
                department=vvr.department,
                faculty=vvr.faculty,
                is_activated=False,
            )

            # Generate and send OTP
            code, otp = create_otp_verification(voter, "REGISTRATION")
            send_otp(voter, code, "REGISTRATION")

            # Store voter ID in session for next step
            request.session["registration_voter_id"] = voter.id

            messages.info(request, "An OTP has been sent to your registered contact. Please enter it below.")
            return redirect("voter_register_step2", slug=slug)
    else:
        form = VoterRegistrationForm(organization=org)

    return render(request, "core/voter_register_step1.html", {
        "form": form,
        "organization": org,
    })


def voter_register_step2(request, slug):
    """Step 2: Verify OTP."""
    org = get_object_or_404(Organization, slug=slug)
    voter_id = request.session.get("registration_voter_id")

    if not voter_id:
        messages.error(request, "Registration session expired. Please start again.")
        return redirect("voter_register_step1", slug=slug)

    voter = get_object_or_404(StudentVoter, id=voter_id, organization=org)

    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]

            try:
                otp = OTPVerification.objects.filter(
                    voter=voter,
                    purpose="REGISTRATION",
                    used=False,
                ).latest("created_at")

                if otp.is_expired():
                    messages.error(request, "OTP has expired. Please request a new one.")
                    return redirect("voter_register_step2", slug=slug)

                if otp.is_locked():
                    messages.error(request, "Too many incorrect attempts. Please request a new code.")
                    return redirect("voter_register_step2", slug=slug)

                if otp.verify_code(code):
                    otp.used = True
                    otp.save()

                    # Re-set session variables and force save
                    request.session["registration_voter_id"] = voter.id
                    request.session["registration_verified"] = True
                    request.session.modified = True

                    messages.success(request, "OTP verified! Now set your password.")
                    return redirect("voter_register_step3", slug=slug)
                else:
                    otp.register_failed_attempt()
                    messages.error(request, "Invalid OTP code. Please try again.")
            except OTPVerification.DoesNotExist:
                messages.error(request, "No pending OTP found. Please start registration again.")
                return redirect("voter_register_step1", slug=slug)
    else:
        form = OTPVerificationForm()

    return render(request, "core/otp_verify.html", {
        "form": form,
        "organization": org,
        "purpose": "registration",
        "voter": voter,
    })

def voter_register_step3(request, slug):
    """Step 3: Set password and activate account."""
    org = get_object_or_404(Organization, slug=slug)
    voter_id = request.session.get("registration_voter_id")
    verified = request.session.get("registration_verified", False)

    if not voter_id or not verified:
        messages.error(request, "Registration session expired. Please start again.")
        return redirect("voter_register_step1", slug=slug)

    voter = get_object_or_404(StudentVoter, id=voter_id, organization=org)

    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            password = form.cleaned_data["password"]

            # Create Django User
            user = User.objects.create_user(
                username=voter.matric_number,
                email=voter.email,
                password=password,
                user_type="VOTER",
            )

            # Link voter to user and activate
            voter.user = user
            voter.is_activated = True
            voter.save()

            # Register device
            register_device(voter, request)

            # Log in the user
            login(request, user)

            # Clean up session safely
            request.session.pop("registration_voter_id", None)
            request.session.pop("registration_verified", None)

            log_action(
                organization=org,
                action_type="VOTER_REGISTERED",
                description=f"Voter {voter.matric_number} completed registration",
                actor=voter.matric_number,
            )

            messages.success(request, "Registration complete! Welcome to the platform.")
            return redirect("voter_dashboard", slug=slug)
    else:
        form = SetPasswordForm()

    return render(request, "core/voter_register_step3.html", {
        "form": form,
        "organization": org,
        "voter": voter,
    })


# ============================================================================
# Password Reset Flow
# ============================================================================
def password_reset_request(request, slug):
    """Request password reset via matric number."""
    org = get_object_or_404(Organization, slug=slug)

    if request.method == "POST":
        form = PasswordResetRequestForm(request.POST, organization=org)
        if form.is_valid():
            matric = form.cleaned_data["matric_number"]

            try:
                voter = StudentVoter.objects.get(
                    organization=org, matric_number=matric.upper(), is_activated=True
                )

                # Generate OTP
                code, otp = create_otp_verification(voter, "PASSWORD_RESET")
                send_otp(voter, code, "PASSWORD_RESET")

                request.session["reset_voter_id"] = voter.id

                log_action(
                    organization=org,
                    action_type="PASSWORD_RESET_REQUESTED",
                    description=f"Password reset requested for {matric}",
                    actor=matric,
                )

                messages.info(request, "A reset code has been sent to your registered contact.")
                return redirect("password_reset_verify", slug=slug)

            except StudentVoter.DoesNotExist:
                # Don't reveal whether matric exists
                messages.info(request, "If this matric number is registered, a reset code has been sent.")
                return redirect("voter_login", slug=slug)
    else:
        form = PasswordResetRequestForm(organization=org)

    return render(request, "core/password_reset_request.html", {
        "form": form,
        "organization": org,
    })


def password_reset_verify(request, slug):
    """Verify OTP for password reset."""
    org = get_object_or_404(Organization, slug=slug)
    voter_id = request.session.get("reset_voter_id")

    if not voter_id:
        messages.error(request, "Session expired. Please try again.")
        return redirect("password_reset_request", slug=slug)

    voter = get_object_or_404(StudentVoter, id=voter_id, organization=org)

    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]

            try:
                otp = OTPVerification.objects.filter(
                    voter=voter,
                    purpose="PASSWORD_RESET",
                    used=False,
                ).latest("created_at")

                if otp.is_expired():
                    messages.error(request, "Code has expired. Please request a new one.")
                    return redirect("password_reset_request", slug=slug)

                if otp.is_locked():
                    messages.error(request, "Too many incorrect attempts. Please request a new code")
                    return redirect("password_reset_request", slug=slug)

                if otp.verify_code(code):
                    otp.used = True
                    otp.save()
                    request.session["reset_verified"] = True
                    messages.success(request, "Code verified! Enter your new password.")
                    return redirect("password_reset_new", slug=slug)
                else:
                    otp.register_failed_attempt()
                    messages.error(request, "Invalid code.")
            except OTPVerification.DoesNotExist:
                messages.error(request, "No pending reset found.")
                return redirect("password_reset_request", slug=slug)
    else:
        form = OTPVerificationForm()

    return render(request, "core/otp_verify.html", {
        "form": form,
        "organization": org,
        "purpose": "password_reset",
    })


def password_reset_new(request, slug):
    """Set new password after OTP verification."""
    org = get_object_or_404(Organization, slug=slug)
    voter_id = request.session.get("reset_voter_id")
    verified = request.session.get("reset_verified", False)

    if not voter_id or not verified:
        messages.error(request, "Session expired. Please try again.")
        return redirect("password_reset_request", slug=slug)

    voter = get_object_or_404(StudentVoter, id=voter_id, organization=org)

    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            password = form.cleaned_data["password"]
            voter.user.set_password(password)
            voter.user.save()

            del request.session["reset_voter_id"]
            del request.session["reset_verified"]

            log_action(
                organization=org,
                action_type="PASSWORD_RESET_COMPLETED",
                description=f"Password reset completed for {voter.matric_number}",
                actor=voter.matric_number,
            )

            messages.success(request, "Password updated successfully! Please log in.")
            return redirect("voter_login", slug=slug)
    else:
        form = SetPasswordForm()

    return render(request, "core/password_reset_new.html", {
        "form": form,
        "organization": org,
    })


# ============================================================================
# Voter Dashboard & Voting
# ============================================================================
@login_required
def voter_dashboard(request, slug):
    """Voter dashboard showing 3-tab category system."""
    org = get_object_or_404(Organization, slug=slug)

    if not hasattr(request.user, "studentvoter") or request.user.studentvoter.organization != org:
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter

    # Get all categories for this org, ordered by display_order
    categories = ElectionCategory.objects.filter(organization=org).order_by("display_order", "name")

    # Build tab data — ONLY live elections in tabs
    tab_data = []
    for category in categories:
        elections = Election.objects.filter(
            category=category,
            state="LIVE",
        ).order_by("-created_at")

        election_data = []
        for election in elections:
            positions = election.positions.all()
            position_status = []
            for pos in positions:
                position_status.append({
                    "position": pos,
                    "voted": voter.has_voted_for_position(pos),
                })
            election_data.append({
                "election": election,
                "positions": position_status,
                 "all_voted": all(p["voted"] for p in position_status) if position_status else False,
            })

        tab_data.append({
            "category": category,
            "elections": election_data,
        })

    # Past elections for results
    past_elections = Election.objects.filter(
        organization=org,
        state__in=["CLOSED", "ARCHIVED"],
    ).order_by("-actual_closed_at")

    return render(request, "core/voter_dashboard.html", {
        "organization": org,
        "voter": voter,
        "tab_data": tab_data,
        "past_elections": past_elections,
    })

@login_required
def state_register(request, slug):
    """Register for a state association."""
    org = get_object_or_404(Organization, slug=slug)

    if not hasattr(request.user, "studentvoter") or request.user.studentvoter.organization != org:
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter

    # Check if already registered for any state
    if voter.state_memberships.exists():
        messages.info(request, "You are already registered for a state association. Use the change page to update.")
        return redirect("voter_dashboard", slug=slug)

    # Check if any state association election is live (locked)
    state_category = ElectionCategory.objects.filter(
        organization=org, category_type="STATE_ASSOCIATION"
    ).first()

    if state_category:
        live_elections = Election.objects.filter(category=state_category, state="LIVE")
        if live_elections.exists():
            messages.error(request, "State association registration is locked because an election is currently live.")
            return redirect("voter_dashboard", slug=slug)

    if request.method == "POST":
        state = request.POST.get("state")
        if not state:
            messages.error(request, "Please select your state of origin.")
        else:
            from .models import StateAssociationMembership
            StateAssociationMembership.objects.create(voter=voter, state=state)
            messages.success(request, f"Successfully registered for {dict(StateAssociationMembership.NIGERIAN_STATES).get(state, state)} State Association.")
            return redirect("voter_dashboard", slug=slug)

    from .models import StateAssociationMembership
    return render(request, "core/state_register.html", {
        "organization": org,
        "voter": voter,
        "states": StateAssociationMembership.NIGERIAN_STATES,
    })


@login_required
def state_change(request, slug):
    """Change state association membership (only if no live election)."""
    org = get_object_or_404(Organization, slug=slug)

    if not hasattr(request.user, "studentvoter") or request.user.studentvoter.organization != org:
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter
    from .models import StateAssociationMembership

    # Check if any state association election is live
    state_category = ElectionCategory.objects.filter(
        organization=org, category_type="STATE_ASSOCIATION"
    ).first()

    if state_category:
        live_elections = Election.objects.filter(category=state_category, state="LIVE")
        if live_elections.exists():
            messages.error(request, "Cannot change state association while an election is live.")
            return redirect("voter_dashboard", slug=slug)

    if request.method == "POST":
        new_state = request.POST.get("state")
        if not new_state:
            messages.error(request, "Please select a state.")
        else:
            # Delete old membership, create new
            voter.state_memberships.all().delete()
            StateAssociationMembership.objects.create(voter=voter, state=new_state)
            messages.success(request, f"State association updated to {dict(StateAssociationMembership.NIGERIAN_STATES).get(new_state, new_state)}.")
            return redirect("voter_dashboard", slug=slug)

    current_membership = voter.state_memberships.first()
    return render(request, "core/state_change.html", {
        "organization": org,
        "voter": voter,
        "states": StateAssociationMembership.NIGERIAN_STATES,
        "current_state": current_membership.state if current_membership else None,
    })

@login_required
def ballot_view(request, slug, election_id):
    """Display the ballot for a live election."""
    org = get_object_or_404(Organization, slug=slug)
    election = get_object_or_404(Election, id=election_id, organization=org)

    if not hasattr(request.user, "studentvoter") or request.user.studentvoter.organization != org:
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter

    if election.state != "LIVE":
        messages.error(request, "This election is not currently open for voting.")
        return redirect("voter_dashboard", slug=slug)

    # Check eligibility
    if not is_voter_eligible(voter, election):
        messages.error(request, "You are not eligible to vote in this election.")
        return redirect("voter_dashboard", slug=slug)

    positions = election.positions.prefetch_related("candidates")

    # For each position, show only APPROVED candidates and mark if voted
    ballot_data = []
    for position in positions:
        candidates = position.candidates.filter(status="APPROVED")
        # Ballots are intentionally anonymous: Vote has no voter foreign key.
        # Therefore the UI can truthfully show participation status, but must
        # never infer a voter's selected candidate from another person's vote.
        ballot_data.append({
            "position": position,
            "candidates": candidates,
            "voted": voter.has_voted_for_position(position),
        })

    return render(request, "core/ballot.html", {
        "organization": org,
        "election": election,
        "ballot_data": ballot_data,
        "voter": voter,
    })


@login_required
@require_POST
def cast_vote_view(request, slug, election_id, position_id):
    """Handle vote casting."""
    org = get_object_or_404(Organization, slug=slug)
    election = get_object_or_404(Election, id=election_id, organization=org)
    position = get_object_or_404(Position, id=position_id, election=election)

    if not hasattr(request.user, "studentvoter") or request.user.studentvoter.organization != org:
        return JsonResponse({"success": False, "message": "Access denied."})

    voter = request.user.studentvoter

    if election.state != "LIVE":
        return JsonResponse({"success": False, "message": "Election is not live."})

    if not is_voter_eligible(voter, election):
        return JsonResponse({"success": False, "message": "Not eligible to vote."})

    candidate_id = request.POST.get("candidate_id")
    if not candidate_id:
        return JsonResponse({"success": False, "message": "No candidate selected."})

    try:
        candidate = Candidate.objects.get(
            id=candidate_id, position=position, status="APPROVED"
        )
    except Candidate.DoesNotExist:
        return JsonResponse({"success": False, "message": "Invalid candidate."})

    success, message = cast_vote(voter, position, candidate)

    if success:
        if request.htmx:
            return render(request, "core/partials/vote_success.html", {
                "position": position,
                "candidate": candidate,
            })
        messages.success(request, message)
    else:
        if request.htmx:
            return render(request, "core/partials/vote_error.html", {
                "message": message,
            })
        messages.error(request, message)

    return redirect("ballot_view", slug=slug, election_id=election_id)


def is_voter_eligible(voter, election):
    """Check if a voter is eligible for an election based on category and rules."""
    # Must be in same organization
    if voter.organization != election.organization:
        return False

    # Must be activated
    if not voter.is_activated:
        return False

    # Category-based eligibility
    category = election.category
    if not category:
        return False

    if category.category_type == "FACULTY":
        # Must belong to the faculty this election is for
        if voter.faculty and category.name:
            return voter.faculty.lower() in category.name.lower()
        return False

    elif category.category_type == "STATE_ASSOCIATION":
        # Must be registered for the state this election is for
        from .models import StateAssociationMembership
        membership = StateAssociationMembership.objects.filter(
            voter=voter, state=category.name
        ).exists()
        return membership

    elif category.category_type == "SUG":
        # All students eligible (when SUG is activated)
        return True

    # Fallback
    return False

#@login_required
def public_election_results(request, slug, election_id):
    """Display election results (only when CLOSED or ARCHIVED)."""
    org = get_object_or_404(Organization, slug=slug)
    election = get_object_or_404(Election, id=election_id, organization=org)

    # Results are ONLY visible when CLOSED or ARCHIVED (Blueprint §8)
    if election.state not in ["CLOSED", "ARCHIVED"]:
        messages.error(request, "Results are not yet available for this election.")
        return redirect("organization_landing", slug=slug)

    # Get tallies per position
    results = []
    for position in election.positions.prefetch_related("candidates"):
        candidates = position.candidates.filter(status="APPROVED")
        candidate_votes = []
        for candidate in candidates:
            vote_count = Vote.objects.filter(candidate=candidate).count()
            candidate_votes.append({
                "candidate": candidate,
                "votes": vote_count,
            })

        # Sort by votes descending
        candidate_votes.sort(key=lambda x: x["votes"], reverse=True)

        # Check for tie (top two have same votes)
        is_tie = False
        if len(candidate_votes) >= 2:
            if candidate_votes[0]["votes"] == candidate_votes[1]["votes"]:
                is_tie = True

        total_votes = sum(c["votes"] for c in candidate_votes)

        results.append({
            "position": position,
            "candidates": candidate_votes,
            "total_votes": total_votes,
            "is_tie": is_tie,
        })

    # Get total eligible voters and turnout
    total_eligible = StudentVoter.objects.filter(
        organization=org, is_activated=True
    ).count()

    # Count unique voters who voted in this election
    voted_positions = set()
    for position in election.positions.all():
        voted_positions.update(
            StudentVoter.objects.filter(
                voted_positions=position
            ).values_list("id", flat=True)
        )
    total_voters = len(voted_positions)
    turnout_percentage = (total_voters / total_eligible * 100) if total_eligible > 0 else 0

    return render(request, "core/election_results.html", {
        "organization": org,
        "election": election,
        "results": results,
        "total_eligible": total_eligible,
        "total_voters": total_voters,
        "turnout_percentage": round(turnout_percentage, 2),
    })


# ============================================================================
# Organization Admin Dashboard
# ============================================================================
@login_required
@org_admin_required
def admin_dashboard(request):
    """Organization Admin main dashboard."""
    org = get_org_from_request(request)

    elections = Election.objects.filter(organization=org).order_by("-created_at")
    live_elections = elections.filter(state="LIVE")
    closed_elections = elections.filter(state__in=["CLOSED", "ARCHIVED"])
    total_voters = StudentVoter.objects.filter(organization=org, is_activated=True).count()
    total_verified = VerifiedVoterRecord.objects.filter(organization=org).count()

    # Recent audit logs
    recent_logs = AuditLog.objects.filter(organization=org)[:20]

    # SUBSCRIPTION LOCK [Blueprint §11.2] — view-only when expired.
    # Dashboard remains fully viewable; only NEW-creation actions are blocked.
    subscription_active = org.is_subscription_active()


    return render(request, "core/admin_dashboard.html", {
        "organization": org,
        "elections": elections,
        "live_elections": live_elections,
        "closed_elections": closed_elections,
        "total_voters": total_voters,
        "total_verified": total_verified,
        "recent_logs": recent_logs,
        "subscription_active": subscription_active,
    })


@login_required
@org_admin_required
def export_organization_data(request):
    """
    Data export on subscription end [Blueprint §17].
    Available at ALL times — an Organization never loses access to its
    own historical records. Excludes raw individual Vote records
    (anonymous by design) and excludes Platform-level or other
    Organizations' data.
    """
    org = get_org_from_request(request)

    export_data = {
        "organization": {
            "name": org.name,
            "slug": org.slug,
            "exported_at": timezone.now().isoformat(),
        },
        "elections": [],
    }

    elections = Election.objects.filter(organization=org)
    for election in elections:
        election_data = {
            "title": election.title,
            "state": election.state,
            "voting_opens_at": str(election.voting_opens_at),
            "voting_closes_at": str(election.voting_closes_at),
            "final_tally_hash": election.final_tally_hash,
            "results_published_at": str(election.results_published_at),
            "positions": [],
        }
        for position in election.positions.all():
            position_data = {
                "name": position.name,
                "candidates": [],
            }
            for candidate in position.candidates.all():
                # Vote COUNT only — never linked to any individual voter
                vote_count = candidate.votes.count()
                position_data["candidates"].append({
                    "name": candidate.name,
                    "status": candidate.status,
                    "vote_count": vote_count,
                })
            election_data["positions"].append(position_data)
        export_data["elections"].append(election_data)

    log_action(
        organization=org,
        action_type="DATA_EXPORT",
        description=f"Organization data exported by {request.user.email}",
        actor=request.user.email,
    )

    response = JsonResponse(export_data, json_dumps_params={"indent": 2})
    response["Content-Disposition"] = f'attachment; filename="{org.slug}_export_{timezone.now().date()}.json"'
    return response

@login_required
@org_admin_required
def category_create(request):
    """Create a new election category (Faculty, State Association, SUG)."""
    org = get_org_from_request(request)

    if not org.is_subscription_active():
        messages.error(request, "Subscription expired. Cannot create categories.")
        return redirect("admin_dashboard")

    if request.method == "POST":
        form = ElectionCategoryForm(request.POST)
        if form.is_valid():
            category = form.save(commit=False)
            category.organization = org
            # Auto-generate slug from name
            if not category.slug:
                base_slug = category.name.lower().replace(" ", "-")
                slug = base_slug
                counter = 1
                from .models import ElectionCategory
                while ElectionCategory.objects.filter(slug=slug).exists():
                    slug = f"{base_slug}-{counter}"
                    counter += 1
                category.slug = slug
            category.save()

            log_action(
                organization=org,
                action_type="CATEGORY_CREATED",
                description=f"Election category '{category.name}' created",
                actor=request.user.email,
            )

            messages.success(request, f"Category '{category.name}' created successfully.")
            return redirect("admin_dashboard")
    else:
        form = ElectionCategoryForm()

    return render(request, "core/admin_category_form.html", {
        "form": form,
        "organization": org,
    })

@login_required
@org_admin_required
def election_create(request):
    """Create a new election."""
    org = get_org_from_request(request)

    # SUBSCRIPTION ENFORCEMENT [Blueprint §11.2]
    # Block NEW elections when subscription has expired.
    # LIVE elections are NEVER interrupted by subscription status.
    if not org.is_subscription_active():
        messages.error(
            request,
            "Your organization's subscription has expired. "
            "New elections cannot be created until renewal. "
            "Existing live elections continue running normally."
        )
        return redirect("admin_dashboard")

    if request.method == "POST":
        form = ElectionForm(request.POST)
        if form.is_valid():
            election = form.save(commit=False)
            election.organization = org
            # Organization is derived from category, but we keep it explicit for queries
            election.save()

            log_action(
                organization=org,
                action_type="ELECTION_CREATED",
                description=f"Election '{election.title}' created",
                actor=request.user.email,
                election=election,
            )

            messages.success(request, f"Election '{election.title}' created successfully.")
            return redirect("admin_election_detail", election_id=election.id)
    else:
        form = ElectionForm()

    return render(request, "core/admin_election_form.html", {
        "form": form,
        "organization": org,
        "action": "Create",
    })


@login_required
@org_admin_required
def election_detail(request, election_id):
    """View and manage a specific election."""
    org = get_org_from_request(request)
    election = get_object_or_404(Election, id=election_id, organization=org)
    positions = election.positions.prefetch_related("candidates")

    return render(request, "core/admin_election_detail.html", {
        "organization": org,
        "election": election,
        "positions": positions,
    })


@login_required
@org_admin_required
def election_transition(request, election_id):
    """Transition election state (DRAFT→LIVE, LIVE→CLOSED)."""
    org = get_org_from_request(request)
    election = get_object_or_404(Election, id=election_id, organization=org)

    if request.method == "POST":
        new_state = request.POST.get("new_state")

        try:
            transition_election_state(election, new_state, actor=request.user.email)
            messages.success(request, f"Election transitioned to {new_state}.")

            if new_state == "LIVE":
                messages.info(request, "The election is now LIVE. Voting has begun.")
            elif new_state == "CLOSED":
                messages.info(request, "The election is now CLOSED. Results have been published.")

        except Exception as e:
            messages.error(request, f"Failed to transition election: {str(e)}")

    return redirect("admin_election_detail", election_id=election_id)


@login_required
@org_admin_required
def position_create(request, election_id):
    """Add a position to an election."""
    org = get_org_from_request(request)
    election = get_object_or_404(Election, id=election_id, organization=org)

    # SUBSCRIPTION ENFORCEMENT [Blueprint §11.2]
    if not org.is_subscription_active():
        messages.error(request, "Subscription expired. Cannot modify election configuration.")
        return redirect("admin_election_detail", election_id=election_id)

    if request.method == "POST":
        form = PositionForm(request.POST)
        if form.is_valid():
            position = form.save(commit=False)
            position.election = election
            position.save()
            messages.success(request, f"Position '{position.name}' added.")
            return redirect("admin_election_detail", election_id=election_id)
    else:
        form = PositionForm()

    return render(request, "core/admin_position_form.html", {
        "form": form,
        "organization": org,
        "election": election,
    })


@login_required
@org_admin_required
def candidate_create(request, position_id):
    """Add a candidate to a position."""
    org = get_org_from_request(request)
    position = get_object_or_404(Position, id=position_id, election__organization=org)

    # SUBSCRIPTION ENFORCEMENT [Blueprint §11.2]
    if not org.is_subscription_active():
        messages.error(request, "Subscription expired. Cannot add candidates.")
        return redirect("admin_election_detail", election_id=position.election.id)

    if request.method == "POST":
        form = CandidateForm(request.POST, request.FILES)
        if form.is_valid():
            candidate = form.save(commit=False)
            candidate.position = position
            candidate.save()
            messages.success(request, f"Candidate '{candidate.name}' added.")
            return redirect("admin_election_detail", election_id=position.election.id)
    else:
        form = CandidateForm()

    return render(request, "core/admin_candidate_form.html", {
        "form": form,
        "organization": org,
        "position": position,
    })


@login_required
@org_admin_required
def candidate_screen(request, candidate_id):
    """Screen (approve/reject/withdraw) a candidate."""
    org = get_org_from_request(request)
    candidate = get_object_or_404(
        Candidate, id=candidate_id, position__election__organization=org
    )

    if request.method == "POST":
        form = CandidateStatusForm(request.POST, instance=candidate)
        if form.is_valid():
            old_status = candidate.status
            candidate = form.save()

            log_action(
                organization=org,
                action_type=f"CANDIDATE_{candidate.status}",
                description=f"Candidate '{candidate.name}' {candidate.status.lower()} by {request.user.email}",
                actor=request.user.email,
                election=candidate.position.election,
                metadata={
                    "candidate_id": candidate.id,
                    "old_status": old_status,
                    "new_status": candidate.status,
                },
            )

            messages.success(request, f"Candidate '{candidate.name}' is now {candidate.status}.")
            return redirect("admin_election_detail", election_id=candidate.position.election.id)
    else:
        form = CandidateStatusForm(instance=candidate)

    return render(request, "core/admin_candidate_screen.html", {
        "form": form,
        "organization": org,
        "candidate": candidate,
    })

@login_required
@org_admin_required
def candidate_delete(request, candidate_id):
    """Delete a candidate. Only allowed if election is not LIVE."""
    org = get_org_from_request(request)
    candidate = get_object_or_404(
        Candidate, id=candidate_id, position__election__organization=org
    )

    election = candidate.position.election

    if election.state == "LIVE":
        messages.error(request, "Cannot delete candidates while election is LIVE.")
        return redirect("admin_election_detail", election_id=election.id)

    if request.method == "POST":
        candidate_name = candidate.name
        candidate.delete()

        log_action(
            organization=org,
            action_type="CANDIDATE_DELETED",
            description=f"Candidate '{candidate_name}' deleted by {request.user.email}",
            actor=request.user.email,
            election=election,
        )

        messages.success(request, f"Candidate '{candidate_name}' has been deleted.")
        return redirect("admin_election_detail", election_id=election.id)

    return render(request, "core/admin_candidate_delete.html", {
        "organization": org,
        "candidate": candidate,
        "election": election,
    })

@login_required
@org_admin_required
def voter_import(request):
    """Import verified voter records from CSV."""
    org = get_org_from_request(request)

    if request.method == "POST":
        form = CSVImportForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = request.FILES["csv_file"]
            decoded_file = csv_file.read().decode("utf-8")
            io_string = io.StringIO(decoded_file)
            reader = csv.DictReader(io_string)

            success_count = 0
            error_count = 0
            errors = []

            for row in reader:
                try:
                    matric = row.get("matric_number", "").strip().upper()
                    email = row.get("official_email", "").strip() or None
                    phone = row.get("official_phone", "").strip() or None
                    level = row.get("level", "").strip()
                    department = row.get("department", "").strip()
                    faculty = row.get("faculty", "").strip()

                    if not matric:
                        error_count += 1
                        errors.append(f"Row missing matric_number")
                        continue

                    VerifiedVoterRecord.objects.update_or_create(
                        organization=org,
                        matric_number=matric,
                        defaults={
                            "official_email": email,
                            "official_phone": phone,
                            "level": int(level) if level and level.isdigit() else None,
                            "department": department,
                            "faculty": faculty,
                            "imported_by": request.user.email,
                        },
                    )
                    success_count += 1

                except Exception as e:
                    error_count += 1
                    errors.append(f"Row error: {str(e)}")

            log_action(
                organization=org,
                action_type="CSV_IMPORT",
                description=f"CSV import: {success_count} succeeded, {error_count} failed",
                actor=request.user.email,
                metadata={"success_count": success_count, "error_count": error_count, "errors": errors[:10]},
            )

            messages.success(request, f"Import complete: {success_count} records imported, {error_count} errors.")
            if errors:
                for err in errors[:5]:
                    messages.warning(request, err)

            return redirect("admin_dashboard")
    else:
        form = CSVImportForm()

    return render(request, "core/admin_voter_import.html", {
        "form": form,
        "organization": org,
    })


@login_required
@org_admin_required
def admin_results(request, election_id):
    """View results for a closed election (same data as public, but admin access)."""
    org = get_org_from_request(request)
    election = get_object_or_404(Election, id=election_id, organization=org)

    if election.state not in ["CLOSED", "ARCHIVED"]:
        messages.error(request, "Results are only available after the election is closed.")
        return redirect("admin_election_detail", election_id=election_id)

    # Reuse the same results logic
    results = []
    for position in election.positions.prefetch_related("candidates"):
        candidates = position.candidates.filter(status="APPROVED")
        candidate_votes = []
        for candidate in candidates:
            vote_count = Vote.objects.filter(candidate=candidate).count()
            candidate_votes.append({
                "candidate": candidate,
                "votes": vote_count,
            })
        candidate_votes.sort(key=lambda x: x["votes"], reverse=True)

        is_tie = False
        if len(candidate_votes) >= 2 and candidate_votes[0]["votes"] == candidate_votes[1]["votes"]:
            is_tie = True

        total_votes = sum(c["votes"] for c in candidate_votes)

        results.append({
            "position": position,
            "candidates": candidate_votes,
            "total_votes": total_votes,
            "is_tie": is_tie,
        })

    return render(request, "core/admin_results.html", {
        "organization": org,
        "election": election,
        "results": results,
    })

@login_required
@org_admin_required
def admin_election_results_export(request, election_id):
    """Export a single election's results as CSV."""
    org = get_org_from_request(request)
    election = get_object_or_404(Election, id=election_id, organization=org)

    if election.state not in ["CLOSED", "ARCHIVED"]:
        messages.error(request, "Results are only available for closed or archived elections.")
        return redirect("admin_election_detail", election_id=election.id)

    # Build results data
    results_data = []
    for position in election.positions.prefetch_related("candidates"):
        candidates = position.candidates.filter(status="APPROVED")
        for candidate in candidates:
            vote_count = Vote.objects.filter(candidate=candidate).count()
            results_data.append({
                "position": position.name,
                "candidate": candidate.name,
                "status": candidate.status,
                "votes": vote_count,
            })

    # Generate CSV
    response = HttpResponse(content_type='text/csv')
    safe_title = election.title.replace(" ", "_").replace("/", "-")
    response['Content-Disposition'] = f'attachment; filename="{safe_title}_results.csv"'

    writer = csv.writer(response)
    writer.writerow(['Position', 'Candidate', 'Status', 'Votes'])

    for row in results_data:
        writer.writerow([row["position"], row["candidate"], row["status"], row["votes"]])

    # Log the export
    log_action(
        organization=org,
        action_type="ELECTION_RESULTS_EXPORTED",
        description=f"Results exported for '{election.title}' by {request.user.email}",
        actor=request.user.email,
        election=election,
    )

    return response

@login_required
@org_admin_required
def admin_audit_log(request):
    """View and filter audit log for the organization."""
    org = get_org_from_request(request)
    
    logs = AuditLog.objects.filter(organization=org).select_related("election")
    
    # Get elections for the filter dropdown
    elections = Election.objects.filter(organization=org).order_by("-created_at")
    
    # Apply filters
    actor_search = request.GET.get("actor", "").strip()
    actor_type = request.GET.get("actor_type", "")
    election_filter = request.GET.get("election", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    
    if actor_search:
        logs = logs.filter(actor__icontains=actor_search)
    
    if actor_type == "SYSTEM":
        logs = logs.filter(actor="SYSTEM")
    elif actor_type == "ADMIN":
        logs = logs.exclude(actor="SYSTEM")
    
    if election_filter:
        try:
            election_id = int(election_filter)
            logs = logs.filter(election_id=election_id)
        except (ValueError, TypeError):
            pass
    
    if date_from:
        try:
            logs = logs.filter(timestamp__date__gte=datetime.strptime(date_from, "%Y-%m-%d").date())
        except (ValueError, TypeError):
            pass
    
    if date_to:
        try:
            logs = logs.filter(timestamp__date__lte=datetime.strptime(date_to, "%Y-%m-%d").date())
        except (ValueError, TypeError):
            pass
    
    logs = logs[:500]  # Limit to prevent huge page loads
    
    # Build query string for preserving filters
    query_params = request.GET.copy()
    if "export" in query_params:
        del query_params["export"]
    query_string = query_params.urlencode()
    
    return render(request, "core/admin_audit_log.html", {
        "organization": org,
        "logs": logs,
        "elections": elections,
        "query_string": query_string,
        "filters": {
            "actor": actor_search,
            "actor_type": actor_type,
            "election": election_filter,
            "date_from": date_from,
            "date_to": date_to,
        },
    })


@login_required
@org_admin_required
def admin_audit_log_export(request):
    """Export filtered audit log as CSV."""
    org = get_org_from_request(request)
    
    logs = AuditLog.objects.filter(organization=org).select_related("election")
    
    # Apply same filters as the main view
    actor_search = request.GET.get("actor", "").strip()
    actor_type = request.GET.get("actor_type", "")
    election_filter = request.GET.get("election", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    
    if actor_search:
        logs = logs.filter(actor__icontains=actor_search)
    
    if actor_type == "SYSTEM":
        logs = logs.filter(actor="SYSTEM")
    elif actor_type == "ADMIN":
        logs = logs.exclude(actor="SYSTEM")
    
    if election_filter:
        try:
            election_id = int(election_filter)
            logs = logs.filter(election_id=election_id)
        except (ValueError, TypeError):
            pass
    
    if date_from:
        try:
            logs = logs.filter(timestamp__date__gte=datetime.strptime(date_from, "%Y-%m-%d").date())
        except (ValueError, TypeError):
            pass
    
    if date_to:
        try:
            logs = logs.filter(timestamp__date__lte=datetime.strptime(date_to, "%Y-%m-%d").date())
        except (ValueError, TypeError):
            pass
    
    # Generate CSV
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="audit_log_{org.slug}_{timezone.now().date()}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Timestamp', 'Action', 'Election', 'Actor', 'Description'])
    
    for log in logs:
        writer.writerow([
            log.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            log.action_type,
            log.election.title if log.election else "",
            log.actor,
            log.description,
        ])
    
    # Log the export action
    log_action(
        organization=org,
        action_type="AUDIT_LOG_EXPORTED",
        description=f"Audit log exported by {request.user.email}",
        actor=request.user.email,
    )
    
    return response


# ============================================================================
# Election Officer (Observer) Dashboard [Blueprint §10]
# ============================================================================
@login_required
@election_officer_required
def observer_dashboard(request):
    """Election Observer dashboard: live turnout, system status, audit feed."""
    org = get_org_from_request(request)
    officer = request.user.electionofficer

    # Get assigned election — NO fallback to other elections
    election = officer.assigned_election

    if not election:
        return render(request, "core/observer_dashboard.html", {
            "organization": org,
            "election": None,
            "no_election": False,
            "no_assignment": True,
        })

    # LIVE TURNOUT: X of Y eligible voters have cast a ballot
    total_eligible = StudentVoter.objects.filter(
        organization=org, is_activated=True
    ).count()

    # Count unique voters who voted in at least one position in this election
    voted_voter_ids = set()
    for position in election.positions.all():
        ids = StudentVoter.objects.filter(
            voted_positions=position
        ).values_list("id", flat=True)
        voted_voter_ids.update(ids)
    turnout_count = len(voted_voter_ids)

    # Real-time audit log feed (last 50 actions)
    audit_feed = AuditLog.objects.filter(
        organization=org, election=election
    ).select_related("election")[:50]

    # System status
    now = timezone.now()
    time_remaining = None
    if election.state == "LIVE" and election.voting_closes_at:
        time_remaining = election.voting_closes_at - now
        if time_remaining.total_seconds() < 0:
            time_remaining = None

    # Check if results are available (only when CLOSED/ARCHIVED)
    results_available = election.state in ["CLOSED", "ARCHIVED"]

    return render(request, "core/observer_dashboard.html", {
        "organization": org,
        "election": election,
        "turnout_count": turnout_count,
        "total_eligible": total_eligible,
        "turnout_percentage": round((turnout_count / total_eligible * 100), 2) if total_eligible else 0,
        "audit_feed": audit_feed,
        "time_remaining": time_remaining,
        "results_available": results_available,
        "no_election": False,
        "no_assignment": False,
    })

# ============================================================================
# Platform Owner Views
# ============================================================================
@login_required
@platform_owner_required
def platform_dashboard(request):
    """Platform Owner dashboard: all organizations, subscriptions, onboarding."""
    organizations = Organization.objects.all().order_by("name")

    stats = {
        "total_orgs": organizations.count(),
        "active_orgs": organizations.filter(subscription_status="ACTIVE").count(),
        "expired_orgs": organizations.filter(subscription_status="EXPIRED").count(),
        "total_elections": Election.objects.count(),
        "live_elections": Election.objects.filter(state="LIVE").count(),
    }

    return render(request, "core/platform_dashboard.html", {
        "organizations": organizations,
        "stats": stats,
    })


@login_required
@platform_owner_required
def organization_onboard(request):
    """Onboard a new organization."""
    if request.method == "POST":
        form = OrganizationForm(request.POST, request.FILES)
        if form.is_valid():
            org = form.save()

            # Create the 3 admin accounts
            admin_emails = [
                request.POST.get("admin1_email"),
                request.POST.get("admin2_email"),
                request.POST.get("admin3_email"),
            ]
            primary_admin = request.POST.get("primary_admin")

            for i, email in enumerate(admin_emails):
                if email:
                    user, created = User.objects.get_or_create(
                        email=email,
                        defaults={
                            "username": email.split("@")[0],
                            "user_type": "ORG_ADMIN",
                        }
                    )
                    OrganizationAdmin.objects.create(
                        organization=org,
                        user=user,
                        is_primary=(email == primary_admin),
                    )

            log_action(
                organization=org,
                action_type="ORGANIZATION_ONBOARDED",
                description=f"Organization '{org.name}' onboarded",
                actor=request.user.email,
            )

            messages.success(request, f"Organization '{org.name}' onboarded successfully.")
            return redirect("platform_dashboard")
    else:
        form = OrganizationForm()

    return render(request, "core/platform_organization_form.html", {
        "form": form,
        "action": "Onboard",
    })
