from __future__ import annotations

import pytest
from django.http import HttpRequest
from django.utils import timezone

from apps.members.models import WorkspaceMembership


def _user(django_user_model, email="content-tools@example.com"):
    user = django_user_model.objects.create_user(
        email=email,
        password="testpass123",
        name="Content Tools",
        tos_accepted_at=timezone.now(),
    )
    WorkspaceMembership.objects.filter(user=user).delete()
    return user


def _workspace(name):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(organization=Organization.objects.create(name=f"{name} org"), name=name)


def _oauth_principal(user, *workspaces, role="owner"):
    from apps.mcp.principal import McpPrincipal, PrincipalWorkspace

    memberships = []
    for workspace in workspaces:
        membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
        memberships.append(
            PrincipalWorkspace(
                membership=membership,
                workspace=workspace,
                effective_permissions=frozenset(
                    key for key, enabled in membership.effective_permissions.items() if enabled
                ),
            )
        )
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
        authorized_workspaces=tuple(memberships),
    )


def _call(principal, name, arguments):
    from apps.mcp.legacy import _tools_call

    request = HttpRequest()
    request.user = principal.user
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    return _tools_call(
        {"name": name, "arguments": arguments},
        {"principal": principal, "request": request},
    )["structuredContent"]


@pytest.fixture(autouse=True)
def _skip_workspace_write_throttle(monkeypatch):
    monkeypatch.setattr("apps.api.limits.enforce_workspace_write_rate_limit", lambda request, workspace_id: None)


@pytest.mark.django_db
def test_list_workspaces_is_global_sorted_and_does_not_change_last_workspace(django_user_model):
    user = _user(django_user_model)
    second = _workspace("Zulu")
    first = _workspace("Alpha")
    principal = _oauth_principal(user, second, first)
    user.last_workspace_id = second.id
    user.save(update_fields=["last_workspace_id"])

    result = _call(principal, "list_workspaces", {})

    assert [item["name"] for item in result["workspaces"]] == ["Alpha", "Zulu"]
    assert result["workspaces"][0]["id"] == str(first.id)
    user.refresh_from_db()
    assert user.last_workspace_id == second.id


@pytest.mark.django_db
def test_list_workspaces_omits_organizations_where_mcp_is_disabled(django_user_model):
    from apps.mcp.models import McpOrganizationConfig

    user = _user(django_user_model)
    disabled = _workspace("Disabled")
    enabled = _workspace("Enabled")
    principal = _oauth_principal(user, disabled, enabled)
    McpOrganizationConfig.objects.create(organization=disabled.organization, enabled=False)

    result = _call(principal, "list_workspaces", {})

    assert result["workspaces"] == [
        {
            "id": str(enabled.id),
            "name": "Enabled",
            "organization_id": str(enabled.organization_id),
            "role": "owner",
            "timezone": enabled.effective_timezone,
        }
    ]


@pytest.mark.django_db
def test_workspace_context_contains_brand_policy_taxonomy_and_templates(django_user_model):
    from apps.composer.models import ContentCategory, PostTemplate, Tag

    user = _user(django_user_model)
    workspace = _workspace("Context")
    workspace.description = "A product workspace"
    workspace.timezone = "Europe/London"
    workspace.primary_color = "#112233"
    workspace.secondary_color = "#445566"
    workspace.default_hashtags = ["#brightbean"]
    workspace.approval_workflow_mode = "required_internal"
    workspace.save()
    ContentCategory.objects.create(workspace=workspace, name="Education", color="#123456")
    Tag.objects.create(workspace=workspace, name="launch")
    PostTemplate.objects.create(workspace=workspace, name="Launch note", description="Reusable")
    principal = _oauth_principal(user, workspace)

    result = _call(principal, "get_workspace_context", {"workspace_id": str(workspace.id)})

    assert result["description"] == "A product workspace"
    assert result["timezone"] == "Europe/London"
    assert result["brand_colors"] == {"primary": "#112233", "secondary": "#445566"}
    assert result["default_hashtags"] == ["#brightbean"]
    assert result["approval_policy"] == "required_internal"
    assert result["categories"][0]["name"] == "Education"
    assert result["tags"][0]["name"] == "launch"
    assert result["templates"][0]["name"] == "Launch note"


