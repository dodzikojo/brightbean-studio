# BrightBean Robust MCP Server Implementation Plan

> Execute with subagent-driven development. Each task starts with a failing test, receives
> specification and quality review, and is committed independently when green.

**Goal:** Ship the approved official-SDK MCP server, multi-workspace authorization, safe mutation
contract, control plane, full operational tools, resources, prompts, and rollback-safe Railway rollout.

**Architecture:** An official MCP SDK v2 Streamable HTTP app is mounted inside the Django ASGI
entry point. MCP adapters call shared domain services. A normalized principal and deterministic
policy engine gate discovery and invocation. Confirmed mutations use database-backed grants and
idempotency. Redacted activity is queryable from BrightBean settings.

**Stack:** Python 3.12, Django 5.1, MCP Python SDK v2, Django Ninja, PostgreSQL,
django-background-tasks, Gunicorn/Uvicorn ASGI workers, pytest, Ruff, mypy.

## Global constraints

- Work on `codex/brightbean-robust-mcp` in the current checkout, as explicitly requested.
- Keep all behavior generic; workspace/product names appear only in acceptance data.
- Preserve existing endpoint, credentials, OAuth discovery, tool names, and REST parity.
- Never use `last_workspace_id` for MCP routing and never mutate it from MCP.
- Never log or persist content bodies, credentials, signed URLs, uploads, or full MCP envelopes.
- Preview calls for consequential actions must not mutate any domain object.
- No destructive delete or social-account connect/disconnect tools.
- Local Windows Python is blocked; use Docker and CI for executable verification.

## Task 1: Repository onboarding and execution ledger

**Files:** `AGENTS.md`, design spec, this plan, `.superpowers/sdd/2026-08-16-robust-mcp-server/progress.md`

- Verify the branch, deployment responsibilities, CI commands, and existing MCP architecture.
- Record the Windows Application Control limitation and tenant/security invariants.
- Initialize the SDD ledger; record the user-approved current-checkout ruling.
- Verify links, formatting, line count, `git diff --check`, and clean secret scan of added docs.
- Commit: `Document robust MCP architecture and execution plan`.

## Task 2: SDK v2 ASGI foundation

**Files:** `requirements.txt`, `config/asgi.py`, `config/settings/base.py`, `apps/mcp/server.py`,
`apps/mcp/routing.py`, `apps/mcp/tests/test_sdk_transport.py`, `.env.example`, `Dockerfile`, `Procfile`,
`railway.toml`, `README.md`

- Write transport tests for exact slash/no-slash behavior, initialize/ping, disabled response,
  backend selection, legacy batching, SDK batch rejection, and Django route passthrough.
- Pin `mcp>=2.0,<3.0`, `uvicorn` and the compatible Gunicorn worker package/version.
- Build the stateless SDK app with server metadata and an ASGI dispatcher that preserves request path.
- Keep Ninja legacy route reachable only when `MCP_TRANSPORT_BACKEND=legacy`.
- Add `MCP_SERVER_ENABLED`, `MCP_TRANSPORT_BACKEND`, and an optional staging alias setting.
- Change all web start commands and docs to multi-worker ASGI; do not change the background worker.
- Verify focused tests, ASGI runtime smoke, lint, and Docker image startup.
- Commit: `Mount the official MCP SDK through ASGI`.

## Task 3: Typed registry, results, and domain errors

**Files:** `apps/mcp/registry.py`, `apps/mcp/results.py`, `apps/mcp/errors.py`,
`apps/mcp/legacy.py`, `apps/mcp/tests/test_registry.py`, existing tool/transport tests

- Write tests for stable names, input/output schemas, annotations, pagination cursors, text fallback,
  resource links, duplicate registration, disabled discovery, and safe typed error payloads.
- Define one metadata-rich registry that can register SDK primitives and adapt legacy discovery/calls.
- Add canonical success/error result builders with JSON-serializable structured content.
- Adapt the 14 existing handlers without changing their business output or names.
- Keep JSON-RPC batch behavior isolated in `legacy.py`.
- Commit: `Introduce the typed MCP registry and results`.

## Task 4: Principal and explicit workspace routing

