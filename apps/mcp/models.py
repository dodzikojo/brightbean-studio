"""Persistence for MCP policy, activity, confirmation, and replay safety."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class McpOrganizationConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="mcp_config",
    )
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mcp_organization_config"


class McpToolPolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="mcp_tool_policies",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="mcp_tool_policies",
        null=True,
        blank=True,
    )
    tool_name = models.CharField(max_length=128)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mcp_tool_policy"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "tool_name"],
                condition=Q(workspace__isnull=True),
                name="mcp_unique_org_tool_policy",
            ),
            models.UniqueConstraint(
                fields=["workspace", "tool_name"],
                condition=Q(workspace__isnull=False),
                name="mcp_unique_workspace_tool_policy",
            ),
        ]
        indexes = [models.Index(fields=["tool_name", "enabled"], name="mcp_policy_tool_enabled")]

    def clean(self):
        super().clean()
        if self.workspace_id and self.organization_id and self.workspace.organization_id != self.organization_id:
            raise ValidationError({"workspace": "Workspace must belong to the same organization as the policy."})


class McpActivityEvent(models.Model):
    class Primitive(models.TextChoices):
        METHOD = "method", "Method"
        TOOL = "tool", "Tool"
        RESOURCE = "resource", "Resource"
        PROMPT = "prompt", "Prompt"

    class CredentialType(models.TextChoices):
        API_KEY = "api_key", "API key"
        OAUTH = "oauth", "OAuth"

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        DENIED = "denied", "Denied"
        RATE_LIMITED = "rate_limited", "Rate limited"

    class ConfirmationState(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required"
        PREVIEW = "preview", "Preview"
        CONFIRMED = "confirmed", "Confirmed"
        REPLAYED = "replayed", "Replayed"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="mcp_activity_events",
        null=True,
        blank=True,
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="mcp_activity_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="mcp_activity_events",
        null=True,
        blank=True,
    )
    api_key = models.ForeignKey(
        "api_keys.ApiKey",
        on_delete=models.SET_NULL,
        related_name="mcp_activity_events",
        null=True,
        blank=True,
    )
    oauth_application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.SET_NULL,
        related_name="mcp_activity_events",
        null=True,
        blank=True,
    )
    credential_type = models.CharField(max_length=16, choices=CredentialType.choices)
    primitive = models.CharField(max_length=16, choices=Primitive.choices)
    name = models.CharField(max_length=128)
    target_type = models.CharField(max_length=64, blank=True, default="")
    target_id = models.CharField(max_length=128, blank=True, default="")
    target_path = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices)
    duration_ms = models.PositiveIntegerField(default=0)
    protocol_version = models.CharField(max_length=32, blank=True, default="")
    correlation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    confirmation_state = models.CharField(
        max_length=16,
        choices=ConfirmationState.choices,
        default=ConfirmationState.NOT_REQUIRED,
    )
    summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "mcp_activity_event"
        indexes = [
            models.Index(fields=["organization", "created_at"], name="mcp_activity_org_time"),
            models.Index(fields=["workspace", "created_at"], name="mcp_activity_ws_time"),
            models.Index(fields=["primitive", "name"], name="mcp_activity_primitive"),
        ]


class McpConfirmationGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    credential_type = models.CharField(max_length=16, choices=McpActivityEvent.CredentialType.choices)
    api_key = models.ForeignKey("api_keys.ApiKey", on_delete=models.CASCADE, null=True, blank=True)
    oauth_application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    tool_name = models.CharField(max_length=128)
    token_digest = models.CharField(max_length=64, unique=True)
    payload_hash = models.CharField(max_length=64)
    safe_preview = models.JSONField(default=dict)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mcp_confirmation_grant"
        indexes = [models.Index(fields=["workspace", "tool_name", "expires_at"], name="mcp_grant_ws_tool_exp")]


class McpIdempotencyRecord(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    credential_type = models.CharField(max_length=16, choices=McpActivityEvent.CredentialType.choices)
    credential_digest = models.CharField(max_length=64)
    tool_name = models.CharField(max_length=128)
    idempotency_key_digest = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    response_summary = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mcp_idempotency_record"
        constraints = [
            models.UniqueConstraint(
                fields=["credential_digest", "tool_name", "idempotency_key_digest"],
                name="mcp_unique_idempotency_key",
            )
        ]
        indexes = [models.Index(fields=["created_at"], name="mcp_idem_created_at")]
