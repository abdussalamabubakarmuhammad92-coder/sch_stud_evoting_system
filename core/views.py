"""
Views for SUG E-Voting Platform.
Organized by user role and functionality.

Single-school deployment: the multi-tenancy layer (organizations, platform
owner, org-slug routing, invitation codes and subscription gating) has been
removed. One deployment serves one school.
"""
import csv
import io
from datetime import datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_http_methods
from django.utils import timezone
from django.conf import settings


from .models import (
    User,
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
    StateAssociationMembership,
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
)


# ============================================================================
# Mixins & Decorators
# ============================================================================
def org_admin_required(view_func):
    """Decorator ensuring user is a School Admin."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("admin_login")
        if request.user.user_type != "ADMIN":
            messages.error(request, "Access denied. School Admin required.")
            return redirect("home")
        return view_func(request, *args, **kwargs)
    return wrapper


def election_officer_required(view_func):
    """Decorator ensuring user is an Election Officer."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("admin_login")
        if not hasattr(request.user, "electionofficer"):
            messages.error(request, "Access denied. Election Officer required.")
            return redirect("home")
        return view_func(request, *args, **kwargs)
    return wrapper


# ============================================================================
# Public Views
# ============================================================================
def home(request):
    """Landing page for the school's voting portal."""
    refresh_states_and_commit = None  # elections open/close via the periodic task

    live_elections = Election.objects.filter(state="LIVE").select_related("category")
    past_elections = Election.objects.filter(
        state__in=["CLOSED", "ARCHIVED"],
    ).order_by("-actual_closed_at")[:5]

    return render(request, "core/home.html", {
        "live_elections": live_elections,
        "past_elections": past_elections,
    })


# ============================================================================
# Authentication Views
# ============================================================================
def voter_login(request):
    """Voter login using matric number + password."""
    if request.method == "POST":
        form = VoterLoginForm(request.POST)
        if form.is_valid():
            matric = form.cleaned_data["matric_number"]
            password = form.cleaned_data["password"]

            try:
                voter = StudentVoter.objects.get(matric_number=matric.upper())
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
                        return redirect("new_device_otp")

                    login(request, user)
                    register_device(voter, request)
                    messages.success(request, f"Welcome back, {voter.matric_number}!")
                    return redirect("voter_dashboard")
                else:
                    log_action(
                        action_type="VOTER_LOGIN_FAILED",
                        description="Login failed: incorrect password.",
                        actor=matric,
                        severity=SEVERITY_SECURITY,
                    )
                    messages.error(request, "Invalid password.")
            except StudentVoter.DoesNotExist:
                log_action(
                    action_type="VOTER_LOGIN_FAILED",
                    description="Login failed: matric number not found.",
                    actor=matric,
                    severity=SEVERITY_SECURITY,
                )
                messages.error(request, "Matric number not found.")
    else:
        form = VoterLoginForm()

    return render(request, "core/voter_login.html", {"form": form})


