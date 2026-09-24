"""
Context processors for SUG E-Voting Platform.
"""
from .models import Organization


def organization_context(request):
    """
    Add organization context to all templates based on subdomain or session.
    For now, uses a session variable or defaults to the first active organization.
    In production, you'd use subdomain routing: fud.sugevoting.ng
    """
    context = {
        "current_organization": None,
        "is_org_admin": False,
        "is_election_officer": False,
        "is_platform_owner": False,
    }

    if request.user.is_authenticated:
        context["is_platform_owner"] = request.user.is_platform_owner

        # Check if user is an org admin
        if hasattr(request.user, "organizationadmin"):
            context["is_org_admin"] = True
            context["current_organization"] = request.user.organizationadmin.organization

        # Check if user is an election officer
        elif hasattr(request.user, "electionofficer"):
            context["is_election_officer"] = True
            context["current_organization"] = request.user.electionofficer.organization

        # Check if user is a voter
        elif hasattr(request.user, "studentvoter"):
            context["current_organization"] = request.user.studentvoter.organization

    # Also check session for organization slug (for non-logged-in visitors)
    org_slug = request.session.get("organization_slug")
    if org_slug and not context["current_organization"]:
        try:
            context["current_organization"] = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            pass

    return context
