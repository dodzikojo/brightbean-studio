"""MCP calendar reads and confirmed publishing mutations."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from ninja.errors import HttpError

from apps.api.limits import check_platform_quota
from apps.calendar.models import CustomCalendarEvent, Queue
from apps.calendar.services import QueueFullError, add_to_queue, reschedule_platform_post
from apps.composer.models import PlatformPost
from apps.composer.services import sync_post_scheduled_at, transition_platform_post
from apps.mcp.content import _post
from apps.mcp.errors import DomainError
from apps.mcp.registry import Tool, register_tool
from apps.mcp.results import success_result


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("invalid_request", f"{field} must be a valid UUID.") from exc


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("invalid_request", "scheduled_at must be an ISO-8601 datetime.") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("invalid_request", f"{field} must be an ISO-8601 date.") from exc


def _check_quota(account) -> None:
    if account.needs_reconnect:
        raise DomainError(
            "account_reconnect_required",
            "Reconnect the social account in BrightBean before scheduling or publishing.",
        )
    try:
        check_platform_quota(account)
    except HttpError as exc:
        raise DomainError(
            "quota_exceeded",
            "The social account's publishing quota has been reached.",
            retryable=True,
        ) from exc


def _get_calendar(args: dict, context: dict[str, Any]) -> dict:
    start = _date(args["start_date"], "start_date")
    end = _date(args["end_date"], "end_date")
    if end < start or end - start > timedelta(days=92):
        raise DomainError("invalid_request", "Calendar ranges must be ordered and at most 93 days.")
    workspace_context = context["workspace_context"]
    workspace_tz = ZoneInfo(context["workspace"].effective_timezone or "UTC")
    start_at = datetime.combine(start, time.min, tzinfo=workspace_tz)
    end_at = datetime.combine(end + timedelta(days=1), time.min, tzinfo=workspace_tz)
    scheduled = (
        PlatformPost.objects.filter(
            post__workspace=context["workspace"],
            social_account_id__in=workspace_context.allowed_account_ids,
            scheduled_at__gte=start_at,
            scheduled_at__lt=end_at,
        )
        .select_related("post", "social_account")
        .order_by("scheduled_at", "id")
    )
    events = CustomCalendarEvent.objects.filter(
        workspace=context["workspace"], start_date__lte=end, end_date__gte=start
    ).order_by("start_date", "id")
    return success_result(
        {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "posts": [
                {
                    "post_id": str(pp.post_id),
                    "platform_post_id": str(pp.id),
                    "social_account_id": str(pp.social_account_id),
                    "platform": pp.social_account.platform,
                    "title": pp.post.title,
                    "status": pp.status,
                    "scheduled_at": pp.scheduled_at.isoformat() if pp.scheduled_at else None,
                }
                for pp in scheduled
            ],
            "events": [
                {
                    "id": str(event.id),
                    "title": event.title,
                    "description": event.description,
                    "start_date": event.start_date.isoformat(),
                    "end_date": event.end_date.isoformat(),
                    "color": event.color,
                }
                for event in events
            ],
        }
    )


register_tool(
    Tool(
        name="get_calendar",
        description="Read authorized scheduled targets and custom events for a date range up to 93 days.",
        input_schema={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
            },
            "required": ["start_date", "end_date"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_get_calendar,
    )
)


def _list_queues(args: dict, context: dict[str, Any]) -> dict:
    del args
    allowed = context["workspace_context"].allowed_account_ids
    queues = (
        Queue.objects.filter(workspace=context["workspace"], social_account_id__in=allowed)
        .select_related("social_account", "category")
        .annotate(entry_count=Count("entries"))
        .order_by("name", "id")
    )
    return success_result(
        {
            "queues": [
                {
                    "id": str(queue.id),
                    "name": queue.name,
                    "social_account_id": str(queue.social_account_id),
                    "platform": queue.social_account.platform,
                    "category_id": str(queue.category_id) if queue.category_id else None,
                    "is_active": queue.is_active,
                    "entry_count": queue.entry_count,
                }
                for queue in queues
            ]
        }
    )


register_tool(
    Tool(
        name="list_queues",
        description="List publishing queues attached to authorized social accounts.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": True},
        handler=_list_queues,
    )
)


def _queue(args: dict, context: dict[str, Any]) -> Queue:
    queue = (
        Queue.objects.filter(
            id=_uuid(args["queue_id"], "queue_id"),
            workspace=context["workspace"],
            social_account_id__in=context["workspace_context"].allowed_account_ids,
            is_active=True,
        )
        .select_related("social_account")
        .first()
    )
    if queue is None:
        raise DomainError("resource_not_found", "Queue not found.")
    return queue


def _enqueue_post(args: dict, context: dict[str, Any]) -> dict:
    post = _post(context, args["post_id"])
    queue = _queue(args, context)
    platform_post = post.platform_posts.filter(social_account=queue.social_account).first()
    if platform_post is None:
        raise DomainError("invalid_request", "The post does not target this queue's social account.")
    try:
        _check_quota(queue.social_account)
        with transaction.atomic():
            entry = add_to_queue(post, queue, priority=args.get("priority", False))
            if entry is None or entry.assigned_slot_datetime is None:
                raise ValueError("The post cannot be queued in its current state.")
            assigned_at = entry.assigned_slot_datetime
            assert assigned_at is not None
            if platform_post.status == PlatformPost.Status.SCHEDULED:
                sync_post_scheduled_at(post)
            else:
                transition_platform_post(platform_post, "scheduled", scheduled_at=assigned_at)
    except QueueFullError as exc:
        raise DomainError("queue_full", "The queue has no available slot.") from exc
    except ValueError as exc:
        raise DomainError("invalid_state", str(exc)) from exc
    return success_result(
        {
            "id": str(entry.id),
            "post_id": str(post.id),
            "queue_id": str(queue.id),
            "status": platform_post.status,
            "scheduled_at": assigned_at.isoformat(),
        }
    )


def _reschedule_post(args: dict, context: dict[str, Any]) -> dict:
    workspace_context = context["workspace_context"]
    platform_post = (
        PlatformPost.objects.filter(
            id=_uuid(args["platform_post_id"], "platform_post_id"),
            post__workspace=context["workspace"],
            social_account_id__in=workspace_context.allowed_account_ids,
        )
        .select_related("post", "social_account")
        .first()
    )
    if platform_post is None:
        raise DomainError("resource_not_found", "Post target not found.")
    scheduled_at = _datetime(args["scheduled_at"])
    try:
        _check_quota(platform_post.social_account)
        reschedule_platform_post(platform_post, scheduled_at)
    except ValueError as exc:
        raise DomainError("invalid_state", str(exc)) from exc
    return success_result(
        {
            "id": str(platform_post.id),
            "post_id": str(platform_post.post_id),
            "status": platform_post.status,
            "scheduled_at": scheduled_at.isoformat(),
        }
    )


def _publish_post(args: dict, context: dict[str, Any]) -> dict:
    post = _post(context, args["post_id"])
    targets = list(
        post.platform_posts.exclude(status__in=PlatformPost.PROTECTED_STATUSES).select_related("social_account")
    )
    if not targets:
        raise DomainError("invalid_state", "The post has no publishable targets.")
    now = timezone.now()
    try:
        for target in targets:
            _check_quota(target.social_account)
        with transaction.atomic():
            for target in targets:
                if target.status == PlatformPost.Status.SCHEDULED:
                    target.scheduled_at = now
                    target.save(update_fields=["scheduled_at", "updated_at"])
                else:
                    transition_platform_post(target, PlatformPost.Status.SCHEDULED, scheduled_at=now)
            sync_post_scheduled_at(post)
    except ValueError as exc:
        raise DomainError("invalid_state", str(exc)) from exc
    return success_result(
        {"id": str(post.id), "post_id": str(post.id), "status": post.status, "scheduled_at": now.isoformat()}
    )


def _confirmed_tool(name: str, description: str, properties: dict[str, Any], required: list[str], handler) -> None:
    register_tool(
        Tool(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            handler=handler,
        )
    )


_confirmed_tool(
    "enqueue_post",
    "Place a post into an authorized queue's next slot.",
    {
        "post_id": {"type": "string", "format": "uuid"},
        "queue_id": {"type": "string", "format": "uuid"},
        "priority": {"type": "boolean", "default": False},
    },
    ["post_id", "queue_id"],
    _enqueue_post,
)
_confirmed_tool(
    "reschedule_post",
    "Move one authorized platform target to a new publish time.",
    {
        "platform_post_id": {"type": "string", "format": "uuid"},
        "scheduled_at": {"type": "string", "format": "date-time"},
    },
    ["platform_post_id", "scheduled_at"],
    _reschedule_post,
)
_confirmed_tool(
    "publish_post",
    "Queue every authorized eligible target for immediate publication by the worker.",
    {"post_id": {"type": "string", "format": "uuid"}},
    ["post_id"],
    _publish_post,
)
