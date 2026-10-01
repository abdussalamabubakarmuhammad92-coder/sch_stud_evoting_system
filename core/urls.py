from django.urls import path
from . import views

urlpatterns = [
    # Public
    path("", views.home, name="home"),

    # Auth
    path("login/", views.voter_login, name="voter_login"),
    path("admin/login/", views.admin_login, name="admin_login"),
    path("logout/", views.logout_view, name="logout"),

    # Voter Registration
    path("register/", views.voter_register_step1, name="voter_register_step1"),
    path("register/verify/", views.voter_register_step2, name="voter_register_step2"),
    path("register/password/", views.voter_register_step3, name="voter_register_step3"),

    # New Device OTP
    path("verify-device/", views.new_device_otp, name="new_device_otp"),

    # Password Reset
    path("password-reset/", views.password_reset_request, name="password_reset_request"),
    path("password-reset/verify/", views.password_reset_verify, name="password_reset_verify"),
    path("password-reset/new/", views.password_reset_new, name="password_reset_new"),

    # Voter Dashboard & Voting
    path("dashboard/", views.voter_dashboard, name="voter_dashboard"),
    path("state/register/", views.state_register, name="state_register"),
    path("state/change/", views.state_change, name="state_change"),
    path("election/<int:election_id>/ballot/", views.ballot_view, name="ballot_view"),
    path("election/<int:election_id>/position/<int:position_id>/vote/", views.cast_vote_view, name="cast_vote_view"),
    path("election/<int:election_id>/results/", views.public_election_results, name="election_results"),

    # School Admin
    path("admin/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin/category/create/", views.category_create, name="category_create"),
    path("admin/election/create/", views.election_create, name="election_create"),
    path("admin/election/<int:election_id>/", views.election_detail, name="admin_election_detail"),
    path("admin/election/<int:election_id>/transition/", views.election_transition, name="election_transition"),
    path("admin/election/<int:election_id>/position/create/", views.position_create, name="position_create"),
    path("admin/position/<int:position_id>/candidate/create/", views.candidate_create, name="candidate_create"),
    path("admin/candidate/<int:candidate_id>/screen/", views.candidate_screen, name="candidate_screen"),
    path("admin/candidate/<int:candidate_id>/delete/", views.candidate_delete, name="candidate_delete"),
    path("admin/voters/import/", views.voter_import, name="voter_import"),
    path("admin/election/<int:election_id>/results/", views.admin_results, name="admin_results"),
    path("admin/election/<int:election_id>/results/export/", views.admin_election_results_export, name="admin_election_results_export"),
    path("admin/audit-log/", views.admin_audit_log, name="admin_audit_log"),
    path("admin/audit-log/export/", views.admin_audit_log_export, name="admin_audit_log_export"),

    # Election Officer (Observer)
    path("observer/dashboard/", views.observer_dashboard, name="observer_dashboard"),
]
