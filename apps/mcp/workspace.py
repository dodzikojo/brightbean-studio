"""Explicit, non-enumerating MCP workspace routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from apps.mcp.errors import DomainError
from apps.mcp.principal import McpPrincipal, PrincipalWorkspace

if TYPE_CHECKING:
    from apps.members.models import WorkspaceMembership
    from apps.workspaces.models import Workspace


@dataclass(frozen=True)
class McpWorkspaceContext:
    principal: McpPrincipal
    workspace: Workspace
    membership: WorkspaceMembership
    effective_permissions: frozenset[str]
    allowed_account_ids: frozenset[UUID]

    @property
    def workspace_id(self):
        return self.workspace.id

    @property
    def issued_by(self):
        return self.principal.user

    @property
    def issued_by_id(self):
        return self.principal.user.pk

    @property
    def social_accounts(self):
        """Compatibility queryset restricted to this resolved workspace."""
        from apps.social_accounts.models import SocialAccount

        return SocialAccount.objects.filter(
            workspace_id=self.workspace.id,
            id__in=self.allowed_account_ids,
        )


def _parse_workspace_id(workspace_id: str | UUID | None) -> UUID | None:
    if workspace_id is None:
        return None
    try:
        return workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))
    except (TypeError, ValueError) as exc:
        raise DomainError("invalid_request", "workspace_id must be a valid UUID.") from exc


def _workspace_required(principal: McpPrincipal) -> DomainError:
    candidates = sorted(
        ({"id": str(item.workspace.id), "name": item.workspace.name} for item in principal.authorized_workspaces),
        key=lambda item: (item["name"].casefold(), item["id"]),
    )
    return DomainError(
        "workspace_required",
        "Choose a workspace for this operation.",
        details={"workspaces": candidates},
    )


def resolve_workspace(principal: McpPrincipal, workspace_id: str | UUID | None) -> McpWorkspaceContext:
    requested = _parse_workspace_id(workspace_id)
    authorized_by_id: dict[UUID, PrincipalWorkspace] = {
        item.workspace.id: item for item in principal.authorized_workspaces
    }

    if principal.credential_kind == "api_key":
        pinned = principal.workspace_pin_id
        if pinned is None or (requested is not None and requested != pinned):
            raise DomainError("forbidden", "This credential cannot access that workspace.")
        selected = authorized_by_id.get(pinned)
        if selected is None:
            raise DomainError("forbidden", "This credential cannot access that workspace.")
    elif requested is None:
        if len(principal.authorized_workspaces) != 1:
            raise _workspace_required(principal)
        selected = principal.authorized_workspaces[0]
    else:
        selected = authorized_by_id.get(requested)
        if selected is None:
            raise DomainError("forbidden", "This credential cannot access that workspace.")

    if principal.credential_kind == "oauth":
        from apps.social_accounts.models import SocialAccount

        allowed = frozenset(
            SocialAccount.objects.filter(workspace_id=selected.workspace.id).values_list("id", flat=True)
        )
    else:
        from apps.social_accounts.models import SocialAccount

        allowed = frozenset(
            SocialAccount.objects.filter(
                workspace_id=selected.workspace.id,
                id__in=principal.account_allowlist_ids,
            ).values_list("id", flat=True)
        )
    return McpWorkspaceContext(
        principal=principal,
        workspace=selected.workspace,
        membership=selected.membership,
        effective_permissions=selected.effective_permissions,
        allowed_account_ids=allowed,
    )


def build_tool_context(principal: McpPrincipal, workspace_id, request, *, is_write: bool) -> dict:
    workspace_context = resolve_workspace(principal, workspace_id)
    from ninja.errors import HttpError

    from apps.api.limits import enforce_workspace_write_rate_limit

    if is_write:
        try:
            enforce_workspace_write_rate_limit(request, workspace_context.workspace.id)
        except HttpError as exc:
            raise DomainError(
                "rate_limited",
                "The workspace request limit has been reached.",
                details={"retry_after_seconds": 60},
                retryable=True,
            ) from exc
    return {
        "principal": principal,
        "workspace_context": workspace_context,
        # Existing handlers consume an API-key-shaped scope object. Point that
        # compatibility name at the resolved context, never at the credential.
        "api_key": workspace_context,
        "workspace": workspace_context.workspace,
        "membership": workspace_context.membership,
        "request": request,
    }
