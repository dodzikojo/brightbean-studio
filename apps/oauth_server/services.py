"""Creation and fail-closed verification of MCP OAuth token bindings."""

from __future__ import annotations

import hashlib
import uuid

from django.db import transaction
from django.utils import timezone

from .models import McpOAuthTokenBinding
from .resources import canonical_mcp_resource_uri
from .scopes import normalize_scopes


def token_digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _binding_defaults(token, *, resource: str, token_family=None, parent=None, scopes: str | None = None) -> dict:
    return {
        "application": token.application,
        "user": token.user,
        "resource_uri": resource,
        "resource_digest": token_digest(resource),
        "granted_scopes": list(normalize_scopes(scopes if scopes is not None else token.scope or "")),
        "token_family": token_family,
        "parent_refresh_binding": parent,
        "revoked_at": None,
    }


@transaction.atomic
def bind_access_token(access_token, raw_token: str) -> McpOAuthTokenBinding:
    resource = canonical_mcp_resource_uri()
    if access_token.resource != [resource] or access_token.user_id is None or access_token.application_id is None:
        raise ValueError("OAuth access token is not canonically bound for MCP.")
    binding, _ = McpOAuthTokenBinding.objects.update_or_create(
        token_digest=token_digest(raw_token),
        defaults={
            "token_kind": McpOAuthTokenBinding.TokenKind.ACCESS,
            "access_token": access_token,
            "refresh_token": None,
            **_binding_defaults(access_token, resource=resource),
        },
    )
    return binding


@transaction.atomic
def bind_refresh_token(refresh_token, raw_token: str, *, parent=None) -> McpOAuthTokenBinding:
    resource = canonical_mcp_resource_uri()
    if refresh_token.resource != [resource] or refresh_token.user_id is None or refresh_token.application_id is None:
        raise ValueError("OAuth refresh token is not canonically bound for MCP.")
    family = refresh_token.token_family or (parent.token_family if parent else None) or uuid.uuid4()
    binding, _ = McpOAuthTokenBinding.objects.update_or_create(
        token_digest=token_digest(raw_token),
        defaults={
            "token_kind": McpOAuthTokenBinding.TokenKind.REFRESH,
            "access_token": None,
            "refresh_token": refresh_token,
            **_binding_defaults(
                refresh_token,
                resource=resource,
                token_family=family,
                parent=parent,
                scopes=refresh_token.access_token.scope if refresh_token.access_token else "",
            ),
        },
    )
    return binding


def verify_access_binding(raw_token: str, access_token=None) -> McpOAuthTokenBinding | None:
    digest = token_digest(raw_token)
    try:
        binding = McpOAuthTokenBinding.objects.select_related(
            "access_token__user", "access_token__application", "user", "application"
        ).get(token_kind=McpOAuthTokenBinding.TokenKind.ACCESS, token_digest=digest)
    except McpOAuthTokenBinding.DoesNotExist:
        return None
    native = binding.access_token
    resource = canonical_mcp_resource_uri()
    expected_scopes = list(normalize_scopes(native.scope or "")) if native is not None else None
    if (
        binding.revoked_at is not None
        or native is None
        or (access_token is not None and native.pk != access_token.pk)
        or native.token_checksum != digest
        or native.is_expired()
        or native.user_id is None
        or native.application_id is None
        or not native.user.is_active
        or binding.user_id != native.user_id
        or binding.application_id != native.application_id
        or native.resource != [resource]
        or binding.resource_uri != resource
        or binding.resource_digest != token_digest(resource)
        or binding.granted_scopes != expected_scopes
    ):
        return None
    return binding


def verify_refresh_binding(raw_token: str, refresh_token=None) -> McpOAuthTokenBinding | None:
    digest = token_digest(raw_token)
    try:
        binding = McpOAuthTokenBinding.objects.select_related(
            "refresh_token__user",
            "refresh_token__application",
            "user",
            "application",
        ).get(
            token_kind=McpOAuthTokenBinding.TokenKind.REFRESH,
            token_digest=digest,
        )
    except McpOAuthTokenBinding.DoesNotExist:
        return None
    native = binding.refresh_token
    resource = canonical_mcp_resource_uri()
    native_scopes = native.access_token.scope if native is not None and native.access_token else ""
    if (
        binding.revoked_at is not None
        or native is None
        or (refresh_token is not None and native.pk != refresh_token.pk)
        or native.token_checksum != digest
        or native.revoked is not None
        or native.user_id is None
        or native.application_id is None
        or not native.user.is_active
        or binding.user_id != native.user_id
        or binding.application_id != native.application_id
        or native.resource != [resource]
        or binding.resource_uri != resource
        or binding.resource_digest != token_digest(resource)
        or binding.granted_scopes != list(normalize_scopes(native_scopes))
    ):
        return None
    return binding


def revoke_binding(raw_token: str) -> None:
    McpOAuthTokenBinding.objects.filter(token_digest=token_digest(raw_token), revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
