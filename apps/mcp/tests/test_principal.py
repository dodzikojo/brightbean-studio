from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.api_keys.services import issue_api_key
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership


def _user(email: str):
    from apps.accounts.models import User

    user = User.objects.create_user(email=email, password="testpass123", name=email, tos_accepted_at=timezone.now())
    WorkspaceMembership.objects.filter(user=user).delete()
    return user


def _workspace(name: str, *, archived: bool = False):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    org = Organization.objects.create(name=f"Org {name}")
    return Workspace.objects.create(name=name, organization=org, is_archived=archived)


def _oauth_token(user, *, scope: str = "mcp mcp.read"):
    from oauth2_provider.models import get_access_token_model, get_application_model

    from apps.oauth_server.resources import canonical_mcp_resource_uri

    app_model = get_application_model()
    app = app_model.objects.create(
        name="MCP test",
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://client.example/callback",
    )
    return get_access_token_model().objects.create(
        user=user,
        application=app,
        token=f"secret-{user.pk}",
        scope=scope,
        resource=[canonical_mcp_resource_uri()],
        expires=timezone.now() + timedelta(hours=1),
    )


@pytest.mark.django_db
def test_api_key_principal_is_pinned_and_intersects_current_permissions():
    from apps.mcp.principal import principal_from_api_key
    from apps.social_accounts.models import SocialAccount

    user = _user("key-principal@example.com")
    workspace = _workspace("Pinned")
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role="owner")
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    allowed = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="allowed",
        account_name="Allowed",
    )
    issued = issue_api_key(
        workspace=workspace,
        social_accounts=[allowed],
        issued_by=user,
        name="test",
        permissions=list(PERMISSION_KEYS),
    )
    membership.workspace_role = "viewer"
    membership.save(update_fields=["workspace_role"])

    principal = principal_from_api_key(issued.api_key)

    assert principal.credential_kind == "api_key"
    assert principal.workspace_pin_id == workspace.id
    assert principal.account_allowlist_ids == frozenset({allowed.id})
    assert principal.authorized_workspaces[0].membership == membership
    assert principal.authorized_workspaces[0].effective_permissions == frozenset({"view_analytics"})
    with pytest.raises(FrozenInstanceError):
        principal.workspace_pin_id = None  # type: ignore[misc]


@pytest.mark.django_db
def test_api_key_principal_excludes_corrupt_cross_workspace_allowlist():
    from apps.mcp.principal import principal_from_api_key
    from apps.social_accounts.models import SocialAccount

    user = _user("key-corrupt@example.com")
    workspace = _workspace("Pinned")
    other = _workspace("Other")
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    allowed = SocialAccount.objects.create(
        workspace=workspace, platform="linkedin_personal", account_platform_id="good", account_name="Good"
    )
    foreign = SocialAccount.objects.create(
        workspace=other, platform="linkedin_personal", account_platform_id="bad", account_name="Bad"
    )
    issued = issue_api_key(
        workspace=workspace,
        social_accounts=[allowed],
        issued_by=user,
        name="test",
        permissions=["create_posts"],
    )
    issued.api_key.social_accounts.add(foreign)

    principal = principal_from_api_key(issued.api_key)

    assert principal.account_allowlist_ids == frozenset({allowed.id})


@pytest.mark.django_db
def test_oauth_principal_discovers_zero_one_many_active_memberships_and_actual_scopes():
    from apps.mcp.principal import principal_from_oauth_token

    user = _user("oauth-principal@example.com")
    active = _workspace("Active")
    second = _workspace("Second")
    archived = _workspace("Archived", archived=True)
    WorkspaceMembership.objects.create(user=user, workspace=active, workspace_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=second, workspace_role="viewer")
    WorkspaceMembership.objects.create(user=user, workspace=archived, workspace_role="owner")
    token = _oauth_token(user, scope="mcp mcp.read mcp.content")
    user.last_workspace_id = archived.id
    user.save(update_fields=["last_workspace_id"])

    principal = principal_from_oauth_token(token)

    assert principal.credential_kind == "oauth"
    assert principal.oauth_client == token.application
    assert principal.granted_scopes == frozenset({"mcp", "mcp.read", "mcp.content"})
    assert {item.workspace.id for item in principal.authorized_workspaces} == {active.id, second.id}
    assert principal.workspace_pin_id is None
    assert "secret-" not in repr(principal)
    assert user.last_workspace_id == archived.id


@pytest.mark.django_db
def test_archived_api_key_workspace_is_rejected_by_token_verifier():
    from apps.api_keys.services import verify_token
    from apps.social_accounts.models import SocialAccount

    user = _user("archived-key@example.com")
    workspace = _workspace("Archived Key")
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="linkedin_personal", account_platform_id="archived", account_name="Archived"
    )
    issued = issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="test",
        permissions=["create_posts"],
    )
    workspace.is_archived = True
    workspace.save(update_fields=["is_archived"])

    assert verify_token(issued.plaintext_token) is None


@pytest.mark.django_db
def test_inactive_users_are_rejected_for_api_key_and_oauth_credentials():
    from apps.api_keys.services import verify_token
    from apps.mcp.errors import DomainError
    from apps.mcp.principal import principal_from_oauth_token
    from apps.social_accounts.models import SocialAccount

    user = _user("inactive-principal@example.com")
    workspace = _workspace("Inactive")
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="linkedin_personal", account_platform_id="inactive", account_name="Inactive"
    )
    issued = issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="inactive",
        permissions=["create_posts"],
    )
    token = _oauth_token(user)
    user.is_active = False
    user.save(update_fields=["is_active"])

    assert verify_token(issued.plaintext_token) is None
    token.refresh_from_db()
    with pytest.raises(DomainError, match="not authorized"):
        principal_from_oauth_token(token)
