from django.contrib import admin

from .models import (
    McpActivityEvent,
    McpConfirmationGrant,
    McpIdempotencyRecord,
    McpOrganizationConfig,
    McpToolPolicy,
)


@admin.register(McpOrganizationConfig)
class McpOrganizationConfigAdmin(admin.ModelAdmin):
    list_display = ("organization", "enabled", "updated_at")
    list_filter = ("enabled",)


@admin.register(McpToolPolicy)
class McpToolPolicyAdmin(admin.ModelAdmin):
    list_display = ("tool_name", "organization", "workspace", "enabled", "updated_at")
    list_filter = ("enabled", "tool_name")
    search_fields = ("tool_name", "organization__name", "workspace__name")


@admin.register(McpActivityEvent)
class McpActivityEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "primitive", "name", "actor", "workspace", "status", "duration_ms")
    list_filter = ("primitive", "status", "credential_type", "confirmation_state")
    search_fields = ("name", "actor__email", "target_id", "correlation_id")
    readonly_fields = tuple(field.name for field in McpActivityEvent._meta.fields)


@admin.register(McpConfirmationGrant)
class McpConfirmationGrantAdmin(admin.ModelAdmin):
    list_display = ("tool_name", "actor", "workspace", "expires_at", "consumed_at")
    readonly_fields = tuple(field.name for field in McpConfirmationGrant._meta.fields)


@admin.register(McpIdempotencyRecord)
class McpIdempotencyRecordAdmin(admin.ModelAdmin):
    list_display = ("tool_name", "actor", "workspace", "status", "created_at", "updated_at")
    readonly_fields = tuple(field.name for field in McpIdempotencyRecord._meta.fields)
