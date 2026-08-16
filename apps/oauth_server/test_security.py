from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client
from django.utils import timezone
from jsonschema import Draft202012Validator

from apps.oauth_server.resources import canonical_mcp_resource_uri
from apps.oauth_server.scopes import ADVERTISED_MCP_SCOPES, scope_allows


def test_scope_alias_and_granular_scope_semantics():
    assert scope_allows({"mcp"}, "mcp.publish")
    assert scope_allows({"mcp.read"}, "mcp.read")
    assert not scope_allows({"mcp.read"}, "mcp.publish")


def test_discovery_advertises_capability_scopes_and_exact_resource():
    authorization = Client().get("/.well-known/oauth-authorization-server").json()
    protected = Client().get("/.well-known/oauth-protected-resource").json()

    assert authorization["scopes_supported"] == list(ADVERTISED_MCP_SCOPES)
    assert protected["scopes_supported"] == list(ADVERTISED_MCP_SCOPES)
    assert protected["resource"] == canonical_mcp_resource_uri()
    assert protected["bearer_methods_supported"] == ["header"]


@pytest.mark.django_db
def test_dcr_rejects_duplicate_fragment_userinfo_and_whitespace_redirects():
    for redirect_uris in (
        ["https://client.example/cb", "https://client.example/cb"],
        ["https://client.example/cb#fragment"],
        ["https://user@client.example/cb"],
        ["https://client.example/cb bad"],
    ):
        response = Client().post(
            "/oauth/register",
            data={"redirect_uris": redirect_uris},
            content_type="application/json",
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.django_db
def test_validator_requires_exact_redirect_uri():
    from oauth2_provider.models import get_application_model

    from apps.oauth_server.validator import S256OnlyOAuth2Validator

    app_model = get_application_model()
    app = app_model.objects.create(
        name="Exact redirect",
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://client.example/callback?fixed=1",
    )
    validator = S256OnlyOAuth2Validator()
    assert validator.validate_redirect_uri(app.client_id, "https://client.example/callback?fixed=1", object())
    assert not validator.validate_redirect_uri(
        app.client_id,
        "https://client.example/callback?fixed=1&added=2",
        object(),
    )


@pytest.mark.django_db
def test_access_binding_is_digest_only_and_fails_closed_on_resource_or_scope_change(django_user_model):
    from oauth2_provider.models import get_access_token_model, get_application_model

    from apps.oauth_server.models import McpOAuthTokenBinding
    from apps.oauth_server.services import bind_access_token, verify_access_binding

    user = django_user_model.objects.create_user(email="binding@example.com", password="test")
    app_model = get_application_model()
    app = app_model.objects.create(
        name="Binding",
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://client.example/callback",
    )
    raw = "raw-access-secret"
    access = get_access_token_model().objects.create(
        user=user,
        application=app,
        token=raw,
        scope="mcp.read",
        resource=[canonical_mcp_resource_uri()],
        expires=timezone.now() + timedelta(hours=1),
    )
    binding = bind_access_token(access, raw)

    assert binding.token_digest != raw
    assert raw not in repr(binding.__dict__)
    assert verify_access_binding(raw, access) == binding
    access.scope = "mcp.publish"
    access.save(update_fields=["scope"])
    assert verify_access_binding(raw, access) is None
    assert McpOAuthTokenBinding.objects.count() == 1


@pytest.mark.django_db
def test_unbound_oauth_token_is_rejected(django_user_model):
    from oauth2_provider.models import get_access_token_model, get_application_model

    from apps.api.auth import _resolve_oauth_actor

    user = django_user_model.objects.create_user(email="unbound@example.com", password="test")
    app_model = get_application_model()
    app = app_model.objects.create(
        name="Unbound",
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://client.example/callback",
    )
    raw = "unbound-secret"
    get_access_token_model().objects.create(
        user=user,
        application=app,
        token=raw,
        scope="mcp.read",
        resource=[canonical_mcp_resource_uri()],
        expires=timezone.now() + timedelta(hours=1),
    )
    assert _resolve_oauth_actor(raw) is None


def test_error_schema_fixture_is_valid_json_schema():
    from apps.mcp.errors import DOMAIN_ERROR_OUTPUT_SCHEMA

    Draft202012Validator.check_schema(DOMAIN_ERROR_OUTPUT_SCHEMA)


@pytest.mark.django_db
def test_authorization_code_flow_binds_access_and_refresh_tokens(django_user_model):
    from oauth2_provider.models import get_application_model

    from apps.oauth_server.models import McpOAuthTokenBinding
    from apps.oauth_server.services import token_digest, verify_refresh_binding

    user = django_user_model.objects.create_user(
        email="pkce-flow@example.com", password="test", tos_accepted_at=timezone.now()
    )
    app_model = get_application_model()
    redirect_uri = "https://client.example/callback"
    app = app_model.objects.create(
        name="PKCE flow",
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris=redirect_uri,
    )
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    resource = canonical_mcp_resource_uri()
    authorize = {
        "client_id": app.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "mcp.read mcp.content",
        "state": "state-1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    client = Client()
    client.force_login(user)
    consent = client.get("/oauth/authorize/", authorize)
    assert consent.status_code == 200, consent.content
    approved = client.post(
        "/oauth/authorize/",
        {**authorize, "allow": True},
    )
    assert approved.status_code == 302
    code = parse_qs(urlparse(approved["Location"]).query)["code"][0]

    issued = client.post(
        "/oauth/token/",
        {
            "grant_type": "authorization_code",
            "client_id": app.client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert issued.status_code == 200, issued.content
    payload = issued.json()
    assert payload["scope"] == "mcp.read mcp.content"
    assert McpOAuthTokenBinding.objects.filter(token_kind="access", revoked_at__isnull=True).count() == 1
    assert McpOAuthTokenBinding.objects.filter(token_kind="refresh", revoked_at__isnull=True).count() == 1
    assert payload["access_token"] not in repr(list(McpOAuthTokenBinding.objects.values()))
    assert payload["refresh_token"] not in repr(list(McpOAuthTokenBinding.objects.values()))

    refresh_binding = McpOAuthTokenBinding.objects.get(token_kind="refresh", revoked_at__isnull=True)
    assert verify_refresh_binding(payload["refresh_token"], refresh_binding.refresh_token) == refresh_binding
    refresh_binding.resource_digest = token_digest("https://attacker.example/api/v1/mcp")
    refresh_binding.save(update_fields=["resource_digest"])
    assert verify_refresh_binding(payload["refresh_token"], refresh_binding.refresh_token) is None
    refresh_binding.resource_digest = token_digest(resource)
    refresh_binding.save(update_fields=["resource_digest"])

    wrong_resource = client.post(
        "/oauth/token/",
        {
            "grant_type": "refresh_token",
            "client_id": app.client_id,
            "refresh_token": payload["refresh_token"],
            "resource": "https://attacker.example/api/v1/mcp",
        },
    )
    assert wrong_resource.status_code == 400
    assert wrong_resource.headers["Content-Type"].startswith("application/json"), wrong_resource.content
    assert wrong_resource.json()["error"] == "invalid_target"

    widened = client.post(
        "/oauth/token/",
        {
            "grant_type": "refresh_token",
            "client_id": app.client_id,
            "refresh_token": payload["refresh_token"],
            "scope": "mcp",
        },
    )
    assert widened.status_code == 400
    assert widened.json()["error"] == "invalid_scope"

    refreshed = client.post(
        "/oauth/token/",
        {
            "grant_type": "refresh_token",
            "client_id": app.client_id,
            "refresh_token": payload["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.content
    refreshed_payload = refreshed.json()
    assert refreshed_payload["scope"] == "mcp.read mcp.content"
    assert refreshed_payload["access_token"] != payload["access_token"]
    assert McpOAuthTokenBinding.objects.filter(token_kind="refresh", revoked_at__isnull=False).count() == 1
    assert McpOAuthTokenBinding.objects.filter(token_kind="access", revoked_at__isnull=True).count() == 1

    revoked = client.post(
        "/oauth/revoke_token/",
        {
            "token": refreshed_payload["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": app.client_id,
        },
    )
    assert revoked.status_code == 200
    assert McpOAuthTokenBinding.objects.filter(token_kind="refresh", revoked_at__isnull=True).count() == 0
    assert McpOAuthTokenBinding.objects.filter(token_kind="access", revoked_at__isnull=True).count() == 0


@pytest.mark.django_db
def test_authorize_rejects_missing_or_noncanonical_resource(django_user_model):
    from oauth2_provider.models import get_application_model

    user = django_user_model.objects.create_user(
        email="resource-reject@example.com", password="test", tos_accepted_at=timezone.now()
    )
    app_model = get_application_model()
    app = app_model.objects.create(
        name="Resource reject",
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://client.example/callback",
    )
    client = Client()
    client.force_login(user)
    base = {
        "client_id": app.client_id,
        "redirect_uri": "https://client.example/callback",
        "response_type": "code",
        "scope": "mcp.read",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
    }
    assert client.get("/oauth/authorize/", base).status_code == 400
    assert client.get("/oauth/authorize/", {**base, "resource": f"{canonical_mcp_resource_uri()}/"}).status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("challenge", "method"),
    [
        (None, None),
        ("a" * 43, "plain"),
    ],
)
def test_authorize_requires_s256_pkce(django_user_model, challenge, method):
    from oauth2_provider.models import get_application_model

    user = django_user_model.objects.create_user(
        email=f"pkce-{method or 'missing'}@example.com",
        password="test",
        tos_accepted_at=timezone.now(),
    )
    app_model = get_application_model()
    app = app_model.objects.create(
        name="PKCE required",
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://client.example/callback",
    )
    request_data = {
        "client_id": app.client_id,
        "redirect_uri": "https://client.example/callback",
        "response_type": "code",
        "scope": "mcp.read",
        "resource": canonical_mcp_resource_uri(),
    }
    if challenge is not None:
        request_data["code_challenge"] = challenge
    if method is not None:
        request_data["code_challenge_method"] = method

    client = Client()
    client.force_login(user)
    response = client.get("/oauth/authorize/", request_data)
    assert response.status_code == 302
    redirect = urlparse(response["Location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == app.redirect_uris
    assert parse_qs(redirect.query)["error"] == ["invalid_request"]
