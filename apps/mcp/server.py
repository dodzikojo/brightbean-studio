"""Official MCP SDK v2 adapter for BrightBean's existing tool surface."""

from __future__ import annotations

import json
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from io import BytesIO
from time import perf_counter
from typing import Any

import mcp.types as types
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.core.handlers.asgi import ASGIRequest
from django.db import connections
from mcp.server import Server, ServerRequestContext
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from ninja.errors import HttpError
from pydantic import AnyHttpUrl, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.responses import Response

from apps.api.auth import McpAuth
from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.mcp.errors import DomainError, domain_error_result, tool_disabled_error
from apps.mcp.protocol import INVALID_PARAMS, SERVER_NAME, SERVER_VERSION, JsonRpcError
from apps.mcp.registry import all_tools, get_tool
from apps.mcp.transport import _status_for_response, _ToolValidationError, _validate_tool_arguments

MAX_REQUEST_BODY_SIZE = 4 * 1024 * 1024
MAX_AUDIT_RESPONSE_SIZE = 64 * 1024
REQUEST_BODY_SCOPE_KEY = "brightbean.mcp.request_body"

_AUDITED_PROTOCOL_METHODS = frozenset(
    {
        "completion/complete",
        "initialize",
        "logging/setLevel",
        "notifications/cancelled",
        "notifications/initialized",
        "ping",
        "prompts/get",
        "prompts/list",
        "resources/list",
        "resources/read",
        "resources/subscribe",
        "resources/templates/list",
        "resources/unsubscribe",
        "roots/list",
        "sampling/createMessage",
        "server/discover",
        "tools/call",
        "tools/list",
    }
)


@dataclass(frozen=True)
class _RequestState:
    access_token: BrightBeanAccessToken | None
    authentication_attempted: bool


_request_state: ContextVar[_RequestState | None] = ContextVar("brightbean_mcp_request_state", default=None)


