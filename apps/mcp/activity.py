"""Redacted MCP activity helpers."""

from __future__ import annotations

import logging
from datetime import timedelta
from numbers import Integral
from typing import Any
from uuid import UUID, uuid4

from django.utils import timezone

LOG = logging.getLogger(__name__)


def safe_activity_summary(values: dict | None) -> dict[str, Any]:
    """Copy only IDs, counts, changed-field names, and internal target paths."""
    if not isinstance(values, dict):
        return {}
    summary: dict[str, Any] = {}
    for key, value in values.items():
        if key.endswith("_id") and isinstance(value, (str, UUID, Integral)) and not isinstance(value, bool):
            summary[key] = str(value)
        elif (key == "count" or key.endswith("_count")) and isinstance(value, Integral) and not isinstance(value, bool):
            summary[key] = int(value)
        elif key == "changed_fields" and isinstance(value, (list, tuple)):
            summary[key] = [field for field in value[:100] if isinstance(field, str) and len(field) <= 128]
        elif key == "target_path" and isinstance(value, str) and value.startswith("/") and "?" not in value:
            summary[key] = value[:255]
    return summary


def _workspace_for_message(principal, arguments: dict):
    requested = arguments.get("workspace_id")
    if requested is not None:
        requested = str(requested)
        return next(
            (item.workspace for item in principal.authorized_workspaces if str(item.workspace.id) == requested),
            None,
        )
    if principal.workspace_pin_id is not None:
        return next(
            (
                item.workspace
                for item in principal.authorized_workspaces
                if item.workspace.id == principal.workspace_pin_id
            ),
            None,
        )
    if len(principal.authorized_workspaces) == 1:
        return principal.authorized_workspaces[0].workspace
    return None


def _activity_identity(message: dict) -> tuple[str, str, dict]:
    raw_method = message.get("method")
    method: str = raw_method if isinstance(raw_method, str) else "unknown"
    raw_params = message.get("params")
    params: dict = raw_params if isinstance(raw_params, dict) else {}
    if method == "tools/call":
        raw_name = params.get("name")
        name: str = raw_name if isinstance(raw_name, str) else "unknown"
        raw_arguments = params.get("arguments")
        arguments: dict = raw_arguments if isinstance(raw_arguments, dict) else {}
        return "tool", name, arguments
    if method == "resources/read":
        return "resource", "read", params
    if method == "prompts/get":
        raw_name = params.get("name")
        name = raw_name if isinstance(raw_name, str) else "unknown"
        raw_arguments = params.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
        return "prompt", name, arguments
    return "method", method, params


def record_activity(
    principal,
    message: dict,
    *,
    status_code: int,
    duration_ms: int,
    protocol_version: str = "",
    correlation_id: UUID | None = None,
):
    """Persist one safe activity row; failures never affect the MCP response."""
    from apps.mcp.models import McpActivityEvent

    try:
        primitive, name, arguments = _activity_identity(message)
        workspace = _workspace_for_message(principal, arguments)
        organizations = {
            item.workspace.organization_id: item.workspace.organization for item in principal.authorized_workspaces
        }
        organization = workspace.organization if workspace is not None else None
        if organization is None and len(organizations) == 1:
            organization = next(iter(organizations.values()))
        summary = safe_activity_summary(arguments)
        target_key = next((key for key in summary if key.endswith("_id") and key != "workspace_id"), "")
        if status_code == 429:
            status = McpActivityEvent.Status.RATE_LIMITED
        elif status_code in {401, 403}:
            status = McpActivityEvent.Status.DENIED
        elif 200 <= status_code < 300:
            status = McpActivityEvent.Status.SUCCEEDED
        else:
            status = McpActivityEvent.Status.FAILED
        confirmation_state = McpActivityEvent.ConfirmationState.NOT_REQUIRED
        if primitive == "tool":
            from apps.mcp.registry import get_tool

            tool = get_tool(name, include_disabled=True)
            if tool is not None and tool.confirmation_required:
                confirmation_state = (
                    McpActivityEvent.ConfirmationState.CONFIRMED
                    if arguments.get("confirmation_token")
                    else McpActivityEvent.ConfirmationState.PREVIEW
                )
        return McpActivityEvent.objects.create(
            organization=organization,
            workspace=workspace,
            actor=principal.user,
            api_key=principal.api_key,
            oauth_application=principal.oauth_client,
            credential_type=principal.credential_kind,
            primitive=primitive,
            name=name[:128],
            target_type=target_key.removesuffix("_id")[:64],
            target_id=str(summary.get(target_key, ""))[:128],
            target_path=str(summary.get("target_path", ""))[:255],
            status=status,
            duration_ms=max(0, int(duration_ms)),
            protocol_version=protocol_version[:32],
            correlation_id=correlation_id or uuid4(),
            confirmation_state=confirmation_state,
            summary=summary,
        )
    except Exception:  # noqa: BLE001 - activity is deliberately best-effort.
        LOG.warning("Failed to record redacted MCP activity", exc_info=True)
        return None


def purge_expired_activity(*, now=None) -> int:
    """Delete activity using each organization's audit-retention setting."""
    from apps.mcp.models import McpActivityEvent
    from apps.settings_manager.defaults import APP_DEFAULTS
    from apps.settings_manager.models import OrgSetting

    now = now or timezone.now()
    default_value = APP_DEFAULTS["org.audit_log_retention_days"]
    default_days = default_value if isinstance(default_value, int) and not isinstance(default_value, bool) else 365
    overrides = {
        organization_id: int(value)
        for organization_id, value in OrgSetting.objects.filter(key="org.audit_log_retention_days").values_list(
            "organization_id", "value"
        )
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3650
    }
    deleted_total = 0
    organization_ids = (
        McpActivityEvent.objects.filter(organization__isnull=False).values_list("organization_id", flat=True).distinct()
    )
    for organization_id in organization_ids:
        retention_days = int(overrides.get(organization_id, default_days))
        deleted, _ = McpActivityEvent.objects.filter(
            organization_id=organization_id,
            created_at__lt=now - timedelta(days=retention_days),
        ).delete()
        deleted_total += deleted
    deleted, _ = McpActivityEvent.objects.filter(
        organization__isnull=True,
        created_at__lt=now - timedelta(days=default_days),
    ).delete()
    return deleted_total + deleted
