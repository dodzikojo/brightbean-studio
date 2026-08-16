"""Custom OAuth2 validator that restricts PKCE to S256 only.

django-oauth-toolkit's default validator (and oauthlib under it) accepts both
``S256`` and ``plain`` for ``code_challenge_method``. RFC 7636 §4.2 marks
``plain`` as insecure unless the channel is fully secure end-to-end — which
defeats the entire point of PKCE for the kind of MCP-client scenarios we
support. The protected-resource metadata document advertises ``S256`` as the
only supported method (see ``apps.oauth_server.metadata``); this validator
enforces that contract at the authorize endpoint, blocking authorization
requests with a missing or ``plain`` method early — before any Grant row is
written.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model, get_refresh_token_model
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749 import errors as oauthlib_errors

from .models import McpOAuthTokenBinding
from .resources import canonical_mcp_resource_uri
from .services import (
    bind_access_token,
    bind_refresh_token,
    revoke_binding,
    token_digest,
    verify_refresh_binding,
)


class S256OnlyOAuth2Validator(OAuth2Validator):
    """OAuth2Validator subclass that requires ``code_challenge_method=S256``.

    Hooks ``is_pkce_required`` because it is the earliest validator method
    oauthlib invokes while ``request.code_challenge_method`` is still the
    raw client-supplied value (oauthlib later defaults a missing method to
    ``"plain"`` at ``authorization_code.py``; we reject before that lands).
    """

    def is_pkce_required(self, client_id, request):
        code_challenge = getattr(request, "code_challenge", None)
        method = getattr(request, "code_challenge_method", None)
        if code_challenge and method != "S256":
            raise oauthlib_errors.InvalidRequestError(
                description="code_challenge_method must be S256",
                request=request,
            )
        return super().is_pkce_required(client_id, request)

    def validate_redirect_uri(self, client_id, redirect_uri, request, *args, **kwargs):
        application = get_application_model().objects.filter(client_id=client_id).first()
        return bool(application and redirect_uri in application.redirect_uris.split())

    def _check_and_set_request_resource(self, request):
        super()._check_and_set_request_resource(request)
        if request.resource != [canonical_mcp_resource_uri()]:
            raise oauthlib_errors.InvalidTargetError(
                description="The canonical MCP resource is required.",
                request=request,
            )

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        valid = super().validate_refresh_token(refresh_token, client, request, *args, **kwargs)
        if not valid:
            return False
        binding = verify_refresh_binding(refresh_token, getattr(request, "refresh_token_instance", None))
        if binding is None:
            return False
        request.mcp_source_refresh_binding = binding
        return True

    @transaction.atomic
    def _save_bearer_token(self, token, request, *args, **kwargs):
        parent = getattr(request, "mcp_source_refresh_binding", None)
        source_access_id = (
            parent.refresh_token.access_token_id if parent is not None and parent.refresh_token is not None else None
        )
        if parent is not None and parent.revoked_at is None:
            parent.revoked_at = timezone.now()
            parent.save(update_fields=["revoked_at"])
        if source_access_id is not None:
            McpOAuthTokenBinding.objects.filter(
                token_kind=McpOAuthTokenBinding.TokenKind.ACCESS,
                access_token_id=source_access_id,
                revoked_at__isnull=True,
            ).update(revoked_at=timezone.now())
        super()._save_bearer_token(token, request, *args, **kwargs)
        access = get_access_token_model().objects.get(token_checksum=token_digest(token["access_token"]))
        bind_access_token(access, token["access_token"])
        raw_refresh = token.get("refresh_token")
        if raw_refresh:
            refresh = get_refresh_token_model().objects.get(
                token_checksum=token_digest(raw_refresh),
                revoked__isnull=True,
            )
            bind_refresh_token(refresh, raw_refresh, parent=parent)

    def revoke_token(self, token, token_type_hint, request, *args, **kwargs):
        binding = (
            McpOAuthTokenBinding.objects.select_related("refresh_token")
            .filter(token_digest=token_digest(token))
            .first()
        )
        associated_access_id = (
            binding.refresh_token.access_token_id if binding is not None and binding.refresh_token is not None else None
        )
        revoke_binding(token)
        if associated_access_id is not None:
            McpOAuthTokenBinding.objects.filter(
                token_kind=McpOAuthTokenBinding.TokenKind.ACCESS,
                access_token_id=associated_access_id,
                revoked_at__isnull=True,
            ).update(revoked_at=timezone.now())
        return super().revoke_token(token, token_type_hint, request, *args, **kwargs)
