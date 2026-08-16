"""Shared application services for browser and MCP inbox workflows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from django.utils import timezone

from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from providers import get_provider

from .models import InboxMessage, InboxReply, InboxSLAConfig, InternalNote

_COMMENT_LIKE_TYPES = {
    InboxMessage.MessageType.COMMENT,
    InboxMessage.MessageType.MENTION,
    InboxMessage.MessageType.REVIEW,
}
HUMAN_AGENT_AFTER = timedelta(hours=24)


def _send_platform_reply(message: InboxMessage, body: str) -> str:
    """Deliver one reply through the account provider and return its remote id."""
    from apps.publisher.engine import _resolve_publish_credentials

    account = message.social_account
    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    extra = dict(message.extra or {})
    if message.sender_handle:
        extra.setdefault("recipient_id", message.sender_handle)
    if message.message_type in _COMMENT_LIKE_TYPES:
        result = provider.reply_to_comment(
            access_token=account.oauth_access_token,
            comment_id=message.platform_message_id,
            text=body,
            extra=extra,
        )
    else:
        result = provider.reply_to_message(
            access_token=account.oauth_access_token,
            message_id=message.platform_message_id,
            text=body,
            extra=extra,
            human_agent=timezone.now() - message.received_at > HUMAN_AGENT_AFTER,
        )
    return result.platform_message_id


def add_note(*, message: InboxMessage, author, body: str) -> InternalNote:
    return InternalNote.objects.create(inbox_message=message, author=author, body=body)


def assign_message(*, message: InboxMessage, actor, user_id: str | None):
    assigned_to = None
    if user_id:
        membership = (
            WorkspaceMembership.objects.filter(workspace=message.workspace, user_id=user_id)
            .select_related("user")
            .first()
        )
        if membership is None:
            raise ValueError("User is not a workspace member.")
        assigned_to = membership.user
    message.assigned_to = assigned_to
    message.save(update_fields=["assigned_to"])
    if assigned_to is not None and assigned_to != actor:
        notify(
            user=assigned_to,
            event_type=EventType.NEW_INBOX_MESSAGE,
            title=f"You were assigned a {message.get_message_type_display()}",
            body=f"From {message.sender_name}: {message.body[:100]}",
            data={"message_id": str(message.id), "workspace_id": str(message.workspace_id)},
        )
    return message


def set_status(*, message: InboxMessage, status: str) -> InboxMessage:
    if status not in InboxMessage.Status.values:
        raise ValueError("Invalid inbox status.")
    message.status = status
    message.save(update_fields=["status"])
    return message


def send_reply(
    *,
    message: InboxMessage,
    author,
    body: str,
    sender: Callable[[InboxMessage, str], str] | None = None,
) -> InboxReply:
    """Deliver then record a reply; a failed delivery never creates a sent row."""
    deliver = sender or _send_platform_reply
    try:
        platform_reply_id = deliver(message, body)
    except NotImplementedError:
        platform_reply_id = ""
    reply = InboxReply.objects.create(
        inbox_message=message,
        author=author,
        body=body,
        platform_reply_id=platform_reply_id,
    )
    sla_config = InboxSLAConfig.objects.filter(workspace=message.workspace, is_active=True).first()
    if sla_config and sla_config.auto_resolve_on_reply:
        message.status = InboxMessage.Status.RESOLVED
        message.save(update_fields=["status"])
    elif message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])
    return reply