def admin_login(request):
    """Admin/Officer login using email + password."""
    if request.method == "POST":
        form = AdminLoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)

            if hasattr(user, "electionofficer"):
                return redirect("observer_dashboard")
            else:
                return redirect("admin_dashboard")
        else:
            log_action(
                action_type="STAFF_LOGIN_FAILED",
                description="Failed staff login attempt.",
                actor=request.POST.get("username", "unknown"),
                severity=SEVERITY_SECURITY,
            )
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
def new_device_otp(request):
    """Verify OTP for new device login."""
    voter_id = request.session.get("pending_login_voter_id")

    if not voter_id:
        messages.error(request, "Session expired. Please log in again.")
        return redirect("voter_login")

    voter = get_object_or_404(StudentVoter, id=voter_id)

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
                    return redirect("voter_login")


                if otp.is_locked():
                    log_action(
                        action_type="OTP_LOCKED",
                        description="New-device verification locked after repeated failures.",
                        actor=voter.matric_number,
                        severity=SEVERITY_SECURITY,
                    )
                    messages.error(request, "Too many incorrect attempts")
                    del request.session["pending_login_voter_id"]
                    return redirect("voter_login")

                if otp.verify_code(code):
                    otp.used = True
                    otp.save()

                    register_device(voter, request)
                    login(request, voter.user)

                    del request.session["pending_login_voter_id"]
                    redirect_url = request.session.pop("pending_login_redirect", "voter_dashboard")

                    log_action(
                        action_type="NEW_DEVICE_LOGIN",
                        description=f"New device verified for {voter.matric_number}",
                        actor=voter.matric_number,
                    )

                    messages.success(request, "Device verified successfully!")
                    return redirect(redirect_url)
                else:
                    otp.register_failed_attempt()
                    log_action(
                        action_type="OTP_VERIFY_FAILED",
                        description="Incorrect code entered for new-device verification.",
                        actor=voter.matric_number,
                        severity=SEVERITY_SECURITY,
                    )
                    messages.error(request, "Invalid OTP code.")
            except OTPVerification.DoesNotExist:
                messages.error(request, "No pending OTP found. Please log in again.")
                return redirect("voter_login")
    else:
        form = OTPVerificationForm()

    return render(request, "core/otp_verify.html", {
        "form": form,
        "purpose": "new_device",
    })


# ============================================================================
# Voter Registration Flow (3 Steps)
# ============================================================================
def voter_register_step1(request):
    """
    Step 1: Student enters matric, email, phone.
    System validates against VerifiedVoterRecord with strict-match rule.
    """
    if request.method == "POST":
        form = VoterRegistrationForm(request.POST)
        if form.is_valid():
            matric = form.cleaned_data["matric_number"]
            email = form.cleaned_data["email"]
            phone = form.cleaned_data["phone_number"]

            # Get verified record
            vvr = VerifiedVoterRecord.objects.get(matric_number=matric)

            # Create unactivated StudentVoter
            voter = StudentVoter.objects.create(
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
            return redirect("voter_register_step2")
    else:
        form = VoterRegistrationForm()

    return render(request, "core/voter_register_step1.html", {"form": form})


def voter_register_step2(request):
    """Step 2: Verify OTP."""
    voter_id = request.session.get("registration_voter_id")

    if not voter_id:
        messages.error(request, "Registration session expired. Please start again.")
        return redirect("voter_register_step1")

    voter = get_object_or_404(StudentVoter, id=voter_id)

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
                    return redirect("voter_register_step2")

                if otp.is_locked():
                    log_action(
                        action_type="OTP_LOCKED",
                        description="Registration verification locked after repeated failures.",
                        actor=voter.matric_number,
                        severity=SEVERITY_SECURITY,
                    )
                    messages.error(request, "Too many incorrect attempts. Please request a new code.")
                    return redirect("voter_register_step2")

                if otp.verify_code(code):
                    otp.used = True
                    otp.save()

                    # Re-set session variables and force save
                    request.session["registration_voter_id"] = voter.id
                    request.session["registration_verified"] = True
                    request.session.modified = True

                    messages.success(request, "OTP verified! Now set your password.")
                    return redirect("voter_register_step3")
                else:
                    otp.register_failed_attempt()
                    log_action(
                        action_type="OTP_VERIFY_FAILED",
                        description="Incorrect code entered for registration.",
                        actor=voter.matric_number,
                        severity=SEVERITY_SECURITY,
                    )
                    messages.error(request, "Invalid OTP code. Please try again.")
            except OTPVerification.DoesNotExist:
                messages.error(request, "No pending OTP found. Please start registration again.")
                return redirect("voter_register_step1")
    else:
        form = OTPVerificationForm()

    return render(request, "core/otp_verify.html", {
        "form": form,
        "purpose": "registration",
        "voter": voter,
    })

def voter_register_step3(request):
    """Step 3: Set password and activate account."""
    voter_id = request.session.get("registration_voter_id")
    verified = request.session.get("registration_verified", False)

    if not voter_id or not verified:
        messages.error(request, "Registration session expired. Please start again.")
        return redirect("voter_register_step1")

    voter = get_object_or_404(StudentVoter, id=voter_id)

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
                action_type="VOTER_REGISTERED",
                description=f"Voter {voter.matric_number} completed registration",
                actor=voter.matric_number,
            )

            messages.success(request, "Registration complete! Welcome to the platform.")
            return redirect("voter_dashboard")
    else:
        form = SetPasswordForm()

    return render(request, "core/voter_register_step3.html", {
        "form": form,
        "voter": voter,
    })


