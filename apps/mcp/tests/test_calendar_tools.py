from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.mcp.tests.test_approval_tools import _post_with_target
from apps.mcp.tests.test_content_tools import _call, _oauth_principal, _user, _workspace


@pytest.fixture(autouse=True)
def _skip_workspace_write_throttle(monkeypatch):
    monkeypatch.setattr("apps.api.limits.enforce_workspace_write_rate_limit", lambda request, workspace_id: None)


@pytest.mark.django_db
def test_calendar_and_queue_reads_are_workspace_scoped(django_user_model):
    from apps.calendar.models import CustomCalendarEvent, Queue

    user = _user(django_user_model, "calendar-read@example.com")
    workspace = _workspace("Calendar read")
    principal = _oauth_principal(user, workspace)
    post, platform_post = _post_with_target(workspace, user, status="scheduled")
    scheduled_at = timezone.now() + timedelta(days=2)
    platform_post.scheduled_at = scheduled_at
    platform_post.save(update_fields=["scheduled_at", "updated_at"])
    queue = Queue.objects.create(
        workspace=workspace,
        name="Company Queue",
        social_account=platform_post.social_account,
    )
    event = CustomCalendarEvent.objects.create(
        workspace=workspace,
        title="Launch",
        start_date=scheduled_at.date(),
        end_date=scheduled_at.date(),
    )

    calendar = _call(
        principal,
        "get_calendar",
        {
            "workspace_id": str(workspace.id),
            "start_date": (scheduled_at.date() - timedelta(days=1)).isoformat(),
            "end_date": (scheduled_at.date() + timedelta(days=1)).isoformat(),
        },
    )
    queues = _call(principal, "list_queues", {"workspace_id": str(workspace.id)})

    assert calendar["posts"][0]["post_id"] == str(post.id)
    assert calendar["events"][0]["id"] == str(event.id)
    assert queues["queues"][0]["id"] == str(queue.id)


@pytest.mark.django_db
def test_enqueue_preview_does_not_mutate_then_confirmation_schedules_once(django_user_model):
    from apps.calendar.models import PostingSlot, Queue, QueueEntry

    user = _user(django_user_model, "enqueue@example.com")
    workspace = _workspace("Queue mutation")
    principal = _oauth_principal(user, workspace)
    post, platform_post = _post_with_target(workspace, user)
    queue = Queue.objects.create(workspace=workspace, name="Main", social_account=platform_post.social_account)
    tomorrow = timezone.localtime() + timedelta(days=1)
    PostingSlot.objects.create(
        social_account=platform_post.social_account,
        day_of_week=tomorrow.weekday(),
        time=tomorrow.time().replace(second=0, microsecond=0),
    )
    arguments = {
        "workspace_id": str(workspace.id),
        "post_id": str(post.id),
        "queue_id": str(queue.id),
    }

    preview = _call(principal, "enqueue_post", arguments)
    platform_post.refresh_from_db()
    assert preview["confirmation_required"] is True
    assert not QueueEntry.objects.exists()
    assert platform_post.status == "draft"

    confirmed_arguments = {
        **arguments,
        "confirmation_token": preview["confirmation_token"],
        "idempotency_key": "enqueue-once",
    }
    confirmed = _call(principal, "enqueue_post", confirmed_arguments)
    replayed = _call(principal, "enqueue_post", confirmed_arguments)
    platform_post.refresh_from_db()
    assert confirmed["status"] == "scheduled"
    assert confirmed["replayed"] is False
    assert replayed["replayed"] is True
    assert platform_post.status == "scheduled"
    assert QueueEntry.objects.count() == 1


@pytest.mark.django_db
def test_reschedule_and_publish_now_are_confirmation_guarded(django_user_model):
    user = _user(django_user_model, "publish-now@example.com")
    workspace = _workspace("Publish safety")
    principal = _oauth_principal(user, workspace)
    post, platform_post = _post_with_target(workspace, user)
    future = timezone.now() + timedelta(days=3)
    reschedule_args = {
        "workspace_id": str(workspace.id),
        "platform_post_id": str(platform_post.id),
        "scheduled_at": future.isoformat(),
    }

    preview = _call(principal, "reschedule_post", reschedule_args)
    platform_post.refresh_from_db()
    assert platform_post.scheduled_at is None
    confirmed = _call(
        principal,
        "reschedule_post",
        {
            **reschedule_args,
            "confirmation_token": preview["confirmation_token"],
            "idempotency_key": "reschedule-once",
        },
    )
    platform_post.refresh_from_db()
    assert confirmed["status"] == "scheduled"
    assert platform_post.scheduled_at == future

    publish_args = {"workspace_id": str(workspace.id), "post_id": str(post.id)}
    publish_preview = _call(principal, "publish_post", publish_args)
    before_confirm = platform_post.scheduled_at
    platform_post.refresh_from_db()
    assert platform_post.scheduled_at == before_confirm
    published = _call(
        principal,
        "publish_post",
        {
            **publish_args,
            "confirmation_token": publish_preview["confirmation_token"],
            "idempotency_key": "publish-once",
        },
    )
    platform_post.refresh_from_db()
    assert published["status"] == "scheduled"
    assert platform_post.scheduled_at < future


@pytest.mark.django_db
def test_api_key_cannot_enqueue_into_a_non_allowlisted_account_queue(django_user_model):
    from apps.api_keys.models import ApiKey
    from apps.calendar.models import Queue, QueueEntry
    from apps.composer.models import PlatformPost, Post
    from apps.mcp.principal import principal_from_api_key
    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import SocialAccount

    user = _user(django_user_model, "queue-allowlist@example.com")
    workspace = _workspace("Queue allowlist")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    allowed = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="queue-allowed",
        account_name="Allowed",
    )
    blocked = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_company",
        account_platform_id="queue-blocked",
        account_name="Blocked",
    )
    post = Post.objects.create(workspace=workspace, author=user, caption="Scoped")
    PlatformPost.objects.create(post=post, social_account=allowed)
    queue = Queue.objects.create(workspace=workspace, name="Blocked queue", social_account=blocked)
    key = ApiKey.objects.create(
        workspace=workspace,
        issued_by=user,
        name="Queue key",
        lookup_prefix="queuekey",
        token_hash="8" * 64,
        permissions=["create_posts", "publish_directly"],
    )
    key.social_accounts.add(allowed)
    principal = principal_from_api_key(key)
    arguments = {"post_id": str(post.id), "queue_id": str(queue.id)}

    preview = _call(principal, "enqueue_post", arguments)
    denied = _call(
        principal,
        "enqueue_post",
        {
            **arguments,
            "confirmation_token": preview["confirmation_token"],
            "idempotency_key": "blocked-queue",
        },
    )
    assert denied["error"]["code"] == "resource_not_found"
    assert not QueueEntry.objects.exists()


def test_calendar_mutations_advertise_publish_scope_and_confirmation():
    from apps.mcp.registry import all_tools

    tools = {tool.name: tool for tool in all_tools()}
    assert tools["get_calendar"].required_scope == "mcp.read"
    assert tools["list_queues"].required_scope == "mcp.read"
    for name in ("enqueue_post", "reschedule_post", "publish_post"):
        assert tools[name].confirmation_required is True
        assert tools[name].required_scope == "mcp.publish"
        assert tools[name].required_permissions == ("create_posts", "publish_directly")
