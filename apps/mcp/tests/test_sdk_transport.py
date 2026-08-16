"""Official MCP SDK v2 ASGI transport integration tests.

These tests exercise the outer ASGI application directly.  The existing
``test_transport`` module remains the compatibility contract for the legacy
Django/Ninja backend.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.core.asgi import get_asgi_application
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from starlette.testclient import TestClient

from apps.api_keys import services
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership

MCP_PATH = "/api/v1/mcp"
MCP_PATH_SLASH = f"{MCP_PATH}/"
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


def _rpc(method: str, params: dict | None = None, *, request_id: int = 1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _initialize(*, request_id: int = 1):
    return _rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "brightbean-tests", "version": "1.0"},
        },
        request_id=request_id,
    )


def _discover_native(*, request_id: int = 1):
    return _rpc(
        "server/discover",
        {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "brightbean-native-tests",
                    "version": "1.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
        request_id=request_id,
    )


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="sdk-mcp-owner@example.com",
        password="testpass123",
        name="SDK MCP Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="SDK MCP Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="SDK MCP Workspace", organization=organization)


@pytest.fixture
def membership(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


@pytest.fixture
def social_account(db, workspace):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-sdk-mcp",
        account_name="LinkedIn SDK MCP",
        connection_status="connected",
    )


@pytest.fixture
def issued_key(db, user, membership, workspace, social_account):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[social_account],
        issued_by=user,
        name="sdk-mcp",
        permissions=list(PERMISSION_KEYS),
    )


def _sdk_app():
    from apps.mcp.routing import create_application

    return create_application(
        django_application=get_asgi_application(),
        enabled=True,
        backend="sdk_v2",
        public_base_url="https://testserver",
        oauth_issuer_url="https://testserver",
    )


def _legacy_app():
    from apps.mcp.routing import create_application

    return create_application(
        django_application=get_asgi_application(),
        enabled=True,
        backend="legacy",
        public_base_url="https://testserver",
        oauth_issuer_url="https://testserver",
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {**MCP_HEADERS, "authorization": f"Bearer {token}"}


def _test_client(application, **kwargs) -> TestClient:
    return TestClient(application, client=("127.0.0.1", 50000), **kwargs)


def _post_asgi_chunks(application, *, token: str, chunks: list[bytes], content_length: int | None = None):
    """Invoke the ASGI app with exact request chunks for body-boundary tests."""

    async def invoke():
        headers = [
            (b"host", b"testserver"),
            (b"accept", MCP_HEADERS["accept"].encode()),
            (b"content-type", MCP_HEADERS["content-type"].encode()),
            (b"authorization", f"Bearer {token}".encode()),
        ]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": MCP_PATH,
            "raw_path": MCP_PATH.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 443),
        }
        messages = [
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            for index, chunk in enumerate(chunks)
        ]
        sent = []

        async def receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await application(scope, receive, send)
        return sent

    messages = asyncio.run(invoke())
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], body


@pytest.mark.django_db(transaction=True)
class TestSdkTransport:
    def test_request_rate_bucket_uses_registered_tool_annotations(self):
        from apps.mcp.server import _request_is_write

        assert _request_is_write(json.dumps(_rpc("tools/list")).encode()) is False
        assert (
            _request_is_write(json.dumps(_rpc("tools/call", {"name": "list_accounts", "arguments": {}})).encode())
            is False
        )
        assert (
            _request_is_write(json.dumps(_rpc("tools/call", {"name": "create_draft", "arguments": {}})).encode())
            is True
        )
        assert _request_is_write(b"not-json") is True

    def test_both_exact_paths_initialize_without_redirect(self, issued_key):
        with _test_client(_sdk_app(), base_url="https://testserver", follow_redirects=False) as client:
            for request_id, path in enumerate((MCP_PATH, MCP_PATH_SLASH), start=1):
                response = client.post(
                    path,
                    headers=_auth_headers(issued_key.plaintext_token),
                    json=_initialize(request_id=request_id),
                )
                assert response.status_code == 200, response.text
                assert response.history == []
                assert response.json()["result"]["serverInfo"]["name"] == "brightbean-studio"

    def test_native_server_discovery_advertises_current_protocol_and_is_safely_audited(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers={
                    **_auth_headers(issued_key.plaintext_token),
                    "mcp-method": "server/discover",
                    "mcp-protocol-version": "2026-07-28",
                },
                json=_discover_native(),
            )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert "result" in payload, response.text
        assert "2026-07-28" in payload["result"]["supportedVersions"]
        assert ApiKeyAuditLog.objects.filter(
            api_key=issued_key.api_key,
            action="mcp.server/discover",
            status_code=200,
        ).exists()

    def test_ping_uses_stateless_json_response(self, issued_key):
        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc("ping"),
            )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["result"] == {}
        assert "mcp-session-id" not in response.headers

    def test_initialize_and_ping_are_each_rate_limited_and_audited_once(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        with (
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            _test_client(_sdk_app(), base_url="https://testserver") as client,
        ):
            initialized = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_initialize(),
            )
            pinged = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc("ping", request_id=2),
            )

        assert initialized.status_code == 200
        assert pinged.status_code == 200
        assert enforce_rate_limits.call_count == 2
        assert ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key, action="mcp.initialize").count() == 1
        assert ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key, action="mcp.ping").count() == 1

    def test_failed_tool_call_records_redacted_activity_in_finally_path(self, issued_key):
        from apps.mcp.models import McpActivityEvent
        from apps.mcp.protocol import INVALID_PARAMS

        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc(
                    "tools/call",
                    {
                        "name": "create_draft",
                        "arguments": {"caption": "private campaign content"},
                    },
                ),
            )

        assert response.status_code == 200
        assert response.json()["error"]["code"] == INVALID_PARAMS
        event = McpActivityEvent.objects.get(api_key=issued_key.api_key, primitive="tool", name="create_draft")
        assert event.status == McpActivityEvent.Status.FAILED
        assert event.duration_ms >= 0
        assert "private campaign content" not in repr(event.summary)

    def test_missing_bearer_returns_oauth_challenge_on_both_paths(self):
        with _test_client(_sdk_app(), base_url="https://testserver", follow_redirects=False) as client:
            for path in (MCP_PATH, MCP_PATH_SLASH):
                response = client.post(path, headers=MCP_HEADERS, json=_initialize())
                assert response.status_code == 401
                assert response.history == []
                challenge = response.headers.get("www-authenticate", "")
                assert "resource_metadata=" in challenge
                assert "/.well-known/oauth-protected-resource/api/v1/mcp" in challenge

    def test_tools_list_and_list_accounts_use_existing_registry_and_allowlist(self, issued_key, social_account):
        from apps.api_keys.models import ApiKeyAuditLog

        headers = _auth_headers(issued_key.plaintext_token)
        with (
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            _test_client(_sdk_app(), base_url="https://testserver") as client,
        ):
            listed = client.post(MCP_PATH, headers=headers, json=_rpc("tools/list"))
            called = client.post(
                MCP_PATH,
                headers=headers,
                json=_rpc("tools/call", {"name": "list_accounts", "arguments": {}}, request_id=2),
            )

        assert listed.status_code == 200, listed.text
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert "list_accounts" in names
        assert all(tool["inputSchema"]["type"] == "object" for tool in listed.json()["result"]["tools"])
        assert all(tool["outputSchema"]["type"] == "object" for tool in listed.json()["result"]["tools"])
        list_accounts = next(tool for tool in listed.json()["result"]["tools"] if tool["name"] == "list_accounts")
        assert list_accounts["annotations"]["readOnlyHint"] is True

        assert called.status_code == 200, called.text
        result = called.json()["result"]
        payload = json.loads(result["content"][0]["text"])
        assert result["structuredContent"] == payload
        assert result["isError"] is False
        assert {account["id"] for account in payload["accounts"]} == {str(social_account.id)}
        assert enforce_rate_limits.call_count == 2
        assert all(
            call.kwargs == {"is_write": False, "include_workspace": False}
            for call in enforce_rate_limits.call_args_list
        )
        assert ApiKeyAuditLog.objects.filter(
            api_key=issued_key.api_key,
            action="mcp.tools/call:list_accounts",
            status_code=200,
        ).exists()

    def test_official_sdk_accepts_typed_error_against_advertised_output_schema(self, issued_key):
        from apps.mcp.registry import get_tool

        enabled_tool = get_tool("list_accounts")
        assert enabled_tool is not None
        disabled_tool = replace(enabled_tool, enabled=False)
        with (
            patch("apps.mcp.server.get_tool", return_value=disabled_tool),
            _test_client(_sdk_app(), base_url="https://testserver") as client,
        ):
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc("tools/call", {"name": "list_accounts", "arguments": {}}),
            )

        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["error"]["code"] == "tool_disabled"

    def test_real_rate_limit_returns_structured_http_429_once(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        issued_key.api_key.rate_override_reads = 0
        issued_key.api_key.save(update_fields=["rate_override_reads"])
        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_initialize(),
            )

        assert response.status_code == 429
        assert response.json() == {
            "error": "rate_limited",
            "tier": "per_key_reads",
            "limit": 0,
            "remaining": 0,
            "retry_after": 60,
        }
        assert response.headers["retry-after"] == "60"
        assert response.headers["x-ratelimit-limit"] == "0"
        assert response.headers["x-ratelimit-remaining"] == "0"
        assert (
            ApiKeyAuditLog.objects.filter(
                api_key=issued_key.api_key,
                action="mcp.initialize",
                status_code=429,
            ).count()
            == 1
        )

    def test_malformed_and_batch_requests_are_rate_limited_and_safely_audited(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        with (
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            _test_client(_sdk_app(), base_url="https://testserver") as client,
        ):
            malformed = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                content=b'{"jsonrpc":',
            )
            batch = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=[_rpc("ping", request_id=1), _rpc("ping", request_id=2)],
            )

        assert malformed.status_code == 400
        assert batch.status_code == 400
        assert enforce_rate_limits.call_count == 2
        assert (
            ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key, action="mcp.unknown", status_code=400).count()
            == 1
        )
        assert (
            ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key, action="mcp.batch", status_code=400).count() == 1
        )

    def test_invalid_tool_arguments_return_invalid_params_and_audit_422(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc(
                    "tools/call",
                    {"name": "create_draft", "arguments": {"social_account_id": 42, "caption": "ok"}},
                ),
            )

        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32602
        assert (
            ApiKeyAuditLog.objects.filter(
                api_key=issued_key.api_key,
                action="mcp.tools/call:create_draft",
                status_code=422,
            ).count()
            == 1
        )

    def test_unknown_tool_is_audited_without_persisting_its_name(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc("tools/call", {"name": "does_not_exist", "arguments": {}}),
            )

        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32602
        assert (
            ApiKeyAuditLog.objects.filter(
                api_key=issued_key.api_key,
                action="mcp.tools/call:unknown",
                status_code=422,
            ).count()
            == 1
        )

    def test_sdk_rejects_json_rpc_batches(self, issued_key):
        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=[_rpc("ping", request_id=1), _rpc("ping", request_id=2)],
            )
        assert response.status_code == 400
        assert not isinstance(response.json(), list)

    def test_transport_security_rejects_noncanonical_host_before_any_side_effect(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        cache_key = "agent_api:auth_fail:127.0.0.1"
        cache.delete(cache_key)
        with (
            patch("apps.mcp.server.McpAuth.authenticate") as authenticate,
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            patch("apps.mcp.server.log_audit_entry") as audit,
            _test_client(_sdk_app(), base_url="https://untrusted.example") as client,
        ):
            response = client.post(MCP_PATH, headers=_auth_headers(issued_key.plaintext_token), json=_initialize())

        assert response.status_code == 421
        authenticate.assert_not_called()
        enforce_rate_limits.assert_not_called()
        audit.assert_not_called()
        issued_key.api_key.refresh_from_db()
        assert issued_key.api_key.last_used_at is None
        assert cache.get(cache_key) is None
        assert not ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key).exists()

    def test_transport_security_rejects_noncanonical_origin_before_any_side_effect(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        cache_key = "agent_api:auth_fail:127.0.0.1"
        cache.delete(cache_key)
        headers = {**_auth_headers(issued_key.plaintext_token), "origin": "https://untrusted.example"}
        with (
            patch("apps.mcp.server.McpAuth.authenticate") as authenticate,
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            patch("apps.mcp.server.log_audit_entry") as audit,
            _test_client(_sdk_app(), base_url="https://testserver") as client,
        ):
            response = client.post(MCP_PATH, headers=headers, json=_initialize())

        assert response.status_code == 403
        authenticate.assert_not_called()
        enforce_rate_limits.assert_not_called()
        audit.assert_not_called()
        issued_key.api_key.refresh_from_db()
        assert issued_key.api_key.last_used_at is None
        assert cache.get(cache_key) is None
        assert not ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key).exists()

    def test_transport_security_rejects_invalid_content_type_before_any_side_effect(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        cache_key = "agent_api:auth_fail:127.0.0.1"
        cache.delete(cache_key)
        headers = {**_auth_headers(issued_key.plaintext_token), "content-type": "text/plain"}
        with (
            patch("apps.mcp.server.McpAuth.authenticate") as authenticate,
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            patch("apps.mcp.server.log_audit_entry") as audit,
            _test_client(_sdk_app(), base_url="https://testserver") as client,
        ):
            response = client.post(
                MCP_PATH,
                headers=headers,
                content=json.dumps(_initialize()).encode(),
            )

        assert response.status_code == 400
        authenticate.assert_not_called()
        enforce_rate_limits.assert_not_called()
        audit.assert_not_called()
        issued_key.api_key.refresh_from_db()
        assert issued_key.api_key.last_used_at is None
        assert cache.get(cache_key) is None
        assert not ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key).exists()

    def test_chunked_oversize_body_is_rejected_before_any_side_effect(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog
        from apps.mcp.server import MAX_REQUEST_BODY_SIZE

        chunks = [b"x" * (MAX_REQUEST_BODY_SIZE // 2), b"y" * (MAX_REQUEST_BODY_SIZE // 2), b"z"]
        with (
            patch("apps.mcp.server.McpAuth.authenticate") as authenticate,
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            patch("apps.mcp.server.log_audit_entry") as audit,
        ):
            status, _body = _post_asgi_chunks(
                _sdk_app(),
                token=issued_key.plaintext_token,
                chunks=chunks,
            )

        assert status == 413
        authenticate.assert_not_called()
        enforce_rate_limits.assert_not_called()
        audit.assert_not_called()
        issued_key.api_key.refresh_from_db()
        assert issued_key.api_key.last_used_at is None
        assert not ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key).exists()

    def test_declared_oversize_body_is_rejected_without_reading_or_side_effects(self, issued_key):
        from apps.mcp.server import MAX_REQUEST_BODY_SIZE

        with (
            patch("apps.mcp.server.McpAuth.authenticate") as authenticate,
            patch("apps.mcp.server.enforce_http_rate_limits") as enforce_rate_limits,
            patch("apps.mcp.server.log_audit_entry") as audit,
        ):
            status, _body = _post_asgi_chunks(
                _sdk_app(),
                token=issued_key.plaintext_token,
                chunks=[],
                content_length=MAX_REQUEST_BODY_SIZE + 1,
            )

        assert status == 413
        authenticate.assert_not_called()
        enforce_rate_limits.assert_not_called()
        audit.assert_not_called()

    def test_audit_actions_never_include_untrusted_method_or_tool_names(self, issued_key):
        from apps.api_keys.models import ApiKeyAuditLog

        sensitive = "secret-token-caption"
        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            unknown_method = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc(sensitive),
            )
            unknown_tool = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_rpc("tools/call", {"name": sensitive, "arguments": {}}, request_id=2),
            )

        assert unknown_method.status_code == 200
        assert unknown_tool.status_code == 200
        assert ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key, action="mcp.unknown").exists()
        assert ApiKeyAuditLog.objects.filter(
            api_key=issued_key.api_key,
            action="mcp.tools/call:unknown",
        ).exists()
        assert not ApiKeyAuditLog.objects.filter(api_key=issued_key.api_key, action__contains=sensitive).exists()

    @override_settings(DEBUG=False)
    def test_plaintext_valid_bearer_is_rejected_and_counted_once(self, issued_key):
        cache.delete("agent_api:auth_fail:127.0.0.1")
        with _test_client(_sdk_app(), base_url="http://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_initialize(),
            )

        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        assert cache.get("agent_api:auth_fail:127.0.0.1") == 1

    def test_invalid_bearers_trip_failed_auth_throttle_without_double_counting(self, issued_key):
        cache.delete("agent_api:auth_fail:127.0.0.1")
        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            for attempt in range(10):
                response = client.post(
                    MCP_PATH,
                    headers=_auth_headers(f"invalid-{attempt}"),
                    json=_initialize(request_id=attempt + 1),
                )
                assert response.status_code == 401
            assert cache.get("agent_api:auth_fail:127.0.0.1") == 10

            blocked = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=_initialize(request_id=20),
            )

        assert blocked.status_code == 401
        assert cache.get("agent_api:auth_fail:127.0.0.1") == 10


class TestSdkRequestBoundary:
    def test_non_post_request_has_an_empty_audit_body_without_buffering(self):
        from apps.mcp.server import _request_body

        assert _request_body({"type": "http", "method": "GET"}) == b""

    @pytest.mark.parametrize(
        ("value", "allowed", "required", "expected"),
        [
            ("example.test", ["example.test"], True, True),
            ("example.test:8443", ["example.test:*"], True, True),
            ("https://example.test:8443", ["https://example.test:*"], False, True),
            (None, ["example.test"], True, False),
            (None, ["https://example.test"], False, True),
            ("untrusted.test", ["example.test", "example.test:*"], True, False),
        ],
    )
    def test_transport_allowlist_matches_official_exact_and_wildcard_port_semantics(
        self,
        value,
        allowed,
        required,
        expected,
    ):
        from apps.mcp.server import _matches_allowed

        assert _matches_allowed(value, allowed, required=required) is expected

    def test_bounded_body_is_buffered_once_and_replayed_from_scope(self):
        from apps.mcp.server import REQUEST_BODY_SCOPE_KEY, BoundedRequestBodyMiddleware

        upstream_reads = 0
        observed_body = None

        async def downstream(scope, receive, send):
            nonlocal observed_body
            observed_body = scope[REQUEST_BODY_SCOPE_KEY]
            first = await receive()
            assert first == {"type": "http.request", "body": b"one-two", "more_body": False}
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def invoke():
            nonlocal upstream_reads
            messages = [
                {"type": "http.request", "body": b"one-", "more_body": True},
                {"type": "http.request", "body": b"two", "more_body": False},
            ]
            scope = {"type": "http", "method": "POST", "headers": []}

            async def receive():
                nonlocal upstream_reads
                upstream_reads += 1
                return messages.pop(0) if messages else {"type": "http.disconnect"}

            async def send(message):
                del message

            await BoundedRequestBodyMiddleware(downstream, max_body_size=32)(scope, receive, send)

        asyncio.run(invoke())

        assert observed_body == b"one-two"
        assert upstream_reads == 2

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_requests_use_isolated_thread_sensitive_executors_and_close_connections(self):
        from django.db import connection, connections

        from apps.mcp.server import REQUEST_BODY_SCOPE_KEY, RequestBoundaryMiddleware

        barrier = threading.Barrier(2, timeout=2)
        auth_threads: dict[str, int] = {}
        database_connections = {}
        close_threads: list[int] = []
        close_all = connections.close_all

        def authenticate(token, scope, body, resource_url):
            del scope, body, resource_url
            auth_threads[token] = threading.get_ident()
            connection.ensure_connection()
            database_connections[token] = connection.connection
            barrier.wait()
            return SimpleNamespace(token=token, django_request=object())

        def close_connections():
            close_threads.append(threading.get_ident())
            close_all()

        async def downstream(scope, receive, send):
            del scope
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def invoke(token: str):
            body = json.dumps(_rpc("ping")).encode()
            scope = {
                "type": "http",
                "method": "POST",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                REQUEST_BODY_SCOPE_KEY: body,
            }
            received = False

            async def receive():
                nonlocal received
                if received:
                    return {"type": "http.disconnect"}
                received = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                del message

            await RequestBoundaryMiddleware(downstream, resource_url="https://testserver/api/v1/mcp")(
                scope,
                receive,
                send,
            )

        async def run_concurrently():
            with (
                patch("apps.mcp.server._authenticate_sync", side_effect=authenticate),
                patch("apps.mcp.server.enforce_http_rate_limits"),
                patch("apps.mcp.server._audit", new=AsyncMock()),
                patch("apps.mcp.server.connections.close_all", side_effect=close_connections),
            ):
                await asyncio.gather(invoke("token-a"), invoke("token-b"))

        asyncio.run(run_concurrently())

        assert len(set(auth_threads.values())) == 2
        assert sorted(close_threads) == sorted(auth_threads.values())
        assert len({id(db_connection) for db_connection in database_connections.values()}) == 2
        assert all(db_connection.closed for db_connection in database_connections.values())

    def test_authentication_exception_still_closes_request_connections(self):
        from apps.mcp.server import REQUEST_BODY_SCOPE_KEY, RequestBoundaryMiddleware

        body = json.dumps(_rpc("ping")).encode()
        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer broken-token")],
            REQUEST_BODY_SCOPE_KEY: body,
        }

        async def downstream(scope, receive, send):
            raise AssertionError("Authentication failure must not reach the SDK application.")

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            del message

        with (
            patch("apps.mcp.server._authenticate_sync", side_effect=RuntimeError("auth failed")),
            patch("apps.mcp.server.connections.close_all") as close_all,
            pytest.raises(RuntimeError, match="auth failed"),
        ):
            asyncio.run(
                RequestBoundaryMiddleware(downstream, resource_url="https://testserver/api/v1/mcp")(
                    scope,
                    receive,
                    send,
                )
            )

        close_all.assert_called_once_with()

    def test_audit_response_capture_stops_at_64_kib_and_still_audits(self):
        from apps.mcp.server import MAX_AUDIT_RESPONSE_SIZE, REQUEST_BODY_SCOPE_KEY, RequestBoundaryMiddleware

        access_token = SimpleNamespace(token="token", django_request=object())
        audit = AsyncMock()

        async def downstream(scope, receive, send):
            del scope
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": b"x" * MAX_AUDIT_RESPONSE_SIZE,
                    "more_body": True,
                }
            )
            await send({"type": "http.response.body", "body": b"y", "more_body": False})

        async def invoke():
            body = json.dumps(_rpc("ping")).encode()
            scope = {
                "type": "http",
                "method": "POST",
                "headers": [(b"authorization", b"Bearer token")],
                REQUEST_BODY_SCOPE_KEY: body,
            }
            messages = [{"type": "http.request", "body": body, "more_body": False}]

            async def receive():
                return messages.pop(0) if messages else {"type": "http.disconnect"}

            async def send(message):
                del message

            with (
                patch("apps.mcp.server._authenticate_sync", return_value=access_token),
                patch("apps.mcp.server.enforce_http_rate_limits"),
                patch("apps.mcp.server._audit", new=audit),
                patch("apps.mcp.server._synthetic_status", return_value=200) as synthetic_status,
                patch("apps.mcp.server.connections.close_all"),
            ):
                await RequestBoundaryMiddleware(downstream, resource_url="https://testserver/api/v1/mcp")(
                    scope,
                    receive,
                    send,
                )
            return synthetic_status

        synthetic_status = asyncio.run(invoke())

        synthetic_status.assert_called_once_with(200, None)
        audit.assert_awaited_once()


@pytest.mark.django_db(transaction=True)
class TestSdkOAuthBridge:
    def test_oauth_token_can_initialize(self, user, membership):
        from oauth2_provider.models import get_access_token_model, get_application_model

        application_model = get_application_model()
        application = application_model.objects.create(
            name="SDK test client",
            client_type=application_model.CLIENT_PUBLIC,
            authorization_grant_type=application_model.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://client.example/callback",
        )
        raw_token = f"sdk-oauth-{user.pk}"
        from apps.oauth_server.resources import canonical_mcp_resource_uri
        from apps.oauth_server.services import bind_access_token

        access_token = get_access_token_model().objects.create(
            user=user,
            application=application,
            token=raw_token,
            scope="mcp",
            resource=[canonical_mcp_resource_uri()],
            expires=timezone.now() + timedelta(hours=1),
        )
        bind_access_token(access_token, raw_token)
        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(MCP_PATH, headers=_auth_headers(raw_token), json=_initialize())

        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "brightbean-studio"

    def test_oauth_token_can_call_tool(self, user, membership, social_account):
        from oauth2_provider.models import get_access_token_model, get_application_model

        from apps.api_keys.models import ApiKeyAuditLog

        application_model = get_application_model()
        user.last_workspace_id = membership.workspace_id
        user.save(update_fields=["last_workspace_id"])
        application = application_model.objects.create(
            name="SDK tool client",
            client_type=application_model.CLIENT_PUBLIC,
            authorization_grant_type=application_model.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://client.example/callback",
        )
        raw_token = f"sdk-oauth-tool-{user.pk}"
        from apps.oauth_server.resources import canonical_mcp_resource_uri
        from apps.oauth_server.services import bind_access_token

        access_token = get_access_token_model().objects.create(
            user=user,
            application=application,
            token=raw_token,
            scope="mcp",
            resource=[canonical_mcp_resource_uri()],
            expires=timezone.now() + timedelta(hours=1),
        )
        bind_access_token(access_token, raw_token)
        from apps.api.auth import _resolve_oauth_actor

        principal = _resolve_oauth_actor(raw_token)
        assert principal is not None
        assert principal.workspace_id is None
        assert membership.workspace_id in {item.workspace.id for item in principal.authorized_workspaces}

        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(raw_token),
                json=_rpc(
                    "tools/call",
                    {"name": "list_accounts", "arguments": {"workspace_id": str(membership.workspace_id)}},
                ),
            )

        assert response.status_code == 200, response.text
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        assert {account["id"] for account in payload["accounts"]} == {str(social_account.id)}
        assert (
            ApiKeyAuditLog.objects.filter(
                actor_user=user,
                actor_label="oauth",
                action="mcp.tools/call:list_accounts",
                status_code=200,
            ).count()
            == 1
        )


@pytest.mark.django_db(transaction=True)
class TestRoutingControls:
    def test_legacy_backend_keeps_batching(self, issued_key):
        with _test_client(_legacy_app(), base_url="https://testserver") as client:
            response = client.post(
                MCP_PATH,
                headers=_auth_headers(issued_key.plaintext_token),
                json=[_rpc("ping", request_id=1), _rpc("ping", request_id=2)],
            )
        assert response.status_code == 200, response.text
        assert {item["id"] for item in response.json()} == {1, 2}

    def test_disabled_server_blocks_both_paths_but_preserves_django(self):
        from apps.mcp.routing import create_application

        application = create_application(
            django_application=get_asgi_application(),
            enabled=False,
            backend="sdk_v2",
            public_base_url="https://testserver",
            oauth_issuer_url="https://testserver",
        )
        with _test_client(application, base_url="https://testserver", follow_redirects=False) as client:
            for path in (MCP_PATH, MCP_PATH_SLASH):
                response = client.post(path, headers=MCP_HEADERS, json=_initialize())
                assert response.status_code == 404
                assert response.history == []
            assert client.get("/health/").status_code == 200

    def test_sdk_backend_preserves_non_mcp_path_and_query_string(self):
        with _test_client(_sdk_app(), base_url="https://testserver") as client:
            response = client.get("/health/?source=mcp-test")
        assert response.status_code == 200

    def test_optional_staging_alias_uses_same_sdk_transport(self, issued_key):
        from apps.mcp.routing import create_application

        application = create_application(
            django_application=get_asgi_application(),
            enabled=True,
            backend="sdk_v2",
            public_base_url="https://testserver",
            oauth_issuer_url="https://testserver",
            staging_alias="/api/v1/mcp-next",
        )
        with _test_client(application, base_url="https://testserver") as client:
            response = client.post(
                "/api/v1/mcp-next",
                headers=_auth_headers(issued_key.plaintext_token),
                json=_initialize(),
            )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "brightbean-studio"

    def test_unknown_backend_is_rejected_at_startup(self):
        from django.core.exceptions import ImproperlyConfigured

        from apps.mcp.routing import create_application

        with pytest.raises(ImproperlyConfigured, match="MCP_TRANSPORT_BACKEND"):
            create_application(
                django_application=get_asgi_application(),
                enabled=True,
                backend="unknown",
                public_base_url="https://testserver",
                oauth_issuer_url="https://testserver",
            )