**Files:** `apps/mcp/principal.py`, `apps/mcp/workspace.py`, `apps/api/auth.py`,
`apps/mcp/tests/test_principal.py`, `apps/mcp/tests/test_workspace_routing.py`

- Test API-key pinning, allowlists, OAuth zero/one/many membership routing, archived workspaces,
  conflicting IDs, role changes, cross-tenant denial, and unchanged `last_workspace_id`.
- Construct `McpPrincipal` from both existing credential modes without querying tenant objects first.
- Resolve only authorized memberships and return safe `workspace_required` candidates.
- Thread the principal and resolved workspace through SDK and legacy contexts.
- Add `workspace_id` to scoped existing tool schemas while preserving valid pinned-key omission.
- Commit: `Add explicit multi-workspace MCP routing`.

## Task 5: OAuth capability scopes and resource binding

**Files:** `apps/oauth_server/models.py`, migration, OAuth settings/metadata/views/forms as applicable,
`apps/oauth_server/services.py`, OAuth/MCP tests

- Test the five scopes, legacy alias expansion, exact redirect URI, mandatory PKCE S256, requested
  scope validation, current-RBAC intersection, canonical resource mismatch, refresh preservation,
  revocation, and token/client boundaries.
- Add the OAuth token-binding record using digests/foreign keys rather than raw token material.
- Bind authorization and refresh grants to the canonical MCP resource URI.
- Advertise capability scopes while accepting `mcp` as the compatibility alias.
- Ensure token refresh cannot widen scope or change resource.
- Commit: `Bind MCP OAuth tokens to capabilities and resource`.

## Task 6: Policy and activity persistence

**Files:** `apps/mcp/models.py`, migrations, `apps/mcp/policy.py`, `apps/mcp/activity.py`,
`apps/mcp/admin.py`, cleanup/backfill migration or command, tests

- Test model constraints, policy precedence, org-disable dominance, workspace restriction, stale calls,
  current scope/RBAC/allowlist checks, safe summaries, duration/correlation, and 365-day retention.
- Add organization config, tool policy, activity event, confirmation grant, and idempotency models.
- Implement a side-effect-free policy decision object shared by discovery and invocation.
- Capture authenticated methods/tools/resources/prompts in `finally` paths, including failures.
- Backfill coarse `mcp.*` audit rows without copying bodies; register retention cleanup.
- Commit: `Add MCP policy and redacted activity models`.

## Task 7: Confirmation and idempotency engine

**Files:** `apps/mcp/confirmations.py`, models/migration adjustments, focused tests

- Test preview no-op, token entropy/digest storage, expiry, actor/credential/workspace/tool binding,
  canonical payload mismatch, one-use consumption, missing idempotency key, key collision, concurrent
  execution, success replay, failure recovery, and redacted previews.
- Canonicalize only validated arguments and exclude confirmation/idempotency fields from payload hash.
- Use database transactions, row locks/conditional updates, and uniqueness constraints for one executor.
- Persist only safe replay envelopes and never raw externally visible content.
- Expose one reusable wrapper for all consequential handlers.
- Commit: `Require confirmation for consequential MCP actions`.

## Task 8: MCP settings control plane

**Files:** `apps/settings_manager/urls.py`, new MCP views/forms or `apps/mcp/views.py`, templates under
`templates/settings/mcp/`, sidebar/navigation template, UI/security tests

- Test anonymous denial, organization/workspace permission boundaries, filter isolation, CSRF/POST,
  org/workspace precedence, search/pagination, target links, and no sensitive rendering.
- Build overview, activity, and searchable tools pages at the approved paths.
- Permit `manage_api_keys` users to control organization settings and inspect organization activity.
- Permit `manage_workspace_settings` users only to inspect/restrict authorized workspaces.
- Display endpoint, version, credential guidance, risk/scope/RBAC/confirmation metadata, and recent events.
- Runtime-smoke each page at desktop and narrow viewport.
- Commit: `Add the MCP settings and activity control plane`.

## Task 9: Discovery and content tools

**Files:** `apps/mcp/context.py`, `apps/mcp/content.py`, relevant shared services/builders, tests

