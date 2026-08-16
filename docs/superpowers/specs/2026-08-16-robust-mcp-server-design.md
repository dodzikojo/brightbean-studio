# BrightBean Robust MCP Server Design

**Date:** 2026-08-16  
**Status:** Approved  
**Branch:** `codex/brightbean-robust-mcp`

## Objective

Replace BrightBean's handwritten MCP protocol layer with the official MCP Python SDK v2 while
preserving the public endpoint, existing credentials, OAuth discovery, and existing tool names.
The result is a stateless, multi-workspace, policy-controlled MCP server with safe confirmed
mutations, typed responses, resources, prompts, and an in-product activity/control plane.

BrightBean remains generic and upstream-friendly. ClashWise and Foreman are representative tenant
data used for isolation smoke tests; neither name may appear in product logic.

## Runtime architecture

The production web process becomes a multi-worker ASGI server. `config.asgi` builds the normal
Django ASGI application and dispatches the two exact MCP paths to an official SDK Streamable HTTP
application. Django serves every other route. The MCP mount accepts both `/api/v1/mcp` and
`/api/v1/mcp/` without redirects.

Two settings protect rollout:

- `MCP_SERVER_ENABLED` disables MCP before authentication or discovery.
- `MCP_TRANSPORT_BACKEND=legacy|sdk_v2` selects the old Ninja/JSON-RPC handler or SDK v2.

The SDK server is stateless. It supports individual MCP requests and notifications, including
older protocol versions supported by the SDK, but not JSON-RPC batches. Batch behavior remains
available only through the legacy backend during the compatibility release.

The web command changes from WSGI Gunicorn to Gunicorn with Uvicorn workers (or an equivalent
pinned ASGI worker). The existing `process_tasks` worker remains separate and unchanged.

## Internal boundaries

`apps/mcp` is organized around these responsibilities:

- `server.py`: SDK construction, capability registration, lifespan, and transport app.
- `routing.py`: exact ASGI path mount, kill switch, backend selection, and legacy bridge.
- `principal.py`: normalized API-key/OAuth identity and current authorization facts.
- `workspace.py`: explicit workspace resolution without dashboard-state mutation.
- `registry.py`: tool metadata, schemas, risk, scopes, RBAC, availability, and handlers.
- `policy.py`: deterministic policy evaluation and filtered discovery.
- `results.py` and `errors.py`: typed structured content, text fallback, links, and domain errors.
- `confirmations.py`: previews, grants, canonical payload hashing, and idempotent execution.
- Domain modules: `context`, `content`, `media`, `calendar`, `analytics`, `approvals`, and `inbox`.
- `resources.py` and `prompts.py`: authorized read-only MCP primitives.
- `activity.py`: redacted event capture and target-link construction.

MCP handlers remain adapters. Existing REST serializers, API builders, and domain services own
business behavior so serialization and permission rules cannot silently diverge.

## Principal and authorization

`McpPrincipal` contains credential kind, user, OAuth client or API key, granted scopes, active
workspace memberships, API-key workspace pin, social-account allowlist, and effective RBAC facts.
It is constructed once per request and passed through MCP context.

Workspace resolution rules are exact:

1. API-key calls always resolve to the key's active workspace. A conflicting `workspace_id` is
   denied without revealing whether the requested workspace exists.
2. OAuth calls resolve from the user's non-archived active memberships.
3. OAuth with one workspace may omit `workspace_id`.
4. OAuth with multiple workspaces must provide it; omission returns `workspace_required` with only
   authorized workspace IDs and safe labels.
5. Resolution never reads or writes `last_workspace_id`.

Authorization is checked at both discovery and invocation in this precedence order:

1. Infrastructure kill switch.
2. Organization MCP switch.
3. Code-level tool availability.
4. Organization tool policy.
5. Workspace tool policy.
6. OAuth scope or API-key permission.
7. Current user RBAC.
8. Social-account allowlist.

An organization-disabled tool cannot be re-enabled by a workspace. Disabled tools are omitted from
discovery; a client invoking a previously discovered name receives `tool_disabled`.

OAuth scopes are `mcp.read`, `mcp.content`, `mcp.publish`, `mcp.inbox.reply`, and `mcp.admin`.
Legacy `mcp` expands to all capability scopes for compatibility, then intersects with current RBAC.
Tokens are bound to the canonical MCP resource URI. Exact redirect URI matching, PKCE S256, scope
intersection, binding preservation on refresh, and current-membership checks are mandatory.

## Persistence and auditability

- `McpOrganizationConfig`: one-to-one organization MCP enabled state.
- `McpToolPolicy`: organization or workspace override with a database constraint enforcing one
  target level and uniqueness per target/tool.
- `McpActivityEvent`: actor/client/workspace/primitive/tool/status/duration/protocol/correlation,
  confirmation state, safe target references, and redacted summary.
- `McpConfirmationGrant`: one-use random token digest, actor/credential/workspace/tool, canonical
  payload hash, expiry, and consumption metadata.
