from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.mcp.models import McpActivityEvent, McpOrganizationConfig, McpToolPolicy
from apps.members.models import OrgMembership, WorkspaceMembership


def _user(django_user_model, email: str):
    return django_user_model.objects.create_user(
        email=email,
        password="testpass123",
        name=email.split("@")[0],
        tos_accepted_at=timezone.now(),
    )


def _move_to_organization(user, organization, *, org_role):
    membership = user.org_memberships.get()
    membership.organization = organization
    membership.org_role = org_role
    membership.save(update_fields=["organization", "org_role"])
    return membership


@pytest.fixture
def mcp_settings_context(django_user_model):
    from apps.workspaces.models import Workspace

    admin = _user(django_user_model, "mcp-settings-admin@example.com")
    organization = admin.org_memberships.get().organization
    admin_membership = admin.org_memberships.get()
    admin_membership.org_role = OrgMembership.OrgRole.OWNER
    admin_membership.save(update_fields=["org_role"])
    first = Workspace.objects.create(organization=organization, name="ClashWise")
    second = Workspace.objects.create(organization=organization, name="Foreman")
    WorkspaceMembership.objects.create(
        user=admin,
        workspace=first,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    manager = _user(django_user_model, "mcp-workspace-manager@example.com")
    _move_to_organization(manager, organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=manager,
        workspace=first,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    outsider = _user(django_user_model, "mcp-outsider@example.com")
    outsider_membership = outsider.org_memberships.get()
    outsider_membership.org_role = OrgMembership.OrgRole.MEMBER
    outsider_membership.save(update_fields=["org_role"])
    outsider.workspace_memberships.update(workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER)
    return organization, first, second, admin, manager, outsider


def _logged_in(client, user):
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_mcp_settings_requires_an_authorized_user(client, mcp_settings_context):
    *_, outsider = mcp_settings_context
    anonymous = client.get(reverse("settings_manager:mcp_overview"))
    assert anonymous.status_code == 302
    response = _logged_in(client, outsider).get(reverse("settings_manager:mcp_overview"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_org_admin_sees_connection_status_and_can_toggle_organization(client, mcp_settings_context, settings):
    organization, _, _, admin, _, _ = mcp_settings_context
    settings.MCP_SERVER_ENABLED = True
    settings.MCP_TRANSPORT_BACKEND = "sdk_v2"
    response = _logged_in(client, admin).get(reverse("settings_manager:mcp_overview"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "/api/v1/mcp" in body
    assert "sdk_v2" in body
    assert "Organization access" in body

    response = client.post(reverse("settings_manager:mcp_overview"), {"enabled": "false"})
    assert response.status_code == 302
    assert McpOrganizationConfig.objects.get(organization=organization).enabled is False


@pytest.mark.django_db
def test_workspace_manager_cannot_toggle_organization(client, mcp_settings_context):
    organization, _, _, _, manager, _ = mcp_settings_context
    response = _logged_in(client, manager).post(
        reverse("settings_manager:mcp_overview"),
        {"enabled": "false"},
    )
    assert response.status_code == 403
    assert not McpOrganizationConfig.objects.filter(organization=organization).exists()


@pytest.mark.django_db
def test_activity_is_filtered_to_workspace_manager_scope(client, mcp_settings_context):
    organization, first, second, admin, manager, _ = mcp_settings_context
    common = {
        "organization": organization,
        "actor": admin,
        "credential_type": McpActivityEvent.CredentialType.API_KEY,
        "primitive": McpActivityEvent.Primitive.TOOL,
        "status": McpActivityEvent.Status.SUCCEEDED,
    }
    McpActivityEvent.objects.create(
        **common,
        workspace=first,
        name="create_draft",
        target_type="post",
        target_id="safe-first-id",
        summary={"post_id": "safe-first-id"},
    )
    McpActivityEvent.objects.create(
        **common,
        workspace=second,
        name="send_inbox_reply",
        target_type="message",
        target_id="hidden-second-id",
        summary={"message_id": "hidden-second-id"},
    )

    response = _logged_in(client, manager).get(reverse("settings_manager:mcp_activity"))
    body = response.content.decode()
    assert response.status_code == 200
    assert "create_draft" in body
    assert "safe-first-id" in body
    assert "send_inbox_reply" not in body
    assert "hidden-second-id" not in body


@pytest.mark.django_db
def test_org_admin_activity_filters_and_never_renders_sensitive_content(client, mcp_settings_context):
    organization, first, _, admin, _, _ = mcp_settings_context
    McpActivityEvent.objects.create(
        organization=organization,
        workspace=first,
        actor=admin,
        credential_type=McpActivityEvent.CredentialType.OAUTH,
        primitive=McpActivityEvent.Primitive.TOOL,
        name="schedule_post",
        status=McpActivityEvent.Status.SUCCEEDED,
        confirmation_state=McpActivityEvent.ConfirmationState.CONFIRMED,
        target_type="post",
        target_id="post-safe-id",
        target_path="/workspace/safe/post-safe-id/",
        summary={"post_id": "post-safe-id"},
    )
    response = _logged_in(client, admin).get(
        reverse("settings_manager:mcp_activity"),
        {
            "workspace": str(first.id),
            "tool": "schedule",
            "outcome": "succeeded",
            "primitive": "tool",
            "confirmation": "confirmed",
            "actor": "mcp-settings-admin",
            "credential": "oauth",
            "since": "7d",
        },
    )
    body = response.content.decode()
    assert response.status_code == 200
    assert "schedule_post" in body
    assert "post-safe-id" in body
    assert "caption" not in body.lower()
    assert "confirmation_token" not in body


@pytest.mark.django_db
def test_activity_paginates_and_revalidates_target_links(client, mcp_settings_context):
    organization, first, _, admin, _, _ = mcp_settings_context
    events = [
        McpActivityEvent(
            organization=organization,
            workspace=first,
            actor=admin,
            credential_type=McpActivityEvent.CredentialType.API_KEY,
            primitive=McpActivityEvent.Primitive.TOOL,
            name=f"tool_{index:02d}",
            status=McpActivityEvent.Status.SUCCEEDED,
            target_type="post",
            target_id=f"post-{index:02d}",
            target_path="//outside.example/unsafe?token=secret" if index == 0 else "/",
        )
        for index in range(51)
    ]
    McpActivityEvent.objects.bulk_create(events)
    client = _logged_in(client, admin)
    first_page = client.get(reverse("settings_manager:mcp_activity"))
    assert len(first_page.context["page_obj"].object_list) == 50
    assert "outside.example" not in first_page.content.decode()
    second_page = client.get(reverse("settings_manager:mcp_activity"), {"page": "2"})
    assert len(second_page.context["page_obj"].object_list) == 1
    assert "outside.example" not in second_page.content.decode()


@pytest.mark.django_db
def test_tool_registry_search_and_org_policy_update(client, mcp_settings_context):
    organization, _, _, admin, _, _ = mcp_settings_context
    response = _logged_in(client, admin).get(reverse("settings_manager:mcp_tools"), {"q": "schedule"})
    body = response.content.decode()
    assert response.status_code == 200
    assert "schedule_post" in body
    assert "list_accounts" not in body
    assert "mcp.publish" in body
    assert "Confirmation required" in body

    response = client.post(
        reverse("settings_manager:mcp_tools"),
        {"scope": "organization", "tool_name": "schedule_post", "enabled": "false"},
    )
    assert response.status_code == 302
    assert (
        McpToolPolicy.objects.get(
            organization=organization,
            workspace__isnull=True,
            tool_name="schedule_post",
        ).enabled
        is False
    )


@pytest.mark.django_db
def test_workspace_manager_can_only_change_policy_for_authorized_workspace(client, mcp_settings_context):
    organization, first, second, _, manager, _ = mcp_settings_context
    client = _logged_in(client, manager)
    allowed = client.post(
        reverse("settings_manager:mcp_tools"),
        {
            "scope": "workspace",
            "workspace_id": str(first.id),
            "tool_name": "schedule_post",
            "enabled": "false",
        },
    )
    assert allowed.status_code == 302
    assert McpToolPolicy.objects.get(workspace=first, tool_name="schedule_post").enabled is False

    denied = client.post(
        reverse("settings_manager:mcp_tools"),
        {
            "scope": "workspace",
            "workspace_id": str(second.id),
            "tool_name": "schedule_post",
            "enabled": "false",
        },
    )
    assert denied.status_code == 403
    assert not McpToolPolicy.objects.filter(workspace=second).exists()
    assert McpToolPolicy.objects.filter(organization=organization).count() == 1


@pytest.mark.django_db
def test_workspace_cannot_override_an_organization_tool_restriction(client, mcp_settings_context):
    organization, first, _, admin, manager, _ = mcp_settings_context
    McpToolPolicy.objects.create(
        organization=organization,
        workspace=None,
        tool_name="schedule_post",
        enabled=False,
    )
    client = _logged_in(client, manager)
    page = client.get(reverse("settings_manager:mcp_tools"), {"workspace": str(first.id), "q": "schedule_post"})
    assert page.status_code == 200
    assert b"Blocked by organization" in page.content
    response = client.post(
        reverse("settings_manager:mcp_tools"),
        {
            "scope": "workspace",
            "workspace_id": str(first.id),
            "tool_name": "schedule_post",
            "enabled": "true",
        },
    )
    assert response.status_code == 403
    assert not McpToolPolicy.objects.filter(workspace=first, tool_name="schedule_post").exists()


@pytest.mark.django_db
def test_mcp_policy_posts_require_csrf(mcp_settings_context):
    *_, admin, _, _ = mcp_settings_context
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin)
    response = client.post(
        reverse("settings_manager:mcp_tools"),
        {"scope": "organization", "tool_name": "schedule_post", "enabled": "false"},
    )
    assert response.status_code == 403
