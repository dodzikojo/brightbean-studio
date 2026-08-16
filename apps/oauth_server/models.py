"""Durable, digest-only OAuth token bindings for MCP."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class McpOAuthTokenBinding(models.Model):
    class TokenKind(models.TextChoices):
        ACCESS = "access", "Access token"
        REFRESH = "refresh", "Refresh token"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_kind = models.CharField(max_length=10, choices=TokenKind.choices)
    token_digest = models.CharField(max_length=64, unique=True)
    access_token = models.OneToOneField(
        "oauth2_provider.AccessToken",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mcp_binding",
    )
    refresh_token = models.OneToOneField(
        "oauth2_provider.RefreshToken",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mcp_binding",
    )
    parent_refresh_binding = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rotated_bindings",
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mcp_token_bindings",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mcp_oauth_token_bindings",
    )
    resource_uri = models.TextField()
    resource_digest = models.CharField(max_length=64, db_index=True)
    granted_scopes = models.JSONField(default=list)
    token_family = models.UUIDField(null=True, blank=True, db_index=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["token_kind", "token_digest"], name="mcp_bind_kind_digest"),
            models.Index(fields=["application", "revoked_at"], name="mcp_bind_app_revoked"),
            models.Index(fields=["user", "revoked_at"], name="mcp_bind_user_revoked"),
        ]