# ============================================================================
# Password Reset Flow
# ============================================================================
def password_reset_request(request):
    """Request password reset via matric number."""
    if request.method == "POST":
        form = PasswordResetRequestForm(request.POST)
        if form.is_valid():
            matric = form.cleaned_data["matric_number"]

            try:
                voter = StudentVoter.objects.get(
                    matric_number=matric.upper(), is_activated=True
                )

                # Generate OTP
                code, otp = create_otp_verification(voter, "PASSWORD_RESET")
                send_otp(voter, code, "PASSWORD_RESET")

                request.session["reset_voter_id"] = voter.id

                log_action(
                    action_type="PASSWORD_RESET_REQUESTED",
                    description=f"Password reset requested for {matric}",
                    actor=matric,
                )

                messages.info(request, "A reset code has been sent to your registered contact.")
                return redirect("password_reset_verify")

            except StudentVoter.DoesNotExist:
                # Don't reveal whether matric exists
                messages.info(request, "If this matric number is registered, a reset code has been sent.")
                return redirect("voter_login")
    else:
        form = PasswordResetRequestForm()

    return render(request, "core/password_reset_request.html", {"form": form})


def password_reset_verify(request):
    """Verify OTP for password reset."""
    voter_id = request.session.get("reset_voter_id")

    if not voter_id:
        messages.error(request, "Session expired. Please try again.")
        return redirect("password_reset_request")

    voter = get_object_or_404(StudentVoter, id=voter_id)

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
                    return redirect("password_reset_request")

                if otp.is_locked():
                    log_action(
                        action_type="OTP_LOCKED",
                        description="Password-reset verification locked after repeated failures.",
                        actor=voter.matric_number,
                        severity=SEVERITY_SECURITY,
                    )
                    messages.error(request, "Too many incorrect attempts. Please request a new code")
                    return redirect("password_reset_request")

                if otp.verify_code(code):
                    otp.used = True
                    otp.save()
                    request.session["reset_verified"] = True
                    messages.success(request, "Code verified! Enter your new password.")
                    return redirect("password_reset_new")
                else:
                    otp.register_failed_attempt()
                    log_action(
                        action_type="OTP_VERIFY_FAILED",
                        description="Incorrect code entered for password reset.",
                        actor=voter.matric_number,
                        severity=SEVERITY_SECURITY,
                    )
                    messages.error(request, "Invalid code.")
            except OTPVerification.DoesNotExist:
                messages.error(request, "No pending reset found.")
                return redirect("password_reset_request")
    else:
        form = OTPVerificationForm()

    return render(request, "core/otp_verify.html", {
        "form": form,
        "purpose": "password_reset",
    })


def password_reset_new(request):
    """Set new password after OTP verification."""
    voter_id = request.session.get("reset_voter_id")
    verified = request.session.get("reset_verified", False)

    if not voter_id or not verified:
        messages.error(request, "Session expired. Please try again.")
        return redirect("password_reset_request")

    voter = get_object_or_404(StudentVoter, id=voter_id)

    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            password = form.cleaned_data["password"]
            voter.user.set_password(password)
            voter.user.save()

            del request.session["reset_voter_id"]
            del request.session["reset_verified"]

            log_action(
                action_type="PASSWORD_RESET_COMPLETED",
                description=f"Password reset completed for {voter.matric_number}",
                actor=voter.matric_number,
            )

            messages.success(request, "Password updated successfully! Please log in.")
            return redirect("voter_login")
    else:
        form = SetPasswordForm()

    return render(request, "core/password_reset_new.html", {"form": form})


