from __future__ import annotations

import base64
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event, Lock

import pytest
from django.db import close_old_connections
from django.utils import timezone

from apps.mcp.errors import DomainError


def _context(django_user_model, *, email="confirm@example.com"):
    from apps.api_keys.models import ApiKey
    from apps.mcp.principal import principal_from_api_key
    from apps.members.models import WorkspaceMembership
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    organization = Organization.objects.create(name="Confirmation org")
    workspace = Workspace.objects.create(organization=organization, name="Confirmation workspace")
    user = django_user_model.objects.create_user(email=email, password="test")
    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    api_key = ApiKey.objects.create(
        workspace=workspace,
        issued_by=user,
        name="Confirmation key",
        lookup_prefix=email[:8],
        token_hash="0" * 64,
        permissions=["create_posts", "publish_directly"],
    )
    return principal_from_api_key(api_key), workspace


def _arguments(**overrides):
    values = {
        "post_id": "00000000-0000-0000-0000-000000000001",
        "scheduled_at": "2026-08-17T12:00:00Z",
        "caption": "private campaign copy",
    }
    values.update(overrides)
    return values


@pytest.mark.django_db
def test_preview_is_non_mutating_payload_bound_expiring_and_digest_only(django_user_model):
    from apps.mcp.confirmations import confirmed_action
    from apps.mcp.models import McpConfirmationGrant

    principal, workspace = _context(django_user_model)
    executions = []
    result = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=_arguments(),
        preview={
            "post_id": "00000000-0000-0000-0000-000000000001",
            "scheduled_at": "2026-08-17T12:00:00Z",
            "caption": "private campaign copy",
        },
        execute=lambda: executions.append("executed"),
    )

    assert executions == []
    assert result["confirmation_required"] is True
    assert result["preview"] == {
        "post_id": "00000000-0000-0000-0000-000000000001",
        "scheduled_at": "2026-08-17T12:00:00Z",
    }
    assert len(result["payload_hash"]) == 64
    assert result["expires_at"] > timezone.now().isoformat()
    grant = McpConfirmationGrant.objects.get()
    assert grant.token_digest != result["confirmation_token"]
    assert result["confirmation_token"] not in repr(grant.__dict__)
    assert grant.safe_preview == result["preview"]
    raw_token = result["confirmation_token"]
    decoded = base64.urlsafe_b64decode(raw_token + "=" * (-len(raw_token) % 4))
    assert len(decoded) >= 32


@pytest.mark.django_db
def test_confirmed_action_executes_once_and_replays_safe_result(django_user_model):
    from apps.mcp.confirmations import confirmed_action
    from apps.mcp.models import McpConfirmationGrant, McpIdempotencyRecord

    principal, workspace = _context(django_user_model)
    arguments = _arguments()
    preview = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=arguments,
        preview=arguments,
        execute=lambda: pytest.fail("preview must not execute"),
    )
    executions = []

    def execute():
        executions.append("executed")
        return {
            "post_id": arguments["post_id"],
            "status": "scheduled",
            "caption": "private campaign copy",
        }

    confirmed_arguments = {
        **arguments,
        "confirmation_token": preview["confirmation_token"],
        "idempotency_key": "schedule-once",
    }
    first = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=confirmed_arguments,
        preview=arguments,
        execute=execute,
    )
    replay = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=confirmed_arguments,
        preview=arguments,
        execute=execute,
    )

    assert executions == ["executed"]
    assert first == {"post_id": arguments["post_id"], "status": "scheduled", "replayed": False}
    assert replay == {"post_id": arguments["post_id"], "status": "scheduled", "replayed": True}
    assert McpConfirmationGrant.objects.get().consumed_at is not None
    record = McpIdempotencyRecord.objects.get()
    assert record.status == McpIdempotencyRecord.Status.SUCCEEDED
    assert "private campaign copy" not in repr(record.response_summary)

    with pytest.raises(DomainError) as consumed:
        confirmed_action(
            principal=principal,
            workspace=workspace,
            tool_name="schedule_post",
            arguments={**confirmed_arguments, "idempotency_key": "different-key"},
            preview=arguments,
            execute=lambda: pytest.fail("consumed grant must not execute"),
        )
    assert consumed.value.code == "confirmation_used"


@pytest.mark.django_db
def test_confirmation_is_bound_to_actor_credential_workspace_and_tool(django_user_model):
    from apps.api_keys.models import ApiKey
    from apps.mcp.confirmations import confirmed_action
    from apps.workspaces.models import Workspace

    principal, workspace = _context(django_user_model)
    arguments = _arguments()
    preview = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=arguments,
        preview=arguments,
        execute=lambda: None,
    )
    other_key = ApiKey.objects.create(
        workspace=workspace,
        issued_by=principal.user,
        name="Other credential",
        lookup_prefix="otherkey",
        token_hash="1" * 64,
        permissions=["create_posts", "publish_directly"],
    )
    other_user = django_user_model.objects.create_user(email="other-actor@example.com", password="test")
    other_workspace = Workspace.objects.create(organization=workspace.organization, name="Other workspace")
    attempts = [
        (replace(principal, user=other_user), workspace, "schedule_post"),
        (replace(principal, api_key=other_key), workspace, "schedule_post"),
        (principal, other_workspace, "schedule_post"),
        (principal, workspace, "cancel_post"),
    ]
    for attempted_principal, attempted_workspace, attempted_tool in attempts:
        with pytest.raises(DomainError) as invalid:
            confirmed_action(
                principal=attempted_principal,
                workspace=attempted_workspace,
                tool_name=attempted_tool,
                arguments={
                    **arguments,
                    "confirmation_token": preview["confirmation_token"],
                    "idempotency_key": f"binding-{attempted_tool}-{attempted_workspace.pk}",
                },
                preview=arguments,
                execute=lambda: pytest.fail("wrong binding must not execute"),
            )
        assert invalid.value.code == "invalid_confirmation"