class BrightBeanAccessToken(AccessToken):
    """SDK access token carrying private, request-local BrightBean context."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    principal: Any = Field(exclude=True, repr=False)
    django_request: ASGIRequest = Field(exclude=True, repr=False)


class RequestBoundaryMiddleware:
    """Authenticate, throttle, and audit exactly once per MCP HTTP request."""

    def __init__(self, application, *, resource_url: str):
        self.application = application
        self.resource_url = resource_url

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        body = _request_body(scope)
        started_at = perf_counter()
        async with ThreadSensitiveContext():
            state_token = None
            try:
                bearer = _bearer_token(scope)
                access_token = None
                if bearer is not None:
                    access_token = await sync_to_async(_authenticate_sync, thread_sensitive=True)(
                        bearer,
                        scope,
                        body,
                        self.resource_url,
                    )
                state_token = _request_state.set(
                    _RequestState(access_token=access_token, authentication_attempted=bearer is not None)
                )
                if access_token is not None:
                    try:
                        await sync_to_async(enforce_http_rate_limits, thread_sensitive=True)(
                            access_token.django_request,
                            is_write=True,
                            include_workspace=False,
                        )
                    except HttpError as exc:
                        if exc.status_code != 429:
                            raise
                        await _send_rate_limit(scope, send, exc.message)
                        await _audit(
                            access_token,
                            body,
                            status_code=429,
                            duration_ms=int((perf_counter() - started_at) * 1000),
                        )
                        return

                response_status = 500
                response_body: bytearray | None = bytearray()

                async def capture_send(message):
                    nonlocal response_status, response_body
                    if message["type"] == "http.response.start":
                        response_status = int(message["status"])
                    elif message["type"] == "http.response.body" and response_body is not None:
                        chunk = message.get("body", b"")
                        if len(response_body) + len(chunk) <= MAX_AUDIT_RESPONSE_SIZE:
                            response_body.extend(chunk)
                        else:
                            response_body = None
                    await send(message)

                await self.application(scope, receive, capture_send)
                if access_token is not None:
                    captured_body = bytes(response_body) if response_body is not None else None
                    await _audit(
                        access_token,
                        body,
                        status_code=_synthetic_status(response_status, captured_body),
                        duration_ms=int((perf_counter() - started_at) * 1000),
                    )
            finally:
                if state_token is not None:
                    _request_state.reset(state_token)
                # Keep cleanup on this request's thread-sensitive executor so
                # no connection state can leak into another MCP request.
                await sync_to_async(connections.close_all, thread_sensitive=True)()


class PreAuthTransportSecurityMiddleware:
    """Validate MCP transport headers before authentication has side effects."""

    def __init__(self, application, *, settings: TransportSecuritySettings):
        self.application = application
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if scope["method"] == "POST" and not _valid_content_type(headers.get("content-type")):
            await Response("Invalid Content-Type header", status_code=400)(scope, receive, send)
            return
        if self.settings.enable_dns_rebinding_protection:
            if not _matches_allowed(headers.get("host"), self.settings.allowed_hosts, required=True):
                await Response("Invalid Host header", status_code=421)(scope, receive, send)
                return
            if not _matches_allowed(headers.get("origin"), self.settings.allowed_origins, required=False):
                await Response("Invalid Origin header", status_code=403)(scope, receive, send)
                return
        await self.application(scope, receive, send)


class BoundedRequestBodyMiddleware:
    """Buffer each POST body once and reject declared or streamed oversize input."""

    def __init__(self, application, *, max_body_size: int):
        self.application = application
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.application(scope, receive, send)
            return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                pass
            else:
                if declared_size > self.max_body_size:
                    await Response("Request body too large", status_code=413)(scope, receive, send)
                    return

        body = bytearray()
        received_request = False
        body_complete = False
        trailing_message = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                trailing_message = message
                break
            received_request = True
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_size:
                await Response("Request body too large", status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                body_complete = True
                break

        immutable_body = bytes(body)
        del body
        request_scope = dict(scope)
        request_scope[REQUEST_BODY_SCOPE_KEY] = immutable_body
        cached_messages = deque()
        if received_request:
            cached_messages.append({"type": "http.request", "body": immutable_body, "more_body": not body_complete})
        if trailing_message is not None:
            cached_messages.append(trailing_message)

        async def replay():
            if cached_messages:
                return cached_messages.popleft()
            return await receive()

        await self.application(request_scope, replay, send)


class BrightBeanTokenVerifier:
    """Bridge SDK bearer authentication to BrightBean's existing verifier."""

    def __init__(self, *, resource_url: str) -> None:
        self.resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        state = _request_state.get()
        if state is None or not state.authentication_attempted:
            return None
        access_token = state.access_token
        if access_token is None or access_token.token != token:
            return None
        return access_token


def _authenticated_context(ctx: ServerRequestContext) -> BrightBeanAccessToken:
    request = ctx.request
    if request is None:
        raise MCPError(code=types.INTERNAL_ERROR, message="Authenticated request context is unavailable.")
    try:
        access_token = getattr(request.user, "access_token", None)
    except (AssertionError, RuntimeError):
        access_token = None
    if not isinstance(access_token, BrightBeanAccessToken):
        raise MCPError(code=types.INTERNAL_ERROR, message="Authenticated request context is unavailable.")
    return access_token


def _list_tools_sync(access_token: BrightBeanAccessToken) -> list[types.Tool]:
    from apps.mcp.policy import is_tool_discoverable

    return [
        types.Tool(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            output_schema=tool.output_schema,
            annotations=types.ToolAnnotations(
                title=tool.annotations.title,
                read_only_hint=tool.annotations.read_only,
                destructive_hint=tool.annotations.destructive,
                idempotent_hint=tool.annotations.idempotent,
                open_world_hint=tool.annotations.open_world,
            ),
        )
        for tool in all_tools()
        if is_tool_discoverable(access_token.principal, tool)
    ]