# ============================================================================
# Voter Dashboard & Voting
# ============================================================================
@login_required
def voter_dashboard(request):
    """Voter dashboard showing 3-tab category system."""
    if not hasattr(request.user, "studentvoter"):
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter

    # Get all categories, ordered by display_order
    categories = ElectionCategory.objects.all().order_by("display_order", "name")

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
        state__in=["CLOSED", "ARCHIVED"],
    ).order_by("-actual_closed_at")

    return render(request, "core/voter_dashboard.html", {
        "voter": voter,
        "tab_data": tab_data,
        "past_elections": past_elections,
    })

@login_required
def state_register(request):
    """Register for a state association."""
    if not hasattr(request.user, "studentvoter"):
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter

    # Check if already registered for any state
    if voter.state_memberships.exists():
        messages.info(request, "You are already registered for a state association. Use the change page to update.")
        return redirect("voter_dashboard")

    # Check if any state association election is live (locked)
    state_category = ElectionCategory.objects.filter(
        category_type="STATE_ASSOCIATION"
    ).first()

    if state_category:
        live_elections = Election.objects.filter(category=state_category, state="LIVE")
        if live_elections.exists():
            messages.error(request, "State association registration is locked because an election is currently live.")
            return redirect("voter_dashboard")

    if request.method == "POST":
        state = request.POST.get("state")
        if not state:
            messages.error(request, "Please select your state of origin.")
        else:
            StateAssociationMembership.objects.create(voter=voter, state=state)
            messages.success(request, f"Successfully registered for {dict(StateAssociationMembership.NIGERIAN_STATES).get(state, state)} State Association.")
            return redirect("voter_dashboard")

    return render(request, "core/state_register.html", {
        "voter": voter,
        "states": StateAssociationMembership.NIGERIAN_STATES,
    })


@login_required
def state_change(request):
    """Change state association membership (only if no live election)."""
    if not hasattr(request.user, "studentvoter"):
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter

    # Check if any state association election is live
    state_category = ElectionCategory.objects.filter(
        category_type="STATE_ASSOCIATION"
    ).first()

    if state_category:
        live_elections = Election.objects.filter(category=state_category, state="LIVE")
        if live_elections.exists():
            messages.error(request, "Cannot change state association while an election is live.")
            return redirect("voter_dashboard")

    if request.method == "POST":
        new_state = request.POST.get("state")
        if not new_state:
            messages.error(request, "Please select a state.")
        else:
            # Delete old membership, create new
            voter.state_memberships.all().delete()
            StateAssociationMembership.objects.create(voter=voter, state=new_state)
            messages.success(request, f"State association updated to {dict(StateAssociationMembership.NIGERIAN_STATES).get(new_state, new_state)}.")
            return redirect("voter_dashboard")

    current_membership = voter.state_memberships.first()
    return render(request, "core/state_change.html", {
        "voter": voter,
        "states": StateAssociationMembership.NIGERIAN_STATES,
        "current_state": current_membership.state if current_membership else None,
    })

@login_required
def ballot_view(request, election_id):
    """Display the ballot for a live election."""
    election = get_object_or_404(Election, id=election_id)

    if not hasattr(request.user, "studentvoter"):
        messages.error(request, "Access denied.")
        return redirect("home")

    voter = request.user.studentvoter

    if election.state != "LIVE":
        messages.error(request, "This election is not currently open for voting.")
        return redirect("voter_dashboard")

    # Check eligibility
    if not is_voter_eligible(voter, election):
        log_action(
            action_type="BALLOT_ACCESS_DENIED",
            description="Ineligible voter attempted to open a ballot.",
            actor=voter.matric_number,
            election=election,
            severity=SEVERITY_SECURITY,
        )
        messages.error(request, "You are not eligible to vote in this election.")
        return redirect("voter_dashboard")

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
        "election": election,
        "ballot_data": ballot_data,
        "voter": voter,
    })


