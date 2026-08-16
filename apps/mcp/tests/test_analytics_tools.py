from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.mcp.tests.test_content_tools import _call, _oauth_principal, _user, _workspace


@pytest.mark.django_db
def test_workspace_analytics_returns_only_authorized_account_summaries(django_user_model):
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "workspace-analytics@example.com")
    workspace = _workspace("Workspace analytics")
    principal = _oauth_principal(user, workspace)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="workspace-analytics",
        account_name="Analytics Company",
    )

    result = _call(
        principal,
        "get_workspace_analytics",
        {"workspace_id": str(workspace.id), "days": 30},
    )

    assert result["workspace_id"] == str(workspace.id)
    assert result["account_count"] == 1
    assert result["accounts"][0]["account_id"] == str(account.id)
    assert result["accounts"][0]["days"] == 30


def _published_target(workspace, user, account, published_at, score):
    from apps.analytics.models import PostInsightsSnapshot
    from apps.composer.models import PlatformPost, Post

    post = Post.objects.create(workspace=workspace, author=user, caption="Measured post")
    target = PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
        published_at=published_at,
    )
    PostInsightsSnapshot.objects.create(
        platform_post=target,
        metric_key="likes",
        date=published_at.date(),
        value=score,
    )
    return target


@pytest.mark.django_db
def test_best_times_uses_measured_engagement_and_minimum_sample(django_user_model):
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "best-times@example.com")
    workspace = _workspace("Best times")
    workspace.timezone = "Europe/London"
    workspace.save(update_fields=["timezone"])
    principal = _oauth_principal(user, workspace)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="best-times",
        account_name="Best Times Company",
    )
    baseline = (timezone.now() - timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
    for score in (3, 6, 9):
        _published_target(workspace, user, account, baseline, score)

    result = _call(
        principal,
        "get_best_times",
        {
            "workspace_id": str(workspace.id),
            "account_id": str(account.id),
            "days": 30,
        },
    )

    assert result["status"] == "ready"
    assert result["minimum_sample_size"] == 3
    assert result["analyzed_posts"] == 3
    assert result["recommendations"][0]["sample_size"] == 3
    assert result["recommendations"][0]["score"] == 6.0


@pytest.mark.django_db
def test_best_times_reports_insufficient_data_without_fabricating_slots(django_user_model):
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "sparse-times@example.com")
    workspace = _workspace("Sparse times")
    principal = _oauth_principal(user, workspace)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="sparse-times",
        account_name="Sparse Company",
    )
    _published_target(workspace, user, account, timezone.now() - timedelta(days=1), 10)

    result = _call(
        principal,
        "get_best_times",
        {"workspace_id": str(workspace.id), "account_id": str(account.id)},
    )

    assert result["status"] == "insufficient_data"
    assert result["analyzed_posts"] == 1
    assert result["recommendations"] == []


def test_workspace_analytics_tools_advertise_read_scope_and_permission():
    from apps.mcp.registry import all_tools

    tools = {tool.name: tool for tool in all_tools()}
    for name in ("get_workspace_analytics", "get_best_times"):
        assert tools[name].required_scope == "mcp.read"
        assert tools[name].required_permissions == ("view_analytics",)
        assert tools[name].annotations.read_only is True
