"""Tests for the OAuth Authorization Server backing the MCP connector flow.

Covers Dynamic Client Registration (RFC 7591), the RFC 8414 / RFC 9728
discovery documents, and the S256-only PKCE enforcement.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

REGISTER_URL = "/oauth/register"
AS_META_URL = "/.well-known/oauth-authorization-server"
PR_META_URL = "/.well-known/oauth-protected-resource"
PR_META_MCP_URL = "/.well-known/oauth-protected-resource/api/v1/mcp"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def _clear_dcr_rate_limit():
    """DCR counters live in the shared cache keyed on 127.0.0.1 — reset them."""
    from django.core.cache import cache

    keys = ("oauth_dcr_ip:127.0.0.1", "oauth_dcr_registrations_global")
    for k in keys:
        cache.delete(k)
    yield
    for k in keys:
        cache.delete(k)


def _register(client: Client, body: dict):
    return client.post(REGISTER_URL, data=json.dumps(body), content_type="application/json")


@pytest.mark.django_db
class TestDynamicClientRegistration:
    def test_register_public_client(self):
        r = _register(
            Client(),
            {"client_name": "Claude", "redirect_uris": [CLAUDE_REDIRECT], "token_endpoint_auth_method": "none"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["client_id"]
        assert data["token_endpoint_auth_method"] == "none"
        assert data["redirect_uris"] == [CLAUDE_REDIRECT]
        assert data["scope"] == "mcp"

        from oauth2_provider.models import get_application_model

        app = get_application_model().objects.get(client_id=data["client_id"])
        assert app.client_type == app.CLIENT_PUBLIC
        assert app.authorization_grant_type == app.GRANT_AUTHORIZATION_CODE

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "http://127.0.0.1:1455/callback/codex",
            "http://localhost:1455/callback/codex",
            "http://[::1]:1455/callback/codex",
        ],
    )
    def test_register_native_client_with_loopback_http_redirect(self, redirect_uri):
        response = _register(Client(), {"redirect_uris": [redirect_uri]})

        assert response.status_code == 201
        assert response.json()["redirect_uris"] == [redirect_uri]

    def test_rejects_http_redirect(self):
        r = _register(Client(), {"redirect_uris": ["http://evil.example.com/cb"]})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_redirect_uri"

    def test_rejects_missing_redirect(self):
        r = _register(Client(), {"client_name": "x"})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_redirect_uri"

    def test_rejects_confidential_client(self):
        r = _register(
            Client(),
            {"redirect_uris": [CLAUDE_REDIRECT], "token_endpoint_auth_method": "client_secret_basic"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client_metadata"

    def test_rejects_non_json_body(self):
        r = Client().post(REGISTER_URL, data="not json", content_type="application/json")
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client_metadata"

    def test_rate_limited_after_per_ip_cap(self):
        c = Client()
        for _ in range(10):  # per-IP cap is 10 → first 10 succeed
            assert _register(c, {"redirect_uris": [CLAUDE_REDIRECT]}).status_code == 201
        r = _register(c, {"redirect_uris": [CLAUDE_REDIRECT]})  # 11th is blocked
        assert r.status_code == 429
        assert r.json()["error"] == "temporarily_unavailable"


@pytest.mark.django_db
class TestDiscoveryMetadata:
    def test_authorization_server_metadata(self):
        data = Client().get(AS_META_URL).json()
        assert data["issuer"]
        assert data["authorization_endpoint"].endswith("/oauth/authorize/")
        assert data["token_endpoint"].endswith("/oauth/token/")
        assert data["registration_endpoint"].endswith("/oauth/register")
        assert data["revocation_endpoint"].endswith("/oauth/revoke_token/")
        assert data["code_challenge_methods_supported"] == ["S256"]
        assert data["token_endpoint_auth_methods_supported"] == ["none"]
        assert data["scopes_supported"][-1] == "mcp"
        assert "mcp.read" in data["scopes_supported"]

    def test_protected_resource_metadata(self):
        data = Client().get(PR_META_URL).json()
        assert data["resource"].endswith("/api/v1/mcp")
        assert data["scopes_supported"][-1] == "mcp"
        assert "mcp.read" in data["scopes_supported"]
        assert data["bearer_methods_supported"] == ["header"]
        assert data["authorization_servers"]

    def test_protected_resource_metadata_path_scoped(self):
        # RFC 9728 path-scoped variant — what the WWW-Authenticate header points at.
        data = Client().get(PR_META_MCP_URL).json()
        assert data["resource"].endswith("/api/v1/mcp")


class _FakeOAuthRequest:
    """Minimal oauthlib-style request for the validator unit tests.

    Unset attributes read as ``None`` so oauthlib's error constructor — which
    copies ``redirect_uri`` / ``state`` / etc. off the request — doesn't trip.
    """

    def __init__(self, *, code_challenge_method):
        self.code_challenge = "abc123"
        self.code_challenge_method = code_challenge_method

    def __getattr__(self, name):
        return None


class TestS256PkceEnforcement:
    def test_plain_method_rejected(self):
        from oauthlib.oauth2.rfc6749 import errors as oauthlib_errors

        from apps.oauth_server.validator import S256OnlyOAuth2Validator

        with pytest.raises(oauthlib_errors.InvalidRequestError):
            S256OnlyOAuth2Validator().is_pkce_required(
                "client-id",
                _FakeOAuthRequest(code_challenge_method="plain"),
            )

    def test_s256_method_allowed(self):
        from apps.oauth_server.validator import S256OnlyOAuth2Validator

        # Does not raise; defers to the parent's PKCE_REQUIRED decision.
        assert S256OnlyOAuth2Validator().is_pkce_required(
            "client-id",
            _FakeOAuthRequest(code_challenge_method="S256"),
        ) in (True, False)


class TestAuthorizationRedirectSecurity:
    def test_authorization_response_allows_loopback_http(self):
        from apps.oauth_server.views import CanonicalResourceAuthorizationView

        response = CanonicalResourceAuthorizationView().redirect(
            "http://127.0.0.1:1455/callback/codex?code=abc",
            application=None,
        )

        assert response.status_code == 302
        assert response["Location"].startswith("http://127.0.0.1:1455/")

    def test_authorization_response_rejects_remote_http(self):
        from django.core.exceptions import DisallowedRedirect

        from apps.oauth_server.views import CanonicalResourceAuthorizationView

        with pytest.raises(DisallowedRedirect):
            CanonicalResourceAuthorizationView().redirect(
                "http://evil.example.com/callback?code=abc",
                application=None,
            )

    @pytest.mark.django_db
    def test_consent_csp_allows_loopback_but_not_general_http(self, django_user_model):
        from oauth2_provider.models import get_application_model

        from apps.oauth_server.resources import canonical_mcp_resource_uri

        user = django_user_model.objects.create_user(
            email="csp@example.com",
            password="test",
            tos_accepted_at=timezone.now(),
        )
        app_model = get_application_model()
        app = app_model.objects.create(
            name="Native client",
            client_type=app_model.CLIENT_PUBLIC,
            authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
            redirect_uris="http://127.0.0.1:1455/callback/codex",
        )
        client = Client()
        client.force_login(user)

        response = client.get(
            "/oauth/authorize/",
            {
                "client_id": app.client_id,
                "redirect_uri": app.redirect_uris,
                "response_type": "code",
                "scope": "mcp.read",
                "code_challenge": "A" * 43,
                "code_challenge_method": "S256",
                "resource": canonical_mcp_resource_uri(),
            },
        )

        assert response.status_code == 200
        policy = response.headers.get("Content-Security-Policy") or response["Content-Security-Policy-Report-Only"]
        form_action = policy.split("form-action ", 1)[1].split(";", 1)[0]
        assert "http://127.0.0.1:*" in form_action
        assert "http://localhost:*" in form_action
        assert "http:" not in form_action.split()
