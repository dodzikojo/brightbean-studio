"""Rollback-safe ASGI routing for the legacy and SDK MCP transports."""

from __future__ import annotations

from contextlib import asynccontextmanager

from django.conf import settings
from django.core.asgi import get_asgi_application
from django.core.exceptions import ImproperlyConfigured
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

MCP_PATH = "/api/v1/mcp"
VALID_BACKENDS = {"legacy", "sdk_v2"}


class McpDispatcher:
    """Dispatch only exact MCP endpoints; preserve every other ASGI scope."""

    def __init__(self, *, django_application, sdk_application=None, enabled: bool, aliases: tuple[str, ...] = ()):
        self.django_application = django_application
        self.sdk_application = sdk_application
        self.enabled = enabled
        bases = (MCP_PATH, *aliases)
        self.mcp_paths = frozenset(path for base in bases for path in (base, f"{base}/"))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in self.mcp_paths:
            await self.django_application(scope, receive, send)
            return
        if not self.enabled:
            await JSONResponse({"error": "not_found", "detail": "Not found."}, status_code=404)(scope, receive, send)
            return
        if self.sdk_application is None:
            await self.django_application(scope, receive, send)
            return

        sdk_scope = dict(scope)
        sdk_scope["brightbean.original_path"] = scope["path"]
        sdk_scope["root_path"] = MCP_PATH
        sdk_scope["path"] = "/"
        sdk_scope["raw_path"] = b"/"
        await self.sdk_application(sdk_scope, receive, send)


def create_application(
    *,
    django_application=None,
    enabled: bool | None = None,
    backend: str | None = None,
    public_base_url: str | None = None,
    oauth_issuer_url: str | None = None,
    staging_alias: str | None = None,
):
    """Build the top-level ASGI app with explicit inputs for reliable tests."""

    django_application = django_application or get_asgi_application()
    enabled = settings.MCP_SERVER_ENABLED if enabled is None else enabled
    backend = settings.MCP_TRANSPORT_BACKEND if backend is None else backend
    public_base_url = public_base_url or settings.MCP_PUBLIC_BASE_URL
    oauth_issuer_url = oauth_issuer_url or settings.MCP_OAUTH_ISSUER_URL
    staging_alias = settings.MCP_STAGING_ALIAS if staging_alias is None else staging_alias

    if backend not in VALID_BACKENDS:
        raise ImproperlyConfigured("MCP_TRANSPORT_BACKEND must be one of: " + ", ".join(sorted(VALID_BACKENDS)))

    aliases: tuple[str, ...] = ()
    if staging_alias:
        if not staging_alias.startswith("/") or staging_alias.endswith("/"):
            raise ImproperlyConfigured("MCP_STAGING_ALIAS must be empty or an absolute path without a trailing slash.")
        aliases = (staging_alias,)

    server = None
    sdk_application = None
    if enabled and backend == "sdk_v2":
        from apps.mcp.server import build_sdk_server

        server, sdk_application = build_sdk_server(
            public_base_url=public_base_url,
            oauth_issuer_url=oauth_issuer_url,
        )

    dispatcher = McpDispatcher(
        django_application=django_application,
        sdk_application=sdk_application,
        enabled=enabled,
        aliases=aliases,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        del app
        if server is None:
            yield
            return
        async with server.session_manager.run():
            yield

    return Starlette(routes=[Mount("/", app=dispatcher)], lifespan=lifespan)
