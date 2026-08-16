from __future__ import annotations

import pytest

from apps.mcp.tests.test_content_tools import _call, _oauth_principal, _user, _workspace


def _post_with_target(workspace, user, *, status="draft"):
    from apps.composer.models import PlatformPost, Post
    from apps.social_accounts.models import SocialAccount

    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id=f"approval-{status}",
        account_name="Approval Company",
    )
    post = Post.objects.create(workspace=workspace, author=user, caption="Private campaign copy")
    platform_post = PlatformPost.objects.create(post=post, social_account=account, status=status)
    return post, platform_post


@pytest.fixture(autouse=True)
def _skip_workspace_write_throttle(monkeypatch):
    monkeypatch.setattr("apps.api.limits.enforce_workspace_write_rate_limit", lambda request, workspace_id: None)


@pytest.mark.django_db
def test_submit_for_review_preview_is_non_mutating_then_executes_once(django_user_model):
    from apps.approvals.models import ApprovalAction

    user = _user(django_user_model, "submit-review@example.com")
    workspace = _workspace("Review")
    principal = _oauth_principal(user, workspace)
    post, platform_post = _post_with_target(workspace, user)
    arguments = {"workspace_id": str(workspace.id), "post_id": str(post.id)}

    preview = _call(principal, "submit_for_review", arguments)
    platform_post.refresh_from_db()
    assert preview["confirmation_required"] is True
    assert preview["preview"] == {"post_id": str(post.id)}
    assert platform_post.status == "draft"
    assert not ApprovalAction.objects.exists()

    confirmed_arguments = {
        **arguments,
        "confirmation_token": preview["confirmation_token"],
        "idempotency_key": "submit-review-once",
    }
    confirmed = _call(principal, "submit_for_review", confirmed_arguments)
    replayed = _call(principal, "submit_for_review", confirmed_arguments)
    platform_post.refresh_from_db()
    assert confirmed["status"] == "pending_review"
    assert confirmed["replayed"] is False
    assert replayed["replayed"] is True
    assert platform_post.status == "pending_review"
    assert ApprovalAction.objects.filter(action=ApprovalAction.ActionType.SUBMITTED).count() == 1


@pytest.mark.django_db
def test_request_changes_confirmation_is_bound_to_comment(django_user_model):
    user = _user(django_user_model, "changes@example.com")
    workspace = _workspace("Changes")
    principal = _oauth_principal(user, workspace)
    post, platform_post = _post_with_target(workspace, user, status="pending_review")
    arguments = {
        "workspace_id": str(workspace.id),
        "post_id": str(post.id),
        "comment": "Please tighten the opening.",
    }

    preview = _call(principal, "request_changes", arguments)
    mismatch = _call(
        principal,
        "request_changes",
        {
            **arguments,
            "comment": "Different instruction",
            "confirmation_token": preview["confirmation_token"],
            "idempotency_key": "changes-once",
        },
    )
    platform_post.refresh_from_db()
    assert mismatch["error"]["code"] == "confirmation_payload_mismatch"
    assert platform_post.status == "pending_review"

    confirmed = _call(
        principal,
        "request_changes",
        {
            **arguments,
            "confirmation_token": preview["confirmation_token"],
            "idempotency_key": "changes-once",
        },
    )
    platform_post.refresh_from_db()
    assert confirmed["status"] == "changes_requested"
    assert platform_post.status == "changes_requested"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tool_name", "extra_arguments", "expected_status"),
    [
        ("approve_post", {"comment": "Approved"}, "approved"),
        ("reject_post", {"comment": "Not suitable"}, "rejected"),
    ],
)
def test_approve_and_reject_require_confirmation(
    django_user_model,
    tool_name,
    extra_arguments,
    expected_status,
):
    user = _user(django_user_model, f"{tool_name}@example.com")
    workspace = _workspace(tool_name)
    principal = _oauth_principal(user, workspace)
    post, platform_post = _post_with_target(workspace, user, status="pending_review")
    arguments = {
        "workspace_id": str(workspace.id),
        "post_id": str(post.id),
        **extra_arguments,
    }

    preview = _call(principal, tool_name, arguments)
    platform_post.refresh_from_db()
    assert platform_post.status == "pending_review"
    confirmed = _call(
        principal,
        tool_name,
        {
            **arguments,
            "confirmation_token": preview["confirmation_token"],
            "idempotency_key": f"{tool_name}-once",
        },
    )
    platform_post.refresh_from_db()
    assert confirmed["status"] == expected_status
    assert platform_post.status == expected_status


@pytest.mark.django_db
def test_comment_tools_preserve_visibility_and_do_not_expose_attachment_urls(django_user_model):
    from apps.approvals.models import PostComment

    owner = _user(django_user_model, "comment-owner@example.com")
    client = _user(django_user_model, "comment-client@example.com")
    workspace = _workspace("Comments")
    owner_principal = _oauth_principal(owner, workspace)
    client_principal = _oauth_principal(client, workspace, role="client")
    post, _ = _post_with_target(workspace, owner)

    internal = _call(
        owner_principal,
        "add_post_comment",
        {
            "workspace_id": str(workspace.id),
            "post_id": str(post.id),
            "body": "Internal note",
            "visibility": "internal",
        },
    )
    external = PostComment.objects.create(post=post, author=owner, body="Client-visible note", visibility="external")
    PostComment.objects.create(
        post=post,
        author=owner,
        parent_comment=external,
        body="Internal reply on external thread",
        visibility="internal",
    )

    owner_list = _call(
        owner_principal,
        "list_post_comments",
        {"workspace_id": str(workspace.id), "post_id": str(post.id)},
    )
    client_list = _call(
        client_principal,
        "list_post_comments",
        {"workspace_id": str(workspace.id), "post_id": str(post.id)},
    )
    assert internal["body"] == "Internal note"
    assert {comment["body"] for comment in owner_list["comments"]} == {
        "Internal note",
        "Client-visible note",
        "Internal reply on external thread",
    }
    assert [comment["body"] for comment in client_list["comments"]] == ["Client-visible note"]
    assert "attachment" not in repr(owner_list)
    assert "url" not in repr(owner_list)


def test_editorial_tools_advertise_scope_rbac_and_confirmation_metadata():
    from apps.mcp.registry import all_tools

    tools = {tool.name: tool for tool in all_tools()}
    assert tools["list_post_comments"].required_scope == "mcp.read"
    assert tools["add_post_comment"].required_scope == "mcp.content"
    assert tools["submit_for_review"].confirmation_required is True
    assert tools["submit_for_review"].required_permissions == ("create_posts",)
    for name in ("approve_post", "request_changes", "reject_post"):
        assert tools[name].confirmation_required is True
        assert tools[name].required_scope == "mcp.publish"
        assert tools[name].required_permissions == ("approve_posts",)
