from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from apps.mcp.errors import DomainError, domain_error_result
from apps.mcp.registry import (
    Tool,
    ToolAnnotations,
    ToolRegistry,
    all_tools,
    get_tool,
    require_tool,
)
from apps.mcp.results import (
    decode_page_cursor,
    encode_page_cursor,
    resource_link,
    success_result,
)

EXPECTED_LEGACY_TOOL_NAMES = {
    "cancel_post",
    "clone_post",
    "convert_idea_to_draft",
    "create_draft",
    "create_idea",
    "finalize_media_upload",
    "get_account_analytics",
    "get_account_health",
    "get_media",
    "get_post",
    "get_post_analytics",
    "get_workspace_context",
    "list_accounts",
    "list_ideas",
    "list_posts",
    "list_workspaces",
    "request_media_upload",
    "schedule_draft",
    "schedule_post",
    "search_media",
    "update_draft",
    "update_idea",
    "upload_media",
}


def _handler(arguments: dict, context: object) -> dict:
    del context
    return success_result({"echo": arguments})


def _tool(name: str = "example", *, enabled: bool = True) -> Tool:
    return Tool(
        name=name,
        description="An example tool.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"echo": {"type": "object"}},
            "required": ["echo"],
        },
        annotations=ToolAnnotations(
            title="Example",
            read_only=True,
            destructive=False,
            idempotent=True,
            open_world=False,
        ),
        handler=_handler,
        enabled=enabled,
    )


def test_existing_tool_names_are_stable_and_metadata_rich():
    tools = all_tools()

    assert {tool.name for tool in tools} == EXPECTED_LEGACY_TOOL_NAMES
    assert all(tool.input_schema.get("type") == "object" for tool in tools)
    assert all(tool.output_schema.get("type") == "object" for tool in tools)
    assert all(isinstance(tool.annotations, ToolAnnotations) for tool in tools)


def test_existing_tools_expose_meaningful_policy_metadata():
    tools = {tool.name: tool for tool in all_tools()}

    assert tools["list_accounts"].annotations.read_only is True
    assert tools["list_accounts"].required_scope == "mcp.read"
    assert tools["create_draft"].required_permission == "create_posts"
    assert tools["create_draft"].required_permissions == ("create_posts",)
    assert tools["schedule_post"].risk_level == "high"
    assert tools["schedule_post"].required_scope == "mcp.publish"
    assert tools["schedule_post"].confirmation_required is True
    assert tools["schedule_post"].required_permissions == ("create_posts", "publish_directly")
    assert tools["schedule_post"].input_schema["properties"]["confirmation_token"]["type"] == "string"
    assert tools["schedule_post"].input_schema["properties"]["idempotency_key"]["maxLength"] == 128
    confirmation_schema = tools["schedule_post"].output_schema["oneOf"][1]
    assert "confirmation_token" in confirmation_schema["required"]
    assert tools["schedule_draft"].required_permissions == ("create_posts", "publish_directly")
    assert tools["cancel_post"].required_permissions == ("create_posts",)


def test_existing_tools_have_shape_specific_output_schemas():
    tools = {tool.name: tool for tool in all_tools()}

    successes = {name: tool.output_schema["oneOf"][0] for name, tool in tools.items()}
    assert "accounts" in successes["list_accounts"]["required"]
    assert successes["list_accounts"]["properties"]["accounts"]["type"] == "array"
    assert {"posts", "limit", "next_cursor"} <= set(successes["list_posts"]["required"])
    assert "items" in successes["search_media"]["required"]
    assert "id" in successes["get_post"]["required"]


def _post_payload() -> dict:
    return {
        "id": "10000000-0000-0000-0000-000000000001",
        "workspace_id": "10000000-0000-0000-0000-000000000002",
        "title": "Launch",
        "caption": "A representative post",
        "first_comment": "",
        "internal_notes": "",
        "scheduled_at": None,
        "published_at": None,
        "proposed_publish_at": None,
        "status": "draft",
        "platform_posts": [
            {
                "id": "10000000-0000-0000-0000-000000000003",
                "social_account_id": "10000000-0000-0000-0000-000000000004",
                "platform": "linkedin",
                "status": "draft",
                "scheduled_at": None,
                "published_at": None,
                "platform_post_id": "",
                "publish_error": "",
            }
        ],
        "created_at": "2026-08-16T09:00:00Z",
        "updated_at": "2026-08-16T09:00:00Z",
    }