@login_required
@require_POST
def cast_vote_view(request, election_id, position_id):
    """Handle vote casting."""
    election = get_object_or_404(Election, id=election_id)
    position = get_object_or_404(Position, id=position_id, election=election)

    if not hasattr(request.user, "studentvoter"):
        return JsonResponse({"success": False, "message": "Access denied."})

    voter = request.user.studentvoter

    if election.state != "LIVE":
        return JsonResponse({"success": False, "message": "Election is not live."})

    if not is_voter_eligible(voter, election):
        log_action(
            action_type="BALLOT_ACCESS_DENIED",
            description="Ineligible voter attempted to cast a ballot.",
            actor=voter.matric_number,
            election=election,
            severity=SEVERITY_SECURITY,
        )
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

    return redirect("ballot_view", election_id=election_id)


def is_voter_eligible(voter, election):
    """Check if a voter is eligible for an election based on category and rules."""
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
        membership = StateAssociationMembership.objects.filter(
            voter=voter, state=category.name
        ).exists()
        return membership

    elif category.category_type == "SUG":
        # All students eligible (when SUG is activated)
        return True

    # Fallback
    return False


@login_required
def public_election_results(request, election_id):
    """Display election results (only when CLOSED or ARCHIVED)."""
    election = get_object_or_404(Election, id=election_id)

    # Results are ONLY visible when CLOSED or ARCHIVED (Blueprint §8)
    if election.state not in ["CLOSED", "ARCHIVED"]:
        messages.error(request, "Results are not yet available for this election.")
        return redirect("home")

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
    total_eligible = StudentVoter.objects.filter(is_activated=True).count()

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
        "election": election,
        "results": results,
        "total_eligible": total_eligible,
        "total_voters": total_voters,
        "turnout_percentage": round(turnout_percentage, 2),
    })


# ============================================================================
# School Admin Dashboard
# ============================================================================
@login_required
@org_admin_required
def admin_dashboard(request):
    """School Admin main dashboard."""
    elections = Election.objects.all().order_by("-created_at")
    live_elections = elections.filter(state="LIVE")
    closed_elections = elections.filter(state__in=["CLOSED", "ARCHIVED"])
    total_voters = StudentVoter.objects.filter(is_activated=True).count()
    total_verified = VerifiedVoterRecord.objects.count()

    # Recent audit logs
    recent_logs = AuditLog.objects.all()[:20]

    return render(request, "core/admin_dashboard.html", {
        "elections": elections,
        "live_elections": live_elections,
        "closed_elections": closed_elections,
        "total_voters": total_voters,
        "total_verified": total_verified,
        "recent_logs": recent_logs,
    })


@login_required
@org_admin_required
def category_create(request):
    """Create a new election category (Faculty, State Association, SUG)."""
    if request.method == "POST":
        form = ElectionCategoryForm(request.POST)
        if form.is_valid():
            category = form.save(commit=False)
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
                action_type="CATEGORY_CREATED",
                description=f"Election category '{category.name}' created",
                actor=request.user.email,
            )

            messages.success(request, f"Category '{category.name}' created successfully.")
            return redirect("admin_dashboard")
    else:
        form = ElectionCategoryForm()

    return render(request, "core/admin_category_form.html", {"form": form})


@login_required
@org_admin_required
def election_create(request):
    """Create a new election."""
    if request.method == "POST":
        form = ElectionForm(request.POST)
        if form.is_valid():
            election = form.save()
            log_action(
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
        "action": "Create",
    })


@login_required
@org_admin_required
def election_detail(request, election_id):
    """View and manage a specific election."""
    election = get_object_or_404(Election, id=election_id)
    positions = election.positions.prefetch_related("candidates")

    return render(request, "core/admin_election_detail.html", {
        "election": election,
        "positions": positions,
    })


@login_required
@org_admin_required
def election_transition(request, election_id):
    """Transition election state (DRAFT→LIVE, LIVE→CLOSED)."""
    election = get_object_or_404(Election, id=election_id)

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
    election = get_object_or_404(Election, id=election_id)

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
        "election": election,
    })


@login_required
@org_admin_required
def candidate_create(request, position_id):
    """Add a candidate to a position."""
    position = get_object_or_404(Position, id=position_id)

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
        "position": position,
    })


