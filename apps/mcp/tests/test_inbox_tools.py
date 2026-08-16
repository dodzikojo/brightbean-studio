from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.mcp.tests.test_content_tools import _call, _oauth_principal, _user, _workspace


def _message(workspace, account, *, body="Need help", status="unread"):
    from apps.inbox.models import InboxMessage

    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id=f"message-{InboxMessage.objects.count()}",
        message_type=InboxMessage.MessageType.DM,
        sender_name="Customer",
        sender_handle="customer-1",
        body=body,
        status=status,
        received_at=timezone.now() - timedelta(minutes=5),
    )


@pytest.mark.django_db
def test_inbox_reads_are_workspace_and_account_scoped(django_user_model):
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "inbox-read@example.com")
    workspace = _workspace("Inbox read")
    other = _workspace("Other inbox")
    principal = _oauth_principal(user, workspace, other)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="inbox-read-account",
        account_name="Inbox account",
    )
    other_account = SocialAccount.objects.create(
        workspace=other,
        platform="facebook",
        account_platform_id="other-inbox-account",
        account_name="Other account",
    )
    message = _message(workspace, account)
    _message(other, other_account, body="Private other workspace")

    listed = _call(principal, "list_inbox", {"workspace_id": str(workspace.id)})
    detail = _call(
        principal,
        "get_inbox_message",
        {"workspace_id": str(workspace.id), "message_id": str(message.id)},
    )

    assert [item["id"] for item in listed["messages"]] == [str(message.id)]
    assert detail["body"] == "Need help"
    assert "Private other workspace" not in str(listed)


@pytest.mark.django_db
def test_inbox_note_assignment_and_status_reuse_domain_services(django_user_model):
    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "inbox-write@example.com")
    assignee = _user(django_user_model, "inbox-assignee@example.com")
    workspace = _workspace("Inbox mutations")
    principal = _oauth_principal(user, workspace)
    WorkspaceMembership.objects.create(user=assignee, workspace=workspace, workspace_role="member")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="inbox-write-account",
        account_name="Inbox account",
    )
    message = _message(workspace, account)
    base = {"workspace_id": str(workspace.id), "message_id": str(message.id)}

    note = _call(principal, "add_inbox_note", {**base, "body": "Escalate to support"})
    assigned = _call(principal, "assign_inbox_message", {**base, "user_id": str(assignee.id)})
    changed = _call(principal, "set_inbox_status", {**base, "status": "resolved"})

    message.refresh_from_db()
    assert note["body"] == "Escalate to support"
    assert assigned["assigned_to_id"] == str(assignee.id)
    assert changed["status"] == "resolved"
    assert message.assigned_to == assignee
    assert message.status == "resolved"


@pytest.mark.django_db
def test_send_inbox_reply_previews_then_executes_exactly_once(django_user_model):
    from apps.inbox.models import InboxReply
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "inbox-reply@example.com")
    workspace = _workspace("Inbox reply")
    principal = _oauth_principal(user, workspace)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="inbox-reply-account",
        account_name="Inbox account",
    )
    message = _message(workspace, account)
    arguments = {
        "workspace_id": str(workspace.id),
        "message_id": str(message.id),
        "body": "Thanks, we are on it.",
    }

    preview = _call(principal, "send_inbox_reply", arguments)
    assert preview["confirmation_required"] is True
    assert not InboxReply.objects.exists()

    confirmed_args = {
        **arguments,
        "confirmation_token": preview["confirmation_token"],
        "idempotency_key": "reply-once",
    }
    with patch("apps.inbox.services._send_platform_reply", return_value="reply-1") as sender:
        confirmed = _call(principal, "send_inbox_reply", confirmed_args)
        replayed = _call(principal, "send_inbox_reply", confirmed_args)

    assert confirmed["replayed"] is False
    assert replayed["replayed"] is True
    assert sender.call_count == 1
    assert InboxReply.objects.filter(inbox_message=message).count() == 1


def test_inbox_registry_metadata_requires_correct_scopes_permissions_and_confirmation():
    from apps.mcp.registry import all_tools

    tools = {tool.name: tool for tool in all_tools()}
    for name in ("list_inbox", "get_inbox_message"):
        assert tools[name].required_scope == "mcp.read"
        assert tools[name].required_permissions == ("use_inbox",)
    for name in ("add_inbox_note", "assign_inbox_message", "set_inbox_status"):
        assert tools[name].required_scope == "mcp.content"
        assert tools[name].required_permissions == ("reply_from_inbox",)
    assert tools["send_inbox_reply"].required_scope == "mcp.inbox.reply"
    assert tools["send_inbox_reply"].required_permissions == ("reply_from_inbox",)
    assert tools["send_inbox_reply"].confirmation_required is True