def _call_tool_sync(access_token: BrightBeanAccessToken, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    request = access_token.django_request
    tool = get_tool(name, include_disabled=True)
    if tool is None:
        raise MCPError(code=INVALID_PARAMS, message=f"tools/call: unknown tool '{name}'")
    if not tool.enabled:
        return domain_error_result(tool_disabled_error(name))

    try:
        _validate_tool_arguments(tool.input_schema, arguments)
        handler_arguments = dict(arguments)
        workspace_id = handler_arguments.pop("workspace_id", None)
        if tool.workspace_scoped:
            from apps.mcp.workspace import build_tool_context

            context = build_tool_context(
                access_token.principal,
                workspace_id,
                request,
                is_write=not tool.annotations.read_only,
            )
        else:
            context = {"principal": access_token.principal, "request": request}
        from apps.mcp.policy import evaluate_tool_policy, policy_error, requested_account_ids

        decision = evaluate_tool_policy(
            access_token.principal,
            tool,
            workspace=context.get("workspace"),
            requested_account_ids=requested_account_ids(handler_arguments),
        )
        if not decision.allowed:
            return domain_error_result(policy_error(decision, name))
        from apps.mcp.confirmations import invoke_tool_with_confirmation

        result = invoke_tool_with_confirmation(tool, handler_arguments, context)
    except _ToolValidationError as exc:
        raise MCPError(code=INVALID_PARAMS, message=f"tools/call '{name}': {exc}") from exc
    except JsonRpcError as exc:
        raise MCPError(code=exc.code, message=exc.message, data=exc.data) from exc
    except DomainError as exc:
        return domain_error_result(exc)
    return result


async def _list_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    del params
    access_token = _authenticated_context(ctx)
    tools = await sync_to_async(_list_tools_sync, thread_sensitive=True)(access_token)
    return types.ListToolsResult(tools=tools)


async def _call_tool(
    ctx: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    access_token = _authenticated_context(ctx)
    arguments = params.arguments or {}
    result = await sync_to_async(_call_tool_sync, thread_sensitive=True)(access_token, params.name, arguments)
    content: list[types.ContentBlock] = []
    for block in result.get("content", []):
        if block.get("type") == "text":
            content.append(types.TextContent(type="text", text=str(block.get("text", ""))))
        elif block.get("type") == "resource_link":
            content.append(
                types.ResourceLink(
                    type="resource_link",
                    uri=str(block.get("uri", "")),
                    name=str(block.get("name", "Resource")),
                    description=block.get("description"),
                    mime_type=block.get("mimeType"),
                )
            )
    return types.CallToolResult(
        content=content,
        structured_content=result.get("structuredContent"),
        is_error=bool(result.get("isError", False)),
    )


def build_sdk_server(
    *,
    public_base_url: str,
    oauth_issuer_url: str,
):
    """Return the low-level server and its configured Streamable HTTP app."""

    resource_url = f"{public_base_url.rstrip('/')}/api/v1/mcp"
    server = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )
    transport_security = _transport_security(public_base_url)
    sdk_application = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_SIZE,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(oauth_issuer_url),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=[],
        ),
        token_verifier=BrightBeanTokenVerifier(resource_url=resource_url),
        transport_security=transport_security,
    )
    boundary_application = RequestBoundaryMiddleware(sdk_application, resource_url=resource_url)
    secured_application = PreAuthTransportSecurityMiddleware(
        boundary_application,
        settings=transport_security,
    )
    return server, BoundedRequestBodyMiddleware(secured_application, max_body_size=MAX_REQUEST_BODY_SIZE)


def _authenticate_sync(
    token: str,
    scope: dict[str, Any],
    body: bytes,
    resource_url: str,
) -> BrightBeanAccessToken | None:
    request_scope = dict(scope)
    original_path = scope.get("brightbean.original_path")
    if isinstance(original_path, str):
        request_scope["path"] = original_path
        request_scope["raw_path"] = original_path.encode("ascii", errors="ignore")
        request_scope["root_path"] = ""
    request = ASGIRequest(request_scope, BytesIO(body))
    principal = McpAuth().authenticate(request, token)
    if principal is None:
        return None
    request.auth = principal  # type: ignore[attr-defined]
    client_id = str(principal.api_key.id) if principal.api_key is not None else None
    if client_id is None and principal.oauth_client is not None:
        client_id = str(principal.oauth_client.client_id)
    return BrightBeanAccessToken(
        token=token,
        client_id=client_id or str(principal.id),
        scopes=sorted(principal.granted_scopes),
        resource=resource_url,
        subject=str(principal.user.pk),
        principal=principal,
        django_request=request,
    )


