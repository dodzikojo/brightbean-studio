from __future__ import annotations

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.mcp.principal import McpPrincipal, PrincipalWorkspace
from apps.mcp.registry import Tool, ToolAnnotations


def _organization(name="Policy org"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _workspace(organization, name="Policy workspace"):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(organization=organization, name=name)


def _principal(user, workspace, membership, *, scopes=("mcp.read",), credential_kind="oauth", account_ids=()):
    effective = frozenset(key for key, allowed in membership.effective_permissions.items() if allowed)
    return McpPrincipal(
        credential_kind=credential_kind,
        user=user,
        api_key=SimpleNamespace(id="key-1") if credential_kind == "api_key" else None,
        oauth_access_token=None,
        oauth_client=None,
        granted_scopes=frozenset(scopes),
        workspace_pin_id=workspace.id if credential_kind == "api_key" else None,
        api_key_permissions=effective if credential_kind == "api_key" else frozenset(),
        account_allowlist_ids=frozenset(account_ids),
        authorized_workspaces=(PrincipalWorkspace(membership, workspace, effective),),
    )


def _tool(*, enabled=True):
    return Tool(
        name="create_idea",
        description="Create an idea.",
        input_schema={"type": "object"},
        handler=lambda arguments, context: {},
        enabled=enabled,
        required_scope="mcp.read",
        required_permissions=("create_posts",),
        workspace_scoped=True,
        annotations=ToolAnnotations(read_only=False),
    )


@pytest.mark.django_db
def test_tool_policy_constraints_and_workspace_organization_validation():
    from apps.mcp.models import McpToolPolicy

    organization = _organization()
    workspace = _workspace(organization)
    other_organization = _organization("Other org")
    other_workspace = _workspace(other_organization, "Other workspace")

    McpToolPolicy.objects.create(organization=organization, tool_name="create_idea", enabled=False)
    with pytest.raises(IntegrityError), transaction.atomic():
        McpToolPolicy.objects.create(organization=organization, tool_name="create_idea", enabled=True)

    McpToolPolicy.objects.create(
        organization=organization,
        workspace=workspace,
        tool_name="create_idea",
        enabled=False,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        McpToolPolicy.objects.create(
            organization=organization,
            workspace=workspace,
            tool_name="create_idea",
            enabled=True,
        )

    invalid = McpToolPolicy(
        organization=organization,
        workspace=other_workspace,
        tool_name="create_idea",
        enabled=False,
    )
    with pytest.raises(ValidationError, match="same organization"):
        invalid.full_clean()


@pytest.mark.django_db
@override_settings(MCP_SERVER_ENABLED=True)
def test_policy_precedence_org_disable_dominates_workspace_enable(django_user_model):
    from apps.mcp.models import McpOrganizationConfig, McpToolPolicy
    from apps.mcp.policy import evaluate_tool_policy
    from apps.members.models import WorkspaceMembership

    organization = _organization()
    workspace = _workspace(organization)
    user = django_user_model.objects.create_user(email="policy@example.com", password="test")
    membership = WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    principal = _principal(user, workspace, membership)
    McpOrganizationConfig.objects.create(organization=organization, enabled=True)
    McpToolPolicy.objects.create(organization=organization, tool_name="create_idea", enabled=False)
    McpToolPolicy.objects.create(
        organization=organization,
        workspace=workspace,
        tool_name="create_idea",
        enabled=True,
    )

    decision = evaluate_tool_policy(principal, _tool(), workspace=workspace)

    assert decision.allowed is False
    assert decision.code == "tool_disabled"
    assert decision.reason == "organization_tool_policy"


@pytest.mark.django_db
@override_settings(MCP_SERVER_ENABLED=True)
def test_policy_checks_org_switch_code_availability_scope_rbac_and_allowlist(django_user_model):
    from apps.mcp.models import McpOrganizationConfig
    from apps.mcp.policy import evaluate_tool_policy
    from apps.members.models import WorkspaceMembership

    organization = _organization()
    workspace = _workspace(organization)
    user = django_user_model.objects.create_user(email="boundaries@example.com", password="test")
    owner = WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    config = McpOrganizationConfig.objects.create(organization=organization, enabled=False)
    principal = _principal(user, workspace, owner)

    assert evaluate_tool_policy(principal, _tool(), workspace=workspace).reason == "organization_disabled"
    config.enabled = True
    config.save(update_fields=["enabled"])
    assert evaluate_tool_policy(principal, _tool(enabled=False), workspace=workspace).reason == "code_disabled"

    missing_scope = _principal(user, workspace, owner, scopes=("mcp.content",))
    assert evaluate_tool_policy(missing_scope, _tool(), workspace=workspace).reason == "oauth_scope"

    owner.workspace_role = WorkspaceMembership.WorkspaceRole.VIEWER
    owner.save(update_fields=["workspace_role"])
    viewer = _principal(user, workspace, owner)
    assert evaluate_tool_policy(viewer, _tool(), workspace=workspace).reason == "rbac"

    owner.workspace_role = WorkspaceMembership.WorkspaceRole.OWNER
    owner.save(update_fields=["workspace_role"])
    key_principal = _principal(user, workspace, owner, credential_kind="api_key", account_ids=())
    denied = evaluate_tool_policy(
        key_principal,
        _tool(),
        workspace=workspace,
        requested_account_ids={"00000000-0000-0000-0000-000000000001"},
    )
    assert denied.reason == "account_allowlist"


@override_settings(MCP_SERVER_ENABLED=False)
def test_infrastructure_kill_switch_has_highest_precedence():
    from apps.mcp.policy import evaluate_tool_policy

    decision = evaluate_tool_policy(SimpleNamespace(), _tool(enabled=False), workspace=None)
    assert decision.allowed is False
    assert decision.code == "server_disabled"
    assert decision.reason == "infrastructure"


def test_activity_summary_is_allowlisted_and_never_copies_content_or_secrets():
    from apps.mcp.activity import safe_activity_summary

    summary = safe_activity_summary(
        {
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "post_id": "00000000-0000-0000-0000-000000000002",
            "caption": "private campaign copy",
            "reply": "private customer reply",
            "confirmation_token": "secret-token",
            "signed_url": "https://bucket.example/private?signature=secret",
            "upload_bytes": b"private bytes",
            "changed_fields": ["status", "caption"],
            "item_count": 3,
        }
    )

    assert summary == {
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "post_id": "00000000-0000-0000-0000-000000000002",
        "changed_fields": ["status", "caption"],
        "item_count": 3,
    }
    serialized = repr(summary)
    assert "private" not in serialized
    assert "secret" not in serialized


@pytest.mark.django_db
@override_settings(MCP_SERVER_ENABLED=True)
def test_org_disabled_tools_disappear_from_both_transports_and_stale_calls_are_typed(django_user_model):
    from apps.mcp import legacy
    from apps.mcp.models import McpOrganizationConfig
    from apps.mcp.server import _call_tool_sync, _list_tools_sync
    from apps.members.models import WorkspaceMembership

    organization = _organization()
    workspace = _workspace(organization)
    user = django_user_model.objects.create_user(email="disabled@example.com", password="test")
    membership = WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    principal = _principal(user, workspace, membership, scopes=("mcp",))
    McpOrganizationConfig.objects.create(organization=organization, enabled=False)
    access_token = SimpleNamespace(principal=principal, django_request=SimpleNamespace())
    legacy_context = {"principal": principal, "request": SimpleNamespace()}

    assert _list_tools_sync(access_token) == []
    assert legacy._tools_list({}, legacy_context) == {"tools": []}

    sdk_result = _call_tool_sync(access_token, "list_accounts", {"workspace_id": str(workspace.id)})
    legacy_result = legacy._tools_call(
        {"name": "list_accounts", "arguments": {"workspace_id": str(workspace.id)}},
        legacy_context,
    )
    assert sdk_result["structuredContent"]["error"]["code"] == "organization_disabled"
    assert legacy_result["structuredContent"]["error"]["code"] == "organization_disabled"


@pytest.mark.django_db
def test_activity_recording_is_redacted_at_write_time_and_retained_for_365_days(django_user_model):
    from apps.mcp.activity import purge_expired_activity, record_activity
    from apps.mcp.models import McpActivityEvent
    from apps.members.models import WorkspaceMembership

    organization = _organization()
    workspace = _workspace(organization)
    user = django_user_model.objects.create_user(email="activity@example.com", password="test")
    membership = WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    principal = _principal(user, workspace, membership, scopes=("mcp",))
    now = timezone.now()

    event = record_activity(
        principal,
        {
            "method": "tools/call",
            "params": {
                "name": "create_draft",
                "arguments": {
                    "workspace_id": str(workspace.id),
                    "post_id": "00000000-0000-0000-0000-000000000002",
                    "caption": "private campaign copy",
                    "confirmation_token": "secret-token",
                },
            },
        },
        status_code=403,
        duration_ms=17,
        protocol_version="2026-07-28",
    )

    assert event is not None
    assert event.organization == organization
    assert event.workspace == workspace
    assert event.actor == user
    assert event.primitive == McpActivityEvent.Primitive.TOOL
    assert event.name == "create_draft"
    assert event.status == McpActivityEvent.Status.DENIED
    assert event.duration_ms == 17
    assert event.summary == {
        "workspace_id": str(workspace.id),
        "post_id": "00000000-0000-0000-0000-000000000002",
    }
    assert "private" not in repr(event.summary)
    assert "secret" not in repr(event.summary)

    McpActivityEvent.objects.filter(pk=event.pk).update(created_at=now - timedelta(days=366))
    recent = record_activity(principal, {"method": "ping"}, status_code=200, duration_ms=1)
    assert purge_expired_activity(now=now) == 1
    assert McpActivityEvent.objects.filter(pk=recent.pk).exists()


@pytest.mark.django_db
def test_legacy_mcp_audit_backfill_creates_only_coarse_activity(django_user_model):
    from django.apps import apps as django_apps

    from apps.api_keys.models import ApiKey, ApiKeyAuditLog
    from apps.mcp.models import McpActivityEvent
    from apps.members.models import WorkspaceMembership

    organization = _organization()
    workspace = _workspace(organization)
    user = django_user_model.objects.create_user(email="backfill@example.com", password="test")
    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    api_key = ApiKey.objects.create(
        workspace=workspace,
        issued_by=user,
        name="Historical MCP",
        lookup_prefix="history1",
        token_hash="0" * 64,
        permissions=["create_posts"],
    )
    audit = ApiKeyAuditLog.objects.create(
        api_key=api_key,
        action="mcp.tools/call:create_draft",
        method="POST",
        path="/api/v1/mcp",
        status_code=200,
    )
    McpActivityEvent.objects.all().delete()

    migration = importlib.import_module("apps.mcp.migrations.0001_initial")
    migration.backfill_legacy_mcp_activity(django_apps, None)

    event = McpActivityEvent.objects.get()
    assert event.organization == organization
    assert event.workspace == workspace
    assert event.actor == user
    assert event.primitive == McpActivityEvent.Primitive.TOOL
    assert event.name == "create_draft"
    assert event.summary == {"legacy_audit_id": str(audit.id)}
