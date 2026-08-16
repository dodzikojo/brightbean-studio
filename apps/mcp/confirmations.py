"""Two-step confirmation and idempotent execution for consequential MCP tools."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.mcp.errors import DomainError
from apps.mcp.models import McpConfirmationGrant, McpIdempotencyRecord
from apps.mcp.principal import McpPrincipal
from apps.mcp.results import success_result

CONFIRMATION_TTL = timedelta(minutes=10)
_CONTROL_ARGUMENTS = frozenset({"confirmation_token", "idempotency_key"})
_SAFE_EXACT_KEYS = frozenset(
    {
        "changed_fields",
        "count",
        "expires_at",
        "id",
        "scheduled_at",
        "state",
        "status",
        "target_path",
    }
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    """Convert supported values to deterministic, JSON-safe primitives."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def canonical_payload_hash(*, tool_name: str, workspace_id: Any, arguments: Mapping[str, Any]) -> str:
    payload = {
        "tool": tool_name,
        "workspace_id": str(workspace_id),
        "arguments": {key: _json_value(value) for key, value in arguments.items() if key not in _CONTROL_ARGUMENTS},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return _digest(encoded)


def credential_digest(principal: McpPrincipal) -> str:
    if principal.api_key is not None:
        identity = f"api_key:{principal.api_key.pk}"
    else:
        client_id = principal.oauth_client.pk if principal.oauth_client is not None else "none"
        identity = f"oauth:{principal.user.pk}:{client_id}"
    return _digest(identity)


def safe_confirmation_data(value: Any) -> Any:
    """Retain identifiers and operational metadata, never user-authored content."""
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _SAFE_EXACT_KEYS or key.endswith("_id") or key.endswith("_count"):
                safe[key] = _json_value(item)
        return safe
    return {}


def _grant_matches(
    grant: McpConfirmationGrant,
    *,
    principal: McpPrincipal,
    workspace: Any,
    tool_name: str,
) -> bool:
    return bool(
        grant.organization_id == workspace.organization_id
        and grant.workspace_id == workspace.pk
        and grant.actor_id == principal.user.pk
        and grant.credential_type == principal.credential_kind
        and grant.api_key_id == (principal.api_key.pk if principal.api_key is not None else None)
        and grant.oauth_application_id == (principal.oauth_client.pk if principal.oauth_client is not None else None)
        and grant.tool_name == tool_name
    )


def _replay(record: McpIdempotencyRecord, *, payload_hash: str) -> dict[str, Any]:
    if record.payload_hash != payload_hash:
        raise DomainError(
            "idempotency_conflict",
            "This idempotency key was already used for different arguments.",
        )
    if record.status == McpIdempotencyRecord.Status.SUCCEEDED:
        return {**(record.response_summary or {}), "replayed": True}
    raise DomainError(
        "operation_in_progress",
        "An operation with this idempotency key is already in progress.",
        retryable=True,
    )


def confirmed_action(
    *,
    principal: McpPrincipal,
    workspace: Any,
    tool_name: str,
    arguments: Mapping[str, Any],
    preview: Mapping[str, Any],
    execute: Callable[[], Any],
) -> dict[str, Any]:
    """Preview a consequential action, or execute it exactly once after confirmation."""
    payload_hash = canonical_payload_hash(
        tool_name=tool_name,
        workspace_id=workspace.pk,
        arguments=arguments,
    )
    token = arguments.get("confirmation_token")
    if not isinstance(token, str) or not token:
        raw_token = secrets.token_urlsafe(32)
        expires_at = timezone.now() + CONFIRMATION_TTL
        safe_preview = safe_confirmation_data(preview)
        McpConfirmationGrant.objects.create(
            organization_id=workspace.organization_id,
            workspace=workspace,
            actor=principal.user,
            credential_type=principal.credential_kind,
            api_key=principal.api_key,
            oauth_application=principal.oauth_client,
            tool_name=tool_name,
            token_digest=_digest(raw_token),
            payload_hash=payload_hash,
            safe_preview=safe_preview,
            expires_at=expires_at,
        )
        return {
            "confirmation_required": True,
            "confirmation_token": raw_token,
            "payload_hash": payload_hash,
            "expires_at": expires_at.isoformat(),
            "preview": safe_preview,
        }

    idempotency_key = arguments.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise DomainError(
            "idempotency_key_required",
            "An idempotency_key between 1 and 128 characters is required when confirming an action.",
        )

    stable_credential = credential_digest(principal)
    key_digest = _digest(idempotency_key)
    with transaction.atomic():
        try:
            grant = McpConfirmationGrant.objects.select_for_update().get(token_digest=_digest(token))
        except McpConfirmationGrant.DoesNotExist as exc:
            raise DomainError("invalid_confirmation", "The confirmation token is invalid.") from exc

        if not _grant_matches(grant, principal=principal, workspace=workspace, tool_name=tool_name):
            raise DomainError("invalid_confirmation", "The confirmation token is invalid.")
        if grant.payload_hash != payload_hash:
            raise DomainError(
                "confirmation_payload_mismatch",
                "The confirmed arguments do not match the preview.",
            )
        if grant.expires_at <= timezone.now():
            raise DomainError("confirmation_expired", "The confirmation token has expired.")

        existing = McpIdempotencyRecord.objects.filter(
            credential_digest=stable_credential,
            tool_name=tool_name,
            idempotency_key_digest=key_digest,
        ).first()
        if existing is not None:
            return _replay(existing, payload_hash=payload_hash)
        if grant.consumed_at is not None:
            raise DomainError("confirmation_used", "The confirmation token has already been used.")

        try:
            with transaction.atomic():
                record = McpIdempotencyRecord.objects.create(
                    organization_id=workspace.organization_id,
                    workspace=workspace,
                    actor=principal.user,
                    credential_type=principal.credential_kind,
                    credential_digest=stable_credential,
                    tool_name=tool_name,
                    idempotency_key_digest=key_digest,
                    payload_hash=payload_hash,
                )
        except IntegrityError:
            concurrent = McpIdempotencyRecord.objects.get(
                credential_digest=stable_credential,
                tool_name=tool_name,
                idempotency_key_digest=key_digest,
            )
            return _replay(concurrent, payload_hash=payload_hash)
        grant.consumed_at = timezone.now()
        grant.save(update_fields=["consumed_at"])

        response = safe_confirmation_data(execute())
        record.status = McpIdempotencyRecord.Status.SUCCEEDED
        record.response_summary = response
        record.save(update_fields=["status", "response_summary", "updated_at"])
        return {**response, "replayed": False}


def invoke_tool_with_confirmation(tool: Any, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Apply the shared two-step contract before invoking a registered handler."""
    if not tool.confirmation_required:
        return tool.handler(arguments, context)
    workspace = context.get("workspace")
    principal = context.get("principal")
    if workspace is None or principal is None:
        raise DomainError("workspace_required", "A workspace is required for this action.")

    def execute() -> Any:
        handler_arguments = {key: value for key, value in arguments.items() if key not in _CONTROL_ARGUMENTS}
        result = tool.handler(handler_arguments, context)
        if isinstance(result, Mapping):
            return result.get("structuredContent", result)
        return result

    outcome = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name=tool.name,
        arguments=arguments,
        preview=arguments,
        execute=execute,
    )
    request = context.get("request")
    if request is not None:
        if outcome.get("confirmation_required"):
            request._mcp_confirmation_state = "preview"
        elif outcome.get("replayed"):
            request._mcp_confirmation_state = "replayed"
        else:
            request._mcp_confirmation_state = "confirmed"
    return success_result(outcome)