@pytest.mark.django_db
def test_confirmation_rejects_missing_idempotency_expiry_and_payload_mismatch(django_user_model):
    from apps.mcp.confirmations import confirmed_action
    from apps.mcp.models import McpConfirmationGrant

    principal, workspace = _context(django_user_model)
    arguments = _arguments()
    preview = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=arguments,
        preview=arguments,
        execute=lambda: None,
    )

    with pytest.raises(DomainError) as missing:
        confirmed_action(
            principal=principal,
            workspace=workspace,
            tool_name="schedule_post",
            arguments={**arguments, "confirmation_token": preview["confirmation_token"]},
            preview=arguments,
            execute=lambda: None,
        )
    assert missing.value.code == "idempotency_key_required"

    with pytest.raises(DomainError) as mismatch:
        confirmed_action(
            principal=principal,
            workspace=workspace,
            tool_name="schedule_post",
            arguments={
                **arguments,
                "scheduled_at": "2026-08-18T12:00:00Z",
                "confirmation_token": preview["confirmation_token"],
                "idempotency_key": "mismatch",
            },
            preview=arguments,
            execute=lambda: None,
        )
    assert mismatch.value.code == "confirmation_payload_mismatch"

    McpConfirmationGrant.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(DomainError) as expired:
        confirmed_action(
            principal=principal,
            workspace=workspace,
            tool_name="schedule_post",
            arguments={
                **arguments,
                "confirmation_token": preview["confirmation_token"],
                "idempotency_key": "expired",
            },
            preview=arguments,
            execute=lambda: None,
        )
    assert expired.value.code == "confirmation_expired"


@pytest.mark.django_db
def test_failed_execution_releases_claim_and_keeps_grant_reusable(django_user_model):
    from apps.mcp.confirmations import confirmed_action
    from apps.mcp.models import McpConfirmationGrant, McpIdempotencyRecord

    principal, workspace = _context(django_user_model)
    arguments = _arguments()
    preview = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=arguments,
        preview=arguments,
        execute=lambda: None,
    )
    confirmed_arguments = {
        **arguments,
        "confirmation_token": preview["confirmation_token"],
        "idempotency_key": "retry-after-failure",
    }

    with pytest.raises(DomainError, match="temporary"):
        confirmed_action(
            principal=principal,
            workspace=workspace,
            tool_name="schedule_post",
            arguments=confirmed_arguments,
            preview=arguments,
            execute=lambda: (_ for _ in ()).throw(DomainError("provider_unavailable", "temporary", retryable=True)),
        )

    assert McpConfirmationGrant.objects.get().consumed_at is None
    assert not McpIdempotencyRecord.objects.exists()
    recovered = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=confirmed_arguments,
        preview=arguments,
        execute=lambda: {"post_id": arguments["post_id"], "status": "scheduled"},
    )
    assert recovered["status"] == "scheduled"


@pytest.mark.django_db
def test_idempotency_key_cannot_be_reused_for_different_payload(django_user_model):
    from apps.mcp.confirmations import confirmed_action

    principal, workspace = _context(django_user_model)
    first_args = _arguments()
    first_preview = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=first_args,
        preview=first_args,
        execute=lambda: None,
    )
    confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments={
            **first_args,
            "confirmation_token": first_preview["confirmation_token"],
            "idempotency_key": "shared-key",
        },
        preview=first_args,
        execute=lambda: {"post_id": first_args["post_id"], "status": "scheduled"},
    )
    second_args = _arguments(scheduled_at="2026-08-19T12:00:00Z")
    second_preview = confirmed_action(
        principal=principal,
        workspace=workspace,
        tool_name="schedule_post",
        arguments=second_args,
        preview=second_args,
        execute=lambda: None,
    )

    with pytest.raises(DomainError) as conflict:
        confirmed_action(
            principal=principal,
            workspace=workspace,
            tool_name="schedule_post",
            arguments={
                **second_args,
                "confirmation_token": second_preview["confirmation_token"],
                "idempotency_key": "shared-key",
            },
            preview=second_args,
            execute=lambda: pytest.fail("conflicting payload must not execute"),
        )
    assert conflict.value.code == "idempotency_conflict"


@pytest.mark.django_db(transaction=True)
def test_concurrent_distinct_grants_with_same_idempotency_key_execute_once(django_user_model):
    from apps.mcp.confirmations import confirmed_action

    principal, workspace = _context(django_user_model)
    arguments = _arguments()
    previews = [
        confirmed_action(
            principal=principal,
            workspace=workspace,
            tool_name="schedule_post",
            arguments=arguments,
            preview=arguments,
            execute=lambda: None,
        )
        for _ in range(2)
    ]
    first_started = Event()
    execution_lock = Lock()
    execution_count = 0

    def execute():
        nonlocal execution_count
        with execution_lock:
            execution_count += 1
        first_started.set()
        time.sleep(0.2)
        return {"post_id": arguments["post_id"], "status": "scheduled"}

    def confirm(preview):
        try:
            return confirmed_action(
                principal=principal,
                workspace=workspace,
                tool_name="schedule_post",
                arguments={
                    **arguments,
                    "confirmation_token": preview["confirmation_token"],
                    "idempotency_key": "concurrent-shared-key",
                },
                preview=arguments,
                execute=execute,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(confirm, previews[0])
        assert first_started.wait(timeout=5)
        second = pool.submit(confirm, previews[1])
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert execution_count == 1
    assert sorted(result["replayed"] for result in results) == [False, True]