def _request_body(scope: dict[str, Any]) -> bytes:
    body = scope.get(REQUEST_BODY_SCOPE_KEY)
    if isinstance(body, bytes):
        return body
    if scope.get("method") != "POST":
        return b""
    raise RuntimeError("MCP request body was not buffered by the boundary middleware.")


def _replay_body(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _bearer_token(scope: dict[str, Any]) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() != b"authorization":
            continue
        scheme, _, token = value.decode("latin-1").partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    return None


async def _send_rate_limit(scope, send, message: str) -> None:
    payload: dict[str, Any] = {"error": "rate_limited"}
    for token in message.split():
        if "=" in token:
            key, value = token.split("=", 1)
            payload[key] = int(value) if value.isdigit() else value
    headers = {}
    for field, header in (
        ("retry_after", "Retry-After"),
        ("limit", "X-RateLimit-Limit"),
        ("remaining", "X-RateLimit-Remaining"),
    ):
        if field in payload:
            headers[header] = str(payload[field])
    from starlette.responses import JSONResponse

    await JSONResponse(payload, status_code=429, headers=headers)(scope, _replay_body(b""), send)


async def _audit(
    access_token: BrightBeanAccessToken,
    request_body: bytes,
    *,
    status_code: int,
    duration_ms: int,
) -> None:
    action = _audit_action(request_body)
    await sync_to_async(log_audit_entry, thread_sensitive=True)(
        access_token.django_request,
        action=action,
        target_id=None,
        status_code=status_code,
    )
    try:
        message = json.loads(request_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(message, dict):
        return
    raw_params = message.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    protocol_version = params.get("protocolVersion") if isinstance(params.get("protocolVersion"), str) else ""
    if not protocol_version:
        protocol_version = access_token.django_request.META.get("HTTP_MCP_PROTOCOL_VERSION", "")
    from apps.mcp.activity import record_activity

    await sync_to_async(record_activity, thread_sensitive=True)(
        access_token.principal,
        message,
        status_code=status_code,
        duration_ms=duration_ms,
        protocol_version=protocol_version,
        confirmation_state=getattr(access_token.django_request, "_mcp_confirmation_state", None),
    )


def _audit_action(request_body: bytes) -> str:
    try:
        message = json.loads(request_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "mcp.unknown"
    if isinstance(message, list):
        return "mcp.batch"
    if not isinstance(message, dict):
        return "mcp.unknown"
    method = message.get("method")
    if not isinstance(method, str) or method not in _AUDITED_PROTOCOL_METHODS:
        return "mcp.unknown"
    action = f"mcp.{method}"
    if method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        registered_names = {tool.name for tool in all_tools()}
        if isinstance(name, str) and name in registered_names:
            action = f"mcp.tools/call:{name}"
        else:
            action = "mcp.tools/call:unknown"
    return action[:255]


def _synthetic_status(http_status: int, response_body: bytes | None) -> int:
    if http_status < 200 or http_status >= 300:
        return http_status
    if response_body is None:
        return http_status
    try:
        response = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return http_status
    return _status_for_response(response)


def _valid_content_type(content_type: str | None) -> bool:
    return content_type is not None and content_type.lower().startswith("application/json")


def _matches_allowed(value: str | None, allowed_values: list[str], *, required: bool) -> bool:
    if value is None:
        return not required
    if value in allowed_values:
        return True
    return any(allowed.endswith(":*") and value.startswith(f"{allowed[:-2]}:") for allowed in allowed_values)


def _transport_security(public_base_url: str) -> TransportSecuritySettings:
    """Allow only the canonical deployment host/origin at the MCP boundary."""

    from urllib.parse import urlparse

    parsed = urlparse(public_base_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("MCP_PUBLIC_BASE_URL must be an absolute URL.")

    allowed_hosts = [parsed.netloc]
    wildcard_port_host = f"{parsed.hostname}:*"
    if wildcard_port_host not in allowed_hosts:
        allowed_hosts.append(wildcard_port_host)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"{parsed.scheme}://{parsed.netloc}"],
    )