def _media_payload() -> dict:
    return {
        "id": "20000000-0000-0000-0000-000000000001",
        "organization_id": "20000000-0000-0000-0000-000000000002",
        "workspace_id": "20000000-0000-0000-0000-000000000003",
        "filename": "launch.png",
        "media_type": "image",
        "mime_type": "image/png",
        "file_size": 1234,
        "file_size_display": "1.2 KB",
        "width": 1200,
        "height": 630,
        "aspect_ratio": 1.9048,
        "duration": 0.0,
        "title": "Launch",
        "alt_text": "Launch graphic",
        "tags": ["launch"],
        "folder_id": None,
        "is_starred": False,
        "is_shared": False,
        "processing_status": "completed",
        "url": "/media/launch.png",
        "thumbnail_url": None,
        "last_used_at": None,
        "created_at": "2026-08-16T09:00:00Z",
        "updated_at": "2026-08-16T09:00:00Z",
    }


def _representative_success_payloads() -> dict[str, dict]:
    post = _post_payload()
    media = _media_payload()
    account = {
        "id": "30000000-0000-0000-0000-000000000001",
        "platform": "linkedin",
        "account_name": "Company",
        "account_handle": "company",
        "connection_status": "connected",
        "char_limit": 3000,
        "escaped_chars": "",
        "needs_title": False,
        "supports_first_comment": True,
    }
    account_analytics = {
        "account_id": account["id"],
        "platform": "linkedin",
        "account_name": "Company",
        "connection_status": "connected",
        "days": 30,
        "analytics_available": False,
        "unavailable_reason": "Analytics unavailable.",
        "hero_metrics": [],
        "engagement": None,
        "follower_growth": None,
        "captured_at": None,
        "next_sync_eta": None,
    }
    post_analytics = {
        "post_id": post["id"],
        "workspace_id": post["workspace_id"],
        "title": post["title"],
        "caption": post["caption"],
        "platform_posts": [
            {
                "platform_post_id": post["platform_posts"][0]["id"],
                "social_account_id": account["id"],
                "platform": "linkedin",
                "status": "draft",
                "published_at": None,
                "analytics_available": True,
                "unavailable_reason": None,
                "metric_tiles": [],
                "captured_at": None,
                "next_sync_eta": None,
            }
        ],
    }
    idea = {
        "id": "50000000-0000-0000-0000-000000000001",
        "workspace_id": post["workspace_id"],
        "title": "Launch idea",
        "description": "A launch concept",
        "tags": ["launch"],
        "status": "unassigned",
        "group_id": None,
        "media_asset_id": None,
        "post_id": None,
        "created_at": "2026-08-16T09:00:00+00:00",
        "updated_at": "2026-08-16T09:00:00+00:00",
    }
    return {
        "list_workspaces": {
            "workspaces": [
                {
                    "id": post["workspace_id"],
                    "name": "Product",
                    "organization_id": "50000000-0000-0000-0000-000000000002",
                    "role": "owner",
                    "timezone": "Europe/London",
                }
            ]
        },
        "get_workspace_context": {
            "id": post["workspace_id"],
            "name": "Product",
            "description": "Product workspace",
            "timezone": "Europe/London",
            "brand_colors": {"primary": "#112233", "secondary": "#445566"},
            "default_hashtags": ["#product"],
            "approval_policy": "none",
            "categories": [],
            "tags": [],
            "templates": [],
        },
        "get_account_health": {
            **account,
            "healthy": True,
            "needs_reconnect": False,
            "issues": [],
            "last_health_check_at": None,
            "reconnect_path": "/accounts/reconnect/",
        },
        "list_ideas": {"ideas": [idea], "limit": 50, "next_cursor": None},
        "create_idea": idea,
        "update_idea": idea,
        "convert_idea_to_draft": post,
        "update_draft": post,
        "clone_post": post,
        "list_accounts": {"accounts": [account]},
        "create_draft": post,
        "schedule_post": post,
        "get_post": post,
        "list_posts": {"posts": [post], "limit": 50, "next_cursor": None},
        "cancel_post": post,
        "schedule_draft": post,
        "search_media": {"items": [media]},
        "get_media": media,
        "upload_media": media,
        "request_media_upload": {
            "upload_id": "40000000-0000-0000-0000-000000000001",
            "method": "POST",
            "url": "https://uploads.example.test",
            "fields": {"key": "media/key"},
            "max_bytes": 1048576,
            "expires_at": "2026-08-16T09:10:00+00:00",
            "instructions": "Upload, then finalize.",
        },
        "finalize_media_upload": media,
        "get_account_analytics": account_analytics,
        "get_post_analytics": post_analytics,
    }