- `McpIdempotencyRecord`: credential-safe key digest, payload hash, execution state, and a safe
  replayable result envelope.
- OAuth token-binding model: token/application/resource binding needed to validate access and refresh.

Activity retention follows the existing 365-day audit setting. A data migration backfills existing
`mcp.*` API-key audit entries as coarse events when safe fields exist. No content bodies are copied.

## Confirmation and idempotency contract

Externally consequential actions include schedule, publish-now, inbox reply, approval/rejection,
and any transition that changes external visibility or commits an editorial decision.

Without `confirmation_token`, the handler validates and authorizes the request, builds a redacted
preview, persists an expiring payload-bound one-use grant, and returns no mutation. With a token,
the handler additionally requires `idempotency_key`, verifies an identical canonical payload and
current authorization, atomically claims both grant and idempotency record, then executes once.

Concurrent callers with the same key observe one executor. Successful retries replay the stored safe
result. A failed execution records a retry-safe failure state without permitting payload substitution.
Tokens, keys, captions, replies, signed URLs, and uploaded bytes never enter activity summaries.

## MCP public surface

Existing tool names remain and the full approved registry is grouped as follows:

- Discovery: `list_workspaces`, `get_workspace_context`, `list_accounts`, `get_account_health`.
- Ideas/content: `list_ideas`, `create_idea`, `update_idea`, `convert_idea_to_draft`, `create_draft`,
  `update_draft`, `clone_post`, `get_post`, `list_posts`.
- Editorial: `list_post_comments`, `add_post_comment`, `submit_for_review`, `approve_post`,
  `request_changes`, `reject_post`.
- Publishing/calendar: `schedule_post`, `schedule_draft`, `publish_post`, `cancel_post`,
  `get_calendar`, `list_queues`, `enqueue_post`, `reschedule_post`.
- Media: `search_media`, `get_media`, `upload_media`, `request_media_upload`,
  `finalize_media_upload`.
- Analytics: `get_workspace_analytics`, `get_account_analytics`, `get_post_analytics`, `get_best_times`.
- Inbox: `list_inbox`, `get_inbox_message`, `add_inbox_note`, `assign_inbox_message`,
  `set_inbox_status`, `send_inbox_reply`.

No tool connects or disconnects a social account. Health results expose capability diagnostics and a
BrightBean browser reconnect URL. No destructive delete tool is included.

Every tool publishes an input schema, output schema, annotations, risk metadata, required scope and
RBAC permission, pagination contract where relevant, structured content, and a readable text fallback.
Failures use stable machine codes such as `workspace_required`, `forbidden`, `not_found`,
`tool_disabled`, `confirmation_required`, `confirmation_expired`, `payload_mismatch`, and
`idempotency_conflict` without cross-tenant information leaks.

## Resources and prompts

Authorized resources are:

- `brightbean://workspaces`
- `brightbean://workspaces/{workspace_id}/context`
- `brightbean://workspaces/{workspace_id}/accounts`
- `brightbean://workspaces/{workspace_id}/calendar/{start_date}/{end_date}`
- `brightbean://workspaces/{workspace_id}/posts/{post_id}`
- `brightbean://workspaces/{workspace_id}/analytics/{days}`
- `brightbean://workspaces/{workspace_id}/inbox/{message_id}`

Workspace context includes description, timezone, brand colors, hashtags, approval policy,
categories, tags, and templates, filtered by authorization.

Prompts are `campaign_plan`, `draft_social_post`, `weekly_performance_review`, and `triage_inbox`.
They require explicit `workspace_id`, reference authorized resources, produce guidance/messages only,
and never perform mutations.

## Settings control plane

An MCP section is added under settings:

- `/settings/mcp/`: connection state, endpoint, version, organization switch, client guidance, recent activity.
- `/settings/mcp/activity/`: paginated filters for workspace, primitive, tool, actor,
  credential/client, outcome, and time.
- `/settings/mcp/tools/`: searchable registry, risk/scope/RBAC/confirmation metadata, and permitted
  organization/workspace restrictions.

Users with `manage_api_keys` control organization policy and see organization activity. Users with
`manage_workspace_settings` may inspect and further restrict only their workspaces. All forms use
POST, CSRF, server-side authorization, explicit validation, and PRG redirects.

## Rollout and acceptance

Development lands in reviewable slice commits on the approved current `codex/` branch. Staging adds
`/api/v1/mcp-next` with an isolated database. Acceptance covers official conformance and Inspector,
Codex, Claude Desktop/Code, Cursor, and API-key clients. The two real workspaces exercise authorized
reads, draft/media creation, preview-without-mutation, one owner-confirmed schedule, activity, policy
disable/re-enable, and cross-workspace denial.

Production then sets `MCP_TRANSPORT_BACKEND=sdk_v2`, monitors errors, latency, and activity for 24
hours, removes the staging alias after acceptance, and retains legacy for one release cycle.