- Add and test `list_workspaces`, `get_workspace_context`, `get_account_health`, `list_ideas`,
  `create_idea`, `update_idea`, `convert_idea_to_draft`, `update_draft`, and `clone_post`.
- Adapt existing `list_accounts`, `create_draft`, `get_post`, and `list_posts` to typed outputs.
- Reuse workspace/composer/account services; extract shared services before copying view logic.
- Add stable cursor pagination and reconnect links that route to BrightBean browser UI.
- Test every role/workspace/allowlist boundary and REST parity where a REST route exists.
- Commit: `Expand MCP discovery and content operations`.

## Task 10: Editorial and calendar/publishing tools

**Files:** `apps/mcp/approvals.py`, `apps/mcp/calendar.py`, shared approval/calendar/composer services, tests

- Add editorial comments and review transitions plus calendar, queues, enqueue, reschedule, publish-now.
- Adapt existing schedule/cancel/schedule-draft operations through shared domain services.
- Apply confirmation wrapper to scheduling, publish-now, approvals, rejections, and consequential states.
- Assert preview leaves posts/platform posts/queues/actions/jobs untouched; confirmed execution occurs once.
- Verify account capability, approval policy, RBAC, and allowlist at execution time.
- Commit: `Add confirmed editorial and publishing operations`.

## Task 11: Media and analytics tools

**Files:** `apps/mcp/media.py`, `apps/mcp/analytics.py`, shared media/analytics builders, tests

- Move/adapt existing media search/get/direct-upload/presigned-upload tools into focused modules.
- Add workspace analytics and best-times; adapt account/post analytics to typed pagination/freshness.
- Preserve upload size/type validation and never put bytes or signed URLs into activity.
- Keep REST and MCP response builders shared and verify exact parity where promised.
- Commit: `Add typed MCP media and analytics operations`.

## Task 12: Inbox tools

**Files:** `apps/mcp/inbox.py`, shared inbox services, tests

- Add list/get, private note, assignment, status, and reply operations with safe pagination.
- Test visibility, account allowlists, assignee membership, status state machine, and provider capabilities.
- Require `mcp.inbox.reply`, matching RBAC, confirmation, and idempotency for external replies.
- Assert note/assignment/status behavior matches existing browser workflows and audit links.
- Commit: `Add MCP inbox triage and confirmed replies`.

## Task 13: Resources and prompts

**Files:** `apps/mcp/resources.py`, `apps/mcp/prompts.py`, SDK registration, tests

- Register every approved URI template and prompt name.
- Test URI parsing, explicit workspace authorization, date/day limits, not-found privacy, MIME/content
  shape, resource links, prompt arguments, and non-mutation.
- Reuse the same builders and policy gates as tools; do not create a second authorization path.
- Ensure prompts return guidance/messages and authorized resource references only.
- Commit: `Expose authorized MCP resources and prompts`.

## Task 14: Full verification and review

**Files:** all changed files plus verification notes in SDD ledger

- Run focused then full `pytest --cov=apps --cov-report=term-missing` in Docker/CI.
- Run `ruff check .`, `ruff format --check .`, mypy, migration drift check, Docker build, and secret scan.
- Run official MCP conformance checks and MCP Inspector against slash and no-slash endpoints.
- Runtime-smoke Django routes, MCP initialize/list/call/resource/prompt, legacy rollback, kill switches,
  activity UI, policy disable/re-enable, and representative failures.
- Request specification review and code-quality review; resolve all findings and rerun verification.
- Commit: `Complete MCP verification and rollout safeguards`.

## Task 15: Railway staging and acceptance

**Files:** deployment configuration/docs only if acceptance exposes changes

- Inventory Railway project/services/environments read-only; create an isolated staging environment/database.
- Deploy the branch with `/api/v1/mcp-next`, legacy production endpoint unchanged, and explicit settings.
- Validate API-key clients, then request owner participation for Codex/Claude/Cursor OAuth consent.
- Smoke both real workspaces without hard-coded assumptions: context, draft, media, no-op preview,
  one owner-confirmed scheduled action, activity, policy switch, and cross-workspace denial.
- Switch production to `sdk_v2`, monitor 24 hours, then remove staging alias after acceptance.
- Keep legacy available for one release cycle and document the later removal issue.

