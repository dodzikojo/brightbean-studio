"""Credential-neutral identity for MCP authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from apps.mcp.errors import DomainError

if TYPE_CHECKING:
    from oauth2_provider.models import AbstractAccessToken, AbstractApplication

    from apps.accounts.models import User
    from apps.api_keys.models import ApiKey
    from apps.members.models import WorkspaceMembership
    from apps.workspaces.models import Workspace

CredentialKind = Literal["api_key", "oauth"]


@dataclass(frozen=True)
class PrincipalWorkspace:
    membership: WorkspaceMembership
    workspace: Workspace
    effective_permissions: frozenset[str]


@dataclass(frozen=True)
class McpPrincipal:
    credential_kind: CredentialKind
    user: User
    api_key: ApiKey | None
    oauth_access_token: AbstractAccessToken | None
    oauth_client: AbstractApplication | None
    granted_scopes: frozenset[str]
    workspace_pin_id: UUID | None
    api_key_permissions: frozenset[str]
    account_allowlist_ids: frozenset[UUID]
    authorized_workspaces: tuple[PrincipalWorkspace, ...]

    @property
    def is_oauth(self) -> bool:
        return self.credential_kind == "oauth"

    @property
    def id(self):
        return self.api_key.id if self.api_key is not None else f"oauth:{self.user.pk}"

    @property
    def issued_by(self):
        return self.user

    @property
    def issued_by_id(self):
        return self.user.pk

    @property
    def workspace_id(self):
        return self.workspace_pin_id

    @property
    def rate_override_writes(self):
        return self.api_key.rate_override_writes if self.api_key is not None else None

    @property
    def rate_override_reads(self):
        return self.api_key.rate_override_reads if self.api_key is not None else None


def _permission_names(membership: WorkspaceMembership) -> frozenset[str]:
    custom_role = membership.custom_role
    if custom_role is not None and custom_role.organization_id != membership.workspace.organization_id:
        return frozenset()
    return frozenset(key for key, enabled in membership.effective_permissions.items() if enabled)


def principal_from_api_key(api_key: ApiKey) -> McpPrincipal:
    from apps.members.models import WorkspaceMembership

    user = api_key.issued_by
    if api_key.issued_by_id is None or user is None or not user.is_active or api_key.workspace.is_archived:
        raise DomainError("forbidden", "This credential is not authorized for MCP.")
    try:
        membership = WorkspaceMembership.objects.select_related("workspace__organization", "custom_role").get(
            user_id=api_key.issued_by_id, workspace_id=api_key.workspace_id
        )
    except WorkspaceMembership.DoesNotExist as exc:
        raise DomainError("forbidden", "This credential is not authorized for MCP.") from exc

    current = _permission_names(membership)
    granted = frozenset(api_key.permissions or ())
    effective = current & granted
    account_ids = frozenset(
        api_key.social_accounts.filter(workspace_id=api_key.workspace_id).values_list("id", flat=True)
    )
    workspace = membership.workspace
    return McpPrincipal(
        credential_kind="api_key",
        user=user,
        api_key=api_key,
        oauth_access_token=None,
        oauth_client=None,
        granted_scopes=frozenset({"mcp"}),
        workspace_pin_id=workspace.id,
        api_key_permissions=granted,
        account_allowlist_ids=account_ids,
        authorized_workspaces=(PrincipalWorkspace(membership, workspace, effective),),
    )


def principal_from_oauth_token(access_token: AbstractAccessToken) -> McpPrincipal:
    from apps.members.models import WorkspaceMembership

    user = access_token.user
    if access_token.user_id is None or user is None or not user.is_active:
        raise DomainError("forbidden", "This credential is not authorized for MCP.")
    memberships = (
        WorkspaceMembership.objects.filter(user_id=access_token.user_id, workspace__is_archived=False)
        .select_related("workspace__organization", "custom_role")
        .order_by("workspace__name", "workspace_id")
    )
    authorized = tuple(
        PrincipalWorkspace(membership, membership.workspace, _permission_names(membership))
        for membership in memberships
    )
    return McpPrincipal(
        credential_kind="oauth",
        user=user,
        api_key=None,
        oauth_access_token=access_token,
        oauth_client=access_token.application,
        granted_scopes=frozenset((access_token.scope or "").split()),
        workspace_pin_id=None,
        api_key_permissions=frozenset(),
        account_allowlist_ids=frozenset(),
        authorized_workspaces=authorized,
    )
