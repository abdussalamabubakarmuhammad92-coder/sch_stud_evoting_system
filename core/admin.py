"""
Django Admin configuration for SUG E-Voting Platform.
Multi-tenant aware: admins only see their Organization's data.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

# Import models with aliases so admin class names NEVER shadow them
from .models import (
    User,
    Organization,
    OrganizationAdmin,
    ElectionOfficer,
    ElectionCategory,
    Election,
    Position,
    Candidate,
    VerifiedVoterRecord,
    StudentVoter,
    Vote,
    OTPVerification,
    RecognizedDevice,
    AuditLog,
    PostElectionReport,
    AdminInvitationCode,
)

# ============================================================================
# Custom User Admin
# ============================================================================
@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["email", "username", "user_type", "is_platform_owner", "is_active"]
    list_filter = ["user_type", "is_platform_owner", "is_active"]
    search_fields = ["email", "username", "phone_number"]
    ordering = ["email"]
    
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Platform", {"fields": ("phone_number", "user_type", "is_platform_owner")}),
    )


# ============================================================================
# Organization Admin Panel
# ============================================================================
@admin.register(Organization)
class OrganizationModelAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "subscription_status", "subscription_expires_at", "created_at"]
    list_filter = ["subscription_status", "created_at"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    date_hierarchy = "created_at"


# ============================================================================
# Org Admin Model Admin
# ============================================================================
@admin.register(OrganizationAdmin)
class OrganizationAdminAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "is_primary", "created_at"]
    list_filter = ["organization", "is_primary"]
    search_fields = ["user__email", "organization__name"]

    def has_delete_permission(self, request, obj=None):
        return request.user.is_platform_owner

    def has_change_permission(self, request, obj=None):
        return request.user.is_platform_owner

    def has_add_permission(self, request):
        return request.user.is_platform_owner


# ============================================================================
# Election Officer Model Admin
# ============================================================================
@admin.register(ElectionOfficer)
class ElectionOfficerModelAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "assigned_election", "created_at"]
    list_filter = ["organization", "assigned_election"]
    search_fields = ["user__email", "organization__name"]


# ============================================================================
# Election Admin (with multi-tenant filtering)
# ============================================================================

@admin.register(ElectionCategory)
class ElectionCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "category_type", "organization", "is_active", "display_order"]
    list_filter = ["category_type", "organization", "is_active"]
    list_editable = ["display_order", "is_active"]
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_platform_owner:
            return qs
        try:
            org_admin = OrganizationAdmin.objects.get(user=request.user)
            return qs.filter(organization=org_admin.organization)
        except OrganizationAdmin.DoesNotExist:
            return qs.none()

@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = ["title", "organization", "state", "voting_opens_at", "voting_closes_at", "created_at"]
    list_filter = ["state", "organization", "created_at"]
    search_fields = ["title"]
    date_hierarchy = "created_at"
    readonly_fields = ["final_tally_hash", "results_published_at", "actual_closed_at", "total_extension_seconds"]
    
    fieldsets = (
        (None, {"fields": ("organization", "title", "state")}),
        ("Voting Window", {"fields": ("voting_opens_at", "voting_closes_at", "actual_closed_at")}),
        ("Eligibility", {"fields": ("eligibility_type", "eligibility_filter_value")}),
        ("Collation", {"fields": ("collation_grouping_enabled", "collation_grouping_field")}),
        ("Results Integrity", {"fields": ("final_tally_hash", "results_published_at"), "classes": ("collapse",)}),
        ("Downtime", {"fields": ("total_extension_seconds",), "classes": ("collapse",)}),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_platform_owner:
            return qs
        # Filter to user's organization
        try:
            org_admin = OrganizationAdmin.objects.get(user=request.user)
            return qs.filter(organization=org_admin.organization)
        except OrganizationAdmin.DoesNotExist:
            return qs.none()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "category" and not request.user.is_platform_owner:
            try:
                org_admin = OrganizationAdmin.objects.get(user=request.user)
                kwargs["queryset"] = ElectionCategory.objects.filter(organization=org_admin.organization)
            except OrganizationAdmin.DoesNotExist:
                kwargs["queryset"] = ElectionCategory.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


# ============================================================================
# Position Admin
# ============================================================================
@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ["name", "election", "display_order"]
    list_filter = ["election__organization"]
    search_fields = ["name", "election__title"]
    ordering = ["election", "display_order"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_platform_owner:
            return qs
        try:
            org_admin = OrganizationAdmin.objects.get(user=request.user)
            return qs.filter(election__organization=org_admin.organization)
        except OrganizationAdmin.DoesNotExist:
            return qs.none()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "election" and not request.user.is_platform_owner:
            try:
                org_admin = OrganizationAdmin.objects.get(user=request.user)
                kwargs["queryset"] = Election.objects.filter(organization=org_admin.organization)
            except OrganizationAdmin.DoesNotExist:
                kwargs["queryset"] = Election.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

# ============================================================================
# Candidate Admin
# ============================================================================
@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ["name", "position", "status", "created_at"]
    list_filter = ["status", "position__election__organization"]
    search_fields = ["name", "position__name"]
    readonly_fields = ["withdrawn_at"]
    
    fieldsets = (
        (None, {"fields": ("position", "name", "manifesto", "photo")}),
        ("Screening", {"fields": ("status", "screening_notes", "withdrawn_at")}),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_platform_owner:
            return qs
        try:
            org_admin = OrganizationAdmin.objects.get(user=request.user)
            return qs.filter(position__election__organization=org_admin.organization)
        except OrganizationAdmin.DoesNotExist:
            return qs.none()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "position" and not request.user.is_platform_owner:
            try:
                org_admin = OrganizationAdmin.objects.get(user=request.user)
                kwargs["queryset"] = Position.objects.filter(election__organization=org_admin.organization)
            except OrganizationAdmin.DoesNotExist:
                kwargs["queryset"] = Position.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


# ============================================================================
# VerifiedVoterRecord Admin
# ============================================================================
@admin.register(VerifiedVoterRecord)
class VerifiedVoterRecordAdmin(admin.ModelAdmin):
    list_display = ["matric_number", "organization", "official_email", "official_phone", "level", "department"]
    list_filter = ["organization", "level", "imported_at"]
    search_fields = ["matric_number", "official_email", "official_phone", "department", "faculty"]
    readonly_fields = ["imported_at"]


# ============================================================================
# StudentVoter Admin
# ============================================================================
@admin.register(StudentVoter)
class StudentVoterAdmin(admin.ModelAdmin):
    list_display = ["matric_number", "organization", "email", "phone_number", "is_activated", "created_at"]
    list_filter = ["is_activated", "organization", "created_at"]
    search_fields = ["matric_number", "email", "phone_number"]
    filter_horizontal = ["voted_positions"]
    readonly_fields = ["created_at", "updated_at"]
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_platform_owner:
            return qs
        try:
            org_admin = OrganizationAdmin.objects.get(user=request.user)
            return qs.filter(organization=org_admin.organization)
        except OrganizationAdmin.DoesNotExist:
            return qs.none()


# ============================================================================
# Vote Admin (read-only, for audit purposes only)
# ============================================================================
@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display = ["position", "candidate", "created_at"]
    list_filter = ["position__election__organization", "created_at"]
    search_fields = ["candidate__name", "position__name"]
    date_hierarchy = "created_at"
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================================
# OTPVerification Admin
# ============================================================================
@admin.register(OTPVerification)
class OTPVerificationAdmin(admin.ModelAdmin):
    list_display = ["voter", "purpose", "delivery_method", "used", "expires_at", "created_at"]
    list_filter = ["purpose", "delivery_method", "used", "created_at"]
    search_fields = ["voter__matric_number"]
    readonly_fields = ["code_hash", "created_at"]
    
    def has_add_permission(self, request):
        return False


# ============================================================================
# RecognizedDevice Admin
# ============================================================================
@admin.register(RecognizedDevice)
class RecognizedDeviceAdmin(admin.ModelAdmin):
    list_display = ["voter", "device_fingerprint", "first_seen_at", "last_seen_at"]
    search_fields = ["voter__matric_number"]


# ============================================================================
# AuditLog Admin (read-only)
# ============================================================================
@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["action_type", "organization", "election", "actor", "timestamp"]
    list_filter = ["action_type", "organization", "timestamp"]
    search_fields = ["actor", "description"]
    date_hierarchy = "timestamp"
    readonly_fields = ["organization", "election", "action_type", "description", "actor", "metadata", "timestamp"]
    actions = ["clear_all_logs"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is None:
            # Returning True for bulk delete allows the custom action to work
            return request.user.is_platform_owner
        return False

    @admin.action(description="⚠️ Clear ALL audit logs (Platform Owner only)")
    def clear_all_logs(self, request, queryset):
        if not request.user.is_platform_owner:
            return None
        count = queryset.count()
        queryset.delete()
        self.message_user(request, f"Deleted {count} audit log entries.")
        return None

@admin.register(AdminInvitationCode)
class AdminInvitationCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "organization", "invited_name", "invited_email", "status", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["code", "invited_name", "invited_email"]
    list_editable = []  # Required to show "Add" button

    def status(self, obj):
        if obj.is_used():
            return "✅ USED"
        return "🟢 AVAILABLE"
    status.short_description = "Status"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_platform_owner:
            return qs
        return qs.none()

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.is_used():
            return ["code", "organization", "invited_name", "invited_email", "used_by", "used_at"]
        return []

    def has_delete_permission(self, request, obj=None):
        return request.user.is_platform_owner

    def has_change_permission(self, request, obj=None):
        if obj and obj.is_used():
            return False  # Cannot edit used codes
        return request.user.is_platform_owner

# ============================================================================
# PostElectionReport Admin
# ============================================================================
@admin.register(PostElectionReport)
class PostElectionReportAdmin(admin.ModelAdmin):
    list_display = ["election", "is_public", "generated_at"]
    list_filter = ["is_public", "generated_at"]
    readonly_fields = ["generated_at"]