def test_every_tool_output_schema_validates_representative_handler_success_payload():
    tools = {tool.name: tool for tool in all_tools()}
    payloads = _representative_success_payloads()

    assert payloads.keys() == tools.keys()
    for name, payload in payloads.items():
        Draft202012Validator(tools[name].output_schema).validate(payload)


def test_every_tool_output_schema_validates_common_typed_error_payload():
    error_payload = domain_error_result(DomainError("workspace_required", "Choose a workspace."))["structuredContent"]

    for tool in all_tools():
        Draft202012Validator(tool.output_schema).validate(error_payload)


def test_official_sdk_client_validation_accepts_common_typed_error_payload():
    from mcp.client import ClientSession
    from mcp.types import CallToolResult, TextContent

    tool = get_tool("list_accounts")
    assert tool is not None
    session = object.__new__(ClientSession)
    session._tool_output_schemas = {tool.name: tool.output_schema}
    session._tool_output_validators = {}
    error_payload = domain_error_result(DomainError("workspace_required", "Choose a workspace."))["structuredContent"]
    result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(error_payload))],
        structured_content=error_payload,
        is_error=True,
    )

    asyncio.run(session.validate_tool_result(tool.name, result))


def test_nested_success_schemas_reject_under_typed_items():
    tools = {tool.name: tool for tool in all_tools()}

    invalid_payloads = {
        "list_accounts": {"accounts": [{"id": "only-an-id"}]},
        "list_posts": {"posts": [{"id": "only-an-id"}], "limit": 50, "next_cursor": None},
        "search_media": {"items": [{"id": "only-an-id"}]},
        "get_account_analytics": {"account_id": "only-an-id", "analytics_available": False},
        "get_post_analytics": {"post_id": "only-an-id", "platform_posts": []},
    }
    for name, payload in invalid_payloads.items():
        assert list(Draft202012Validator(tools[name].output_schema).iter_errors(payload)), name


def test_list_posts_handler_rejects_decodable_noncanonical_cursor():
    from apps.mcp.handlers import _list_posts
    from apps.mcp.protocol import JsonRpcError

    with pytest.raises(JsonRpcError, match="cursor"):
        _list_posts({"cursor": "e30"}, {"api_key": object()})


