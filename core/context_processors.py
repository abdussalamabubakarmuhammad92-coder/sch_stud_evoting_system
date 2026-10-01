"""
Context processors for SUG E-Voting Platform.
"""
from django.conf import settings


def school_context(request):
    """School identity and role flags for all templates."""
    context = {
        "school_name": settings.SCHOOL_NAME,
        "is_admin": False,
        "is_election_officer": False,
        "is_voter": False,
    }

    if request.user.is_authenticated:
        context["is_admin"] = request.user.user_type == "ADMIN"
        context["is_election_officer"] = request.user.user_type == "ELECTION_OFFICER"
        context["is_voter"] = request.user.user_type == "VOTER"

    return context