@login_required
@org_admin_required
def candidate_screen(request, candidate_id):
    """Screen (approve/reject/withdraw) a candidate."""
    candidate = get_object_or_404(Candidate, id=candidate_id)

    if request.method == "POST":
        form = CandidateStatusForm(request.POST, instance=candidate)
        if form.is_valid():
            old_status = candidate.status
            candidate = form.save()

            log_action(
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
        "candidate": candidate,
    })

@login_required
@org_admin_required
def candidate_delete(request, candidate_id):
    """Delete a candidate. Only allowed if election is not LIVE."""
    candidate = get_object_or_404(Candidate, id=candidate_id)

    election = candidate.position.election

    if election.state == "LIVE":
        messages.error(request, "Cannot delete candidates while election is LIVE.")
        return redirect("admin_election_detail", election_id=election.id)

    if request.method == "POST":
        candidate_name = candidate.name
        candidate.delete()

        log_action(
            action_type="CANDIDATE_DELETED",
            description=f"Candidate '{candidate_name}' deleted by {request.user.email}",
            actor=request.user.email,
            election=election,
        )

        messages.success(request, f"Candidate '{candidate_name}' has been deleted.")
        return redirect("admin_election_detail", election_id=election.id)

    return render(request, "core/admin_candidate_delete.html", {
        "candidate": candidate,
        "election": election,
    })

@login_required
@org_admin_required
def voter_import(request):
    """Import verified voter records from CSV."""
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

    return render(request, "core/admin_voter_import.html", {"form": form})


@login_required
@org_admin_required
def admin_results(request, election_id):
    """View results for a closed election (same data as public, but admin access)."""
    election = get_object_or_404(Election, id=election_id)

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
        "election": election,
        "results": results,
    })

@login_required
@org_admin_required
def admin_election_results_export(request, election_id):
    """Export a single election's results as CSV."""
    election = get_object_or_404(Election, id=election_id)

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
        action_type="ELECTION_RESULTS_EXPORTED",
        description=f"Results exported for '{election.title}' by {request.user.email}",
        actor=request.user.email,
        election=election,
    )

    return response

@login_required
@org_admin_required
def admin_audit_log(request):
    """View and filter audit log."""
    logs = AuditLog.objects.select_related("election")

    # Get elections for the filter dropdown
    elections = Election.objects.all().order_by("-created_at")

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
    logs = AuditLog.objects.select_related("election")

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
    response['Content-Disposition'] = f'attachment; filename="audit_log_{timezone.now().date()}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'Timestamp', 'Severity', 'Action', 'Election', 'Actor',
        'Description', 'Previous Hash', 'Entry Hash',
    ])

    for log in logs:
        writer.writerow([
            log.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            log.severity,
            log.action_type,
            log.election.title if log.election else "",
            log.actor,
            log.description,
            log.prev_hash,
            log.entry_hash,
        ])

    # Log the export action
    log_action(
        action_type="AUDIT_LOG_EXPORTED",
        description=f"Audit log exported by {request.user.email}",
        actor=request.user.email,
    )

    return response


@login_required
@org_admin_required
def audit_log_verify(request):
    """Re-walk every audit chain and report any tampering."""
    report = verify_audit_chain()
    return render(request, "core/admin_audit_verify.html", {"report": report})


# ============================================================================
# Election Officer (Observer) Dashboard [Blueprint §10]
# ============================================================================
@login_required
@election_officer_required
def observer_dashboard(request):
    """Election Observer dashboard: live turnout, system status, audit feed."""
    officer = request.user.electionofficer

    # Get assigned election — NO fallback to other elections
    election = officer.assigned_election

    if not election:
        return render(request, "core/observer_dashboard.html", {
            "election": None,
            "no_election": False,
            "no_assignment": True,
        })

    # LIVE TURNOUT: X of Y eligible voters have cast a ballot
    total_eligible = StudentVoter.objects.filter(is_activated=True).count()

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
        election=election
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
