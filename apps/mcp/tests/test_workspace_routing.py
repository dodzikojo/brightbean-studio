from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.mcp.errors import DomainError
from apps.members.models import WorkspaceMembership


def _user(email: str):
    from apps.accounts.models import User

    user = User.objects.create_user(email=email, password="testpass123", name=email, tos_accepted_at=timezone.now())
    WorkspaceMembership.objects.filter(user=user).delete()
    return user


def _workspace(name: str):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name=name, organization=Organization.objects.create(name=f"Org {name}"))


def _oauth_principal(user):
    from apps.mcp.principal import McpPrincipal, PrincipalWorkspace

    memberships = WorkspaceMembership.objects.filter(user=user).select_related("workspace", "workspace__organization")
    return McpPrincipal(
        credential_kind="oauth",
        user=user,
        api_key=None,
        oauth_access_token=None,
        oauth_client=None,
        granted_scopes=frozenset({"mcp"}),
        workspace_pin_id=None,
        api_key_permissions=frozenset(),
        account_allowlist_ids=frozenset(),
        authorized_workspaces=tuple(
            PrincipalWorkspace(
                membership=m,
                workspace=m.workspace,
                effective_permissions=frozenset(k for k, value in m.effective_permissions.items() if value),
            )
            for m in memberships
            if not m.workspace.is_archived
        ),
    )


@pytest.mark.django_db
def test_oauth_omission_requires_workspace_and_candidates_are_safe_and_sorted():
    from apps.mcp.workspace import resolve_workspace

    user = _user("routing-many@example.com")
    zebra = _workspace("zebra")
    alpha = _workspace("Alpha")
    WorkspaceMembership.objects.create(user=user, workspace=zebra, workspace_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=alpha, workspace_role="viewer")
    principal = _oauth_principal(user)

    with pytest.raises(DomainError) as caught:
        resolve_workspace(principal, None)

    error = caught.value.as_structured_content()["error"]
    assert error["code"] == "workspace_required"
    assert error["details"]["workspaces"] == [
        {"id": str(alpha.id), "name": "Alpha"},
        {"id": str(zebra.id), "name": "zebra"},
    ]
    assert set(error["details"]["workspaces"][0]) == {"id", "name"}


@pytest.mark.django_db
def test_oauth_zero_requires_workspace_one_defaults_and_explicit_routes_without_last_workspace():
    from apps.mcp.workspace import resolve_workspace

    user = _user("routing-counts@example.com")
    with pytest.raises(DomainError) as empty:
        resolve_workspace(_oauth_principal(user), None)
    assert empty.value.code == "workspace_required"
    assert empty.value.details == {"workspaces": []}

    first = _workspace("First")
    WorkspaceMembership.objects.create(user=user, workspace=first, workspace_role="owner")
    assert resolve_workspace(_oauth_principal(user), None).workspace == first

    second = _workspace("Second")
    WorkspaceMembership.objects.create(user=user, workspace=second, workspace_role="viewer")
    user.last_workspace_id = first.id
    user.save(update_fields=["last_workspace_id"])
    principal = _oauth_principal(user)
    assert resolve_workspace(principal, str(second.id)).workspace == second
    user.refresh_from_db()
    assert user.last_workspace_id == first.id


@pytest.mark.django_db
def test_explicit_foreign_and_nonexistent_ids_are_indistinguishable_without_workspace_lookup(monkeypatch):
    from apps.mcp.workspace import resolve_workspace
    from apps.workspaces.models import Workspace

    user = _user("routing-private@example.com")
    own = _workspace("Own")
    foreign = _workspace("Foreign")
    WorkspaceMembership.objects.create(user=user, workspace=own, workspace_role="owner")
    principal = _oauth_principal(user)
    monkeypatch.setattr(Workspace.objects, "get", lambda *args, **kwargs: pytest.fail("arbitrary workspace lookup"))

    failures = []
    for workspace_id in (str(foreign.id), str(uuid4())):
        with pytest.raises(DomainError) as caught:
            resolve_workspace(principal, workspace_id)
        failures.append(caught.value.as_structured_content())
    assert failures[0] == failures[1]
    assert failures[0]["error"]["code"] == "forbidden"


@pytest.mark.django_db
def test_api_key_pin_allows_omission_and_match_but_rejects_conflict_and_malformed():
    from apps.mcp.principal import McpPrincipal, PrincipalWorkspace
    from apps.mcp.workspace import resolve_workspace

    user = _user("routing-key@example.com")
    workspace = _workspace("Pinned")
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    item = PrincipalWorkspace(
        membership, workspace, frozenset(k for k, v in membership.effective_permissions.items() if v)
    )
    principal = McpPrincipal(
        credential_kind="api_key",
        user=user,
        api_key=None,
        oauth_access_token=None,
        oauth_client=None,
        granted_scopes=frozenset({"mcp"}),
        workspace_pin_id=workspace.id,
        api_key_permissions=frozenset({"create_posts"}),
        account_allowlist_ids=frozenset(),
        authorized_workspaces=(item,),
    )

    assert resolve_workspace(principal, None).workspace == workspace
    assert resolve_workspace(principal, str(workspace.id)).workspace == workspace
    with pytest.raises(DomainError) as conflict:
        resolve_workspace(principal, str(uuid4()))
    assert conflict.value.code == "forbidden"
    with pytest.raises(DomainError) as malformed:
        resolve_workspace(principal, "not-a-uuid")
    assert malformed.value.code == "invalid_request"


@pytest.mark.django_db
def test_resolved_account_ids_are_workspace_limited_for_oauth_and_key():
    from apps.mcp.workspace import resolve_workspace
    from apps.social_accounts.models import SocialAccount

    user = _user("routing-accounts@example.com")
    first = _workspace("First")
    second = _workspace("Second")
    WorkspaceMembership.objects.create(user=user, workspace=first, workspace_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=second, workspace_role="owner")
    first_account = SocialAccount.objects.create(
        workspace=first, platform="linkedin_personal", account_platform_id="first", account_name="First"
    )
    SocialAccount.objects.create(
        workspace=second, platform="linkedin_personal", account_platform_id="second", account_name="Second"
    )

    oauth_context = resolve_workspace(_oauth_principal(user), first.id)
    assert oauth_context.allowed_account_ids == frozenset({first_account.id})

    key_principal = replace(_oauth_principal(user), credential_kind="api_key", workspace_pin_id=first.id)
    key_principal = replace(
        key_principal,
        authorized_workspaces=tuple(
            item for item in key_principal.authorized_workspaces if item.workspace.id == first.id
        ),
    )
    key_principal = replace(key_principal, account_allowlist_ids=frozenset({first_account.id, uuid4()}))
    key_context = resolve_workspace(key_principal, None)
    assert key_context.allowed_account_ids == frozenset({first_account.id})


@pytest.mark.django_db
def test_read_only_tool_context_does_not_consume_workspace_write_quota(monkeypatch):
    from apps.mcp import workspace as workspace_module

    user = _user("routing-read-limit@example.com")
    workspace = _workspace("Read only")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    principal = _oauth_principal(user)
    charged = []
    monkeypatch.setattr(
        "apps.api.limits.enforce_workspace_write_rate_limit",
        lambda request, workspace_id: charged.append((request, workspace_id)),
    )

    workspace_module.build_tool_context(principal, workspace.id, object(), is_write=False)

    assert charged == []


def test_nested_typed_tool_errors_produce_failed_audit_status():
    from apps.mcp.legacy import _status_for_response

    response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": True,
            "structuredContent": {"error": {"code": "forbidden", "message": "Denied"}},
        },
    }

    assert _status_for_response(response) == 403