@pytest.mark.django_db
def test_account_health_is_allowlisted_diagnostic_with_browser_reconnect_link(django_user_model):
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model)
    workspace = _workspace("Health")
    principal = _oauth_principal(user, workspace)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="health-company",
        account_name="Health Company",
        connection_status=SocialAccount.ConnectionStatus.ERROR,
        last_error="provider token detail that must not leak",
        analytics_needs_reconnect=True,
        webhooks_active=False,
        webhook_needs_reconnect=True,
    )

    result = _call(
        principal,
        "get_account_health",
        {"workspace_id": str(workspace.id), "social_account_id": str(account.id)},
    )

    assert result["connection_status"] == "error"
    assert result["needs_reconnect"] is True
    assert set(result["issues"]) == {"connection", "analytics", "webhooks"}
    assert result["reconnect_path"].endswith(f"/{account.id}/reconnect/")
    assert "provider token detail" not in repr(result)
    assert "oauth_access_token" not in repr(result)


@pytest.mark.django_db
def test_ideas_crud_uses_stable_cursor_and_rejects_cross_workspace(django_user_model):
    user = _user(django_user_model)
    first = _workspace("Ideas first")
    second = _workspace("Ideas second")
    principal = _oauth_principal(user, first, second)
    created = []
    for title in ("First idea", "Second idea"):
        created.append(
            _call(
                principal,
                "create_idea",
                {
                    "workspace_id": str(first.id),
                    "title": title,
                    "description": f"{title} details",
                    "tags": ["launch"],
                },
            )
        )
    page_one = _call(principal, "list_ideas", {"workspace_id": str(first.id), "limit": 1})
    page_two = _call(
        principal,
        "list_ideas",
        {"workspace_id": str(first.id), "limit": 1, "cursor": page_one["next_cursor"]},
    )
    assert len(page_one["ideas"]) == len(page_two["ideas"]) == 1
    assert page_one["ideas"][0]["id"] != page_two["ideas"][0]["id"]
    assert page_two["next_cursor"] is None

    updated = _call(
        principal,
        "update_idea",
        {
            "workspace_id": str(first.id),
            "idea_id": created[0]["id"],
            "title": "Updated idea",
            "status": "in_progress",
        },
    )
    assert updated["title"] == "Updated idea"
    assert updated["status"] == "in_progress"

    denied = _call(
        principal,
        "update_idea",
        {"workspace_id": str(second.id), "idea_id": created[0]["id"], "title": "Cross tenant"},
    )
    assert denied["error"]["code"] == "resource_not_found"


@pytest.mark.django_db
def test_viewer_can_read_ideas_but_cannot_create_them(django_user_model):
    from apps.composer.models import Idea

    user = _user(django_user_model)
    workspace = _workspace("Read only")
    principal = _oauth_principal(user, workspace, role="viewer")
    Idea.objects.create(workspace=workspace, title="Visible")

    listed = _call(principal, "list_ideas", {"workspace_id": str(workspace.id)})
    denied = _call(
        principal,
        "create_idea",
        {"workspace_id": str(workspace.id), "title": "Must not exist"},
    )

    assert [idea["title"] for idea in listed["ideas"]] == ["Visible"]
    assert denied["error"]["code"] == "forbidden"
    assert not Idea.objects.filter(title="Must not exist").exists()


@pytest.mark.django_db
def test_convert_requires_a_target_and_update_draft_cannot_change_scheduled_content(django_user_model):
    from apps.composer.models import Idea, PlatformPost, Post
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model)
    workspace = _workspace("Mutation safety")
    principal = _oauth_principal(user, workspace)
    idea = Idea.objects.create(workspace=workspace, author=user, title="No target")

    no_target = _call(
        principal,
        "convert_idea_to_draft",
        {"workspace_id": str(workspace.id), "idea_id": str(idea.id)},
    )
    assert no_target["error"]["code"] == "invalid_request"
    assert not Post.objects.exists()

    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="scheduled-company",
        account_name="Scheduled Company",
    )
    post = Post.objects.create(workspace=workspace, author=user, caption="Original")
    PlatformPost.objects.create(post=post, social_account=account, status=PlatformPost.Status.SCHEDULED)
    denied = _call(
        principal,
        "update_draft",
        {
            "workspace_id": str(workspace.id),
            "post_id": str(post.id),
            "caption": "Must not change",
        },
    )
    post.refresh_from_db()
    assert denied["error"]["code"] == "invalid_state"
    assert post.caption == "Original"