def test_wire_metadata_contains_schemas_and_annotations():
    wire = _tool().to_mcp_dict()

    assert wire["inputSchema"]["type"] == "object"
    assert wire["outputSchema"]["required"] == ["echo"]
    assert wire["annotations"] == {
        "title": "Example",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry()
    registry.register(_tool())

    with pytest.raises(ValueError, match="Duplicate MCP tool registered: example"):
        registry.register(_tool())


def test_disabled_tools_are_hidden_but_remain_addressable_for_stale_calls():
    registry = ToolRegistry()
    registry.register(_tool(enabled=False))

    assert registry.discover() == []
    assert registry.get("example") is None
    assert registry.get("example", include_disabled=True) is not None


def test_global_lookup_distinguishes_unknown_from_disabled():
    tool = get_tool("list_accounts", include_disabled=True)

    assert tool is not None
    assert tool.name == "list_accounts"


def test_require_tool_returns_safe_typed_disabled_error():
    registry = ToolRegistry()
    registry.register(_tool(enabled=False))

    with pytest.raises(DomainError) as exc_info:
        require_tool("example", registry=registry)

    assert exc_info.value.code == "tool_disabled"
    assert exc_info.value.message == "This tool is currently disabled."


def test_require_tool_returns_safe_typed_unknown_error():
    with pytest.raises(DomainError) as exc_info:
        require_tool("missing", registry=ToolRegistry())

    assert exc_info.value.code == "unknown_tool"
    assert exc_info.value.message == "Unknown tool."


def test_sdk_discovery_uses_canonical_output_schema_and_annotations():
    from apps.mcp.server import _list_tools_sync

    access_token = SimpleNamespace(
        principal=SimpleNamespace(
            credential_kind="oauth",
            granted_scopes=frozenset({"mcp"}),
            api_key_permissions=frozenset(),
        )
    )
    sdk_tool = next(tool for tool in _list_tools_sync(access_token) if tool.name == "list_accounts")
    payload = sdk_tool.model_dump(by_alias=True, exclude_none=True)

    assert payload["outputSchema"]["type"] == "object"
    assert payload["annotations"]["readOnlyHint"] is True


def test_sdk_discovery_enforces_granular_oauth_scopes():
    from apps.mcp.server import _list_tools_sync

    access_token = SimpleNamespace(
        principal=SimpleNamespace(
            credential_kind="oauth",
            granted_scopes=frozenset({"mcp.read"}),
            api_key_permissions=frozenset(),
        ),
        django_request=object(),
    )
    discovered = {tool.name for tool in _list_tools_sync(access_token)}

    assert "list_accounts" in discovered
    assert "schedule_post" not in discovered


def test_legacy_batch_endpoint_is_isolated_in_legacy_module():
    from apps.mcp.legacy import mcp_endpoint

    assert mcp_endpoint.__module__ == "apps.mcp.legacy"


def test_success_result_has_structured_content_and_json_text_fallback():
    result = success_result({"items": [{"id": "one"}], "next_cursor": None})

    assert result["structuredContent"] == {"items": [{"id": "one"}], "next_cursor": None}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["isError"] is False


def test_success_result_can_include_resource_links():
    link = resource_link(
        "brightbean://workspaces/ws-1/posts/post-1",
        name="Post post-1",
        description="Open the authorized post resource.",
        mime_type="application/json",
    )

    result = success_result({"id": "post-1"}, resource_links=[link])

    assert result["content"][1] == {
        "type": "resource_link",
        "uri": "brightbean://workspaces/ws-1/posts/post-1",
        "name": "Post post-1",
        "description": "Open the authorized post resource.",
        "mimeType": "application/json",
    }


@pytest.mark.parametrize("offset", [0, 1, 100, 2**31])
def test_page_cursor_round_trips_non_negative_offsets(offset):
    assert decode_page_cursor(encode_page_cursor(offset)) == offset


@pytest.mark.parametrize("cursor", ["not-base64", "e30", "eyJvIjogLTF9", 123])
def test_page_cursor_rejects_malformed_values(cursor):
    with pytest.raises(ValueError, match="Invalid cursor"):
        decode_page_cursor(cursor)  # type: ignore[arg-type]


def test_domain_error_result_is_machine_readable_and_does_not_leak_cause():
    error = DomainError(
        code="resource_not_found",
        message="Resource not found.",
        details={"resource_type": "post"},
        retryable=False,
    )
    error.__cause__ = RuntimeError("postgres password=do-not-leak")

    result = domain_error_result(error)

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "error": {
            "code": "resource_not_found",
            "message": "Resource not found.",
            "details": {"resource_type": "post"},
            "retryable": False,
        }
    }
    serialized = json.dumps(result)
    assert "do-not-leak" not in serialized
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_domain_error_rejects_non_json_serializable_details():
    with pytest.raises(TypeError, match="JSON-serializable"):
        DomainError("invalid_request", "Invalid request.", details={"bad": object()})


def test_unexpected_legacy_exception_does_not_leak_internal_message():
    from apps.mcp.protocol import INTERNAL_ERROR, dispatch

    def explode(params, context):
        del params, context
        raise RuntimeError("postgres password=do-not-leak")

    response = dispatch(
        {"jsonrpc": "2.0", "id": 7, "method": "explode"},
        {},
        {"explode": explode},
    )

    assert response is not None
    assert response["error"] == {"code": INTERNAL_ERROR, "message": "Internal server error."}
    assert "do-not-leak" not in json.dumps(response)


def test_legacy_stale_call_returns_typed_tool_disabled_result(monkeypatch):
    from apps.mcp import legacy

    monkeypatch.setattr(legacy, "get_tool", lambda name, **kwargs: _tool(name, enabled=False))

    result = legacy._tools_call({"name": "example", "arguments": {}}, {})

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "tool_disabled"


def test_legacy_domain_error_is_returned_as_safe_tool_result(monkeypatch):
    from apps.mcp import legacy

    def fail(arguments, context):
        del arguments, context
        raise DomainError("workspace_required", "Choose a workspace.", details={"workspace_ids": ["ws-1"]})

    monkeypatch.setattr(
        legacy,
        "get_tool",
        lambda name, **kwargs: Tool(
            name=name,
            description="Failure test.",
            input_schema={"type": "object"},
            handler=fail,
        ),
    )

    context = {
        "principal": SimpleNamespace(
            credential_kind="oauth",
            granted_scopes=frozenset({"mcp"}),
            api_key_permissions=frozenset(),
        )
    }
    result = legacy._tools_call({"name": "example", "arguments": {}}, context)

    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": "workspace_required",
        "message": "Choose a workspace.",
        "details": {"workspace_ids": ["ws-1"]},
        "retryable": False,
    }
