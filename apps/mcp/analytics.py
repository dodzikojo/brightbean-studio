"""Workspace-wide and evidence-based MCP analytics tools."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db.models import Max
from django.utils import timezone

from apps.analytics.api_builders import build_account_analytics
from apps.analytics.models import PostInsightsSnapshot
from apps.composer.models import PlatformPost
from apps.mcp.errors import DomainError
from apps.mcp.registry import Tool, register_tool
from apps.mcp.results import success_result

_ENGAGEMENT_KEYS = ("likes", "comments", "shares", "clicks", "saves")
_MIN_BEST_TIME_SAMPLE = 3


def _days(args: dict[str, Any]) -> int:
    days = args.get("days", 30)
    if not isinstance(days, int) or isinstance(days, bool) or not 7 <= days <= 90:
        raise DomainError("invalid_request", "days must be an integer between 7 and 90.")
    return days


def _accounts(context: dict[str, Any], account_id: str | None = None):
    queryset = context["workspace_context"].social_accounts.order_by("platform", "account_name", "id")
    if account_id is not None:
        try:
            parsed_account_id = UUID(account_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise DomainError("invalid_request", "account_id must be a valid UUID.") from exc
        queryset = queryset.filter(id=parsed_account_id)
        if not queryset.exists():
            raise DomainError("resource_not_found", "Social account not found.")
    return queryset


def _get_workspace_analytics(args: dict, context: dict[str, Any]) -> dict:
    days = _days(args)
    summaries = [build_account_analytics(account, days).model_dump(mode="json") for account in _accounts(context)]
    captured = [item["captured_at"] for item in summaries if item.get("captured_at")]
    return success_result(
        {
            "workspace_id": str(context["workspace"].id),
            "days": days,
            "account_count": len(summaries),
            "analytics_available_count": sum(bool(item["analytics_available"]) for item in summaries),
            "captured_at": max(captured) if captured else None,
            "accounts": summaries,
        }
    )


register_tool(
    Tool(
        name="get_workspace_analytics",
        description="Read typed analytics summaries for every authorized account in a workspace.",
        input_schema={
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 7, "maximum": 90, "default": 30}},
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_get_workspace_analytics,
    )
)


def _get_best_times(args: dict, context: dict[str, Any]) -> dict:
    days = _days(args)
    account_id = args.get("account_id")
    accounts = list(_accounts(context, account_id))
    account_ids = [account.id for account in accounts]
    cutoff = timezone.now() - timedelta(days=days)
    targets = list(
        PlatformPost.objects.filter(
            post__workspace=context["workspace"],
            social_account_id__in=account_ids,
            published_at__gte=cutoff,
        )
        .select_related("social_account")
        .order_by("id")
    )
    target_by_id = {target.id: target for target in targets}
    rows = (
        PostInsightsSnapshot.objects.filter(
            platform_post_id__in=target_by_id,
            metric_key__in=_ENGAGEMENT_KEYS,
        )
        .values("platform_post_id", "metric_key")
        .annotate(latest_date=Max("date"))
    )
    latest_keys = {(row["platform_post_id"], row["metric_key"], row["latest_date"]) for row in rows}
    scores: dict[Any, float] = defaultdict(float)
    if latest_keys:
        snapshots = PostInsightsSnapshot.objects.filter(
            platform_post_id__in=target_by_id,
            metric_key__in=_ENGAGEMENT_KEYS,
        )
        for snapshot in snapshots:
            if (snapshot.platform_post_id, snapshot.metric_key, snapshot.date) in latest_keys:
                scores[snapshot.platform_post_id] += snapshot.value

    workspace_tz = ZoneInfo(context["workspace"].effective_timezone or "UTC")
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    for target in targets:
        if target.published_at is None:
            continue
        local = target.published_at.astimezone(workspace_tz)
        buckets[(local.weekday(), local.hour)].append(scores.get(target.id, 0.0))
    recommendations: list[dict[str, Any]] = [
        {
            "day_of_week": weekday,
            "day_name": calendar.day_name[weekday],
            "hour": hour,
            "local_time": f"{hour:02d}:00",
            "score": round(sum(values) / len(values), 4),
            "sample_size": len(values),
        }
        for (weekday, hour), values in buckets.items()
        if len(values) >= _MIN_BEST_TIME_SAMPLE
    ]
    recommendations.sort(key=lambda item: (-item["score"], -item["sample_size"], item["day_of_week"], item["hour"]))
    return success_result(
        {
            "workspace_id": str(context["workspace"].id),
            "account_id": account_id,
            "days": days,
            "timezone": str(workspace_tz),
            "status": "ready" if recommendations else "insufficient_data",
            "minimum_sample_size": _MIN_BEST_TIME_SAMPLE,
            "analyzed_posts": len(targets),
            "recommendations": recommendations[:5],
        }
    )


register_tool(
    Tool(
        name="get_best_times",
        description=(
            "Rank observed publish-time buckets by measured engagement; returns insufficient_data "
            "instead of inventing recommendations when fewer than three posts support a bucket."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "format": "uuid"},
                "days": {"type": "integer", "minimum": 7, "maximum": 90, "default": 30},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_get_best_times,
    )
)