@pytest.mark.django_db
def test_convert_update_and_clone_post_reuse_composer_services_and_allowlist(django_user_model):
    from apps.composer.models import Idea, PlatformPost, Post
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model)
    workspace = _workspace("Conversion")
    principal = _oauth_principal(user, workspace)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="convert-company",
        account_name="Convert Company",
    )
    idea = Idea.objects.create(
        workspace=workspace,
        author=user,
        title="Launch",
        description="Original caption",
        tags=["launch"],
    )
    converted = _call(
        principal,
        "convert_idea_to_draft",
        {
            "workspace_id": str(workspace.id),
            "idea_id": str(idea.id),
            "social_account_ids": [str(account.id)],
        },
    )
    post = Post.objects.get(id=converted["id"])
    idea.refresh_from_db()
    assert idea.post == post
    assert post.caption == "Original caption"
    assert list(post.platform_posts.values_list("social_account_id", flat=True)) == [account.id]

    updated = _call(
        principal,
        "update_draft",
        {
            "workspace_id": str(workspace.id),
            "post_id": str(post.id),
            "caption": "Updated caption",
            "internal_notes": "private",
        },
    )
    assert updated["caption"] == "Updated caption"
    assert updated["internal_notes"] == "private"

    cloned = _call(
        principal,
        "clone_post",
        {"workspace_id": str(workspace.id), "post_id": str(post.id)},
    )
    clone = Post.objects.get(id=cloned["id"])
    assert clone.id != post.id
    assert clone.caption == "Updated caption"
    assert clone.platform_posts.get().status == PlatformPost.Status.DRAFT


@pytest.mark.django_db
def test_api_key_content_tools_enforce_social_account_allowlist(django_user_model):
    from apps.api_keys.models import ApiKey
    from apps.composer.models import Idea, PlatformPost, Post
    from apps.mcp.principal import principal_from_api_key
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "content-key@example.com")
    workspace = _workspace("Allowlist")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    allowed = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="allowed-company",
        account_name="Allowed",
    )
    blocked = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="blocked-company",
        account_name="Blocked",
    )
    api_key = ApiKey.objects.create(
        workspace=workspace,
        issued_by=user,
        name="Content key",
        lookup_prefix="content1",
        token_hash="2" * 64,
        permissions=["create_posts"],
    )
    api_key.social_accounts.add(allowed)
    principal = principal_from_api_key(api_key)
    idea = Idea.objects.create(workspace=workspace, author=user, title="Scoped", description="Caption")

    denied = _call(
        principal,
        "convert_idea_to_draft",
        {"idea_id": str(idea.id), "social_account_ids": [str(blocked.id)]},
    )
    assert denied["error"]["code"] == "forbidden"
    assert not Post.objects.exists()

    source = Post.objects.create(workspace=workspace, author=user, caption="Mixed targets")
    PlatformPost.objects.create(post=source, social_account=allowed)
    PlatformPost.objects.create(post=source, social_account=blocked)
    clone_denied = _call(principal, "clone_post", {"post_id": str(source.id)})
    assert clone_denied["error"]["code"] == "resource_not_found"
    assert Post.objects.count() == 1


def test_discovery_and_content_tools_have_explicit_metadata():
    from apps.mcp.registry import all_tools

    tools = {tool.name: tool for tool in all_tools()}
    expected = {
        "list_workspaces",
        "get_workspace_context",
        "get_account_health",
        "list_ideas",
        "create_idea",
        "update_idea",
        "convert_idea_to_draft",
        "update_draft",
        "clone_post",
    }
    assert expected <= set(tools)
    assert tools["list_workspaces"].workspace_scoped is False
    assert "workspace_id" not in tools["list_workspaces"].input_schema["properties"]
    assert tools["list_ideas"].required_scope == "mcp.read"
    assert tools["create_idea"].required_scope == "mcp.content"
    assert tools["clone_post"].required_permissions == ("create_posts",)
    assert all(tool.output_schema.get("type") == "object" for tool in (tools[name] for name in expected))
