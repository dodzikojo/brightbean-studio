# BrightBean Studio Agent Guide

## Project intent

- BrightBean Studio is a generic, upstream-friendly social-media management product.
- Keep fork changes product-agnostic. ClashWise, Foreman, and future brands are workspace data,
  never branches, constants, fixtures, defaults, or UI copy in product code.
- Prefer small, reviewable services shared by REST, browser views, jobs, and MCP over parallel logic.
- Preserve upstream conventions unless a documented security or platform requirement overrides them.

## Stack and layout

- Python 3.12, Django 5.1, Django Ninja, PostgreSQL, and django-background-tasks.
- `apps/` contains Django product domains; `providers/` contains social-provider adapters.
- `config/` owns settings, URL routing, WSGI/ASGI entry points, and deployment wiring.
- `templates/` and `theme/static_src/` own server-rendered UI and Tailwind sources.
- `apps/api/` is the public REST surface; `apps/mcp/` is the MCP integration layer.
- `apps/oauth_server/` is BrightBean's OAuth authorization server for MCP clients.
- Generated migrations are excluded from Ruff; do not hand-edit existing migrations.

## Local setup and verified commands

- Install dependencies: `pip install -r requirements.txt` and `cd theme/static_src; npm install`.
- Apply schema: `python manage.py migrate`.
- Start web: `python manage.py runserver`.
- Start required jobs in another terminal: `python manage.py process_tasks`.
- Run tests: `pytest` or coverage: `pytest --cov=apps --cov-report=term-missing`.
- Lint: `ruff check .` and `ruff format --check .`.
- Type-check: `mypy apps/ config/ providers/ tests/ --ignore-missing-imports`.
- Build production image: `docker build -t brightbean-studio .`.
- These are the repository and CI commands; run all relevant commands before a completion claim.
- In this Windows checkout, `pytest` was not on PATH and `py -3.12` was blocked by Windows
  Application Control. Use the Docker image or CI; do not alter code to work around that policy.

## Railway and production

- Railway uses one image for web and worker services plus managed PostgreSQL.
- The web service serves Django and the in-process MCP ASGI application.
- The worker runs `python manage.py process_tasks`; publishing, inbox sync, analytics, retries,
  reminders, media processing, and cleanup depend on it.
- Migrations must run once on each deploy. The Railway template does this on web startup;
  manual Railway setups must configure an equivalent pre-deploy/release command.
- Keep Dockerfile, Procfile, Railway documentation, and actual start commands synchronized.
- Never assume a deployment is healthy from build success alone: check `/health/`, worker logs,
  migrations, MCP initialize/discovery, and one authorized read call.

## Security and tenancy invariants

- Every workspace-owned queryset and mutation must be scoped before object lookup.
- API keys are pinned to one workspace and may be restricted to a social-account allowlist.
- OAuth users may have multiple workspaces; never derive MCP routing from `last_workspace_id`.
- An omitted `workspace_id` is valid only for a pinned API key or exactly one active membership.
- Never change dashboard workspace state as a side effect of an API or MCP request.
- Re-check current OAuth scopes, user membership, RBAC, organization/workspace policy, and account
  allowlists at execution time; authorization captured at token issue is not sufficient.
- Social-account connect/disconnect remains browser-only.
- No MCP operation deletes posts, media, accounts, credentials, workspaces, or organizations.
- Publishing, scheduling, replies, and approval transitions require payload-bound confirmation and
  idempotency. A preview call must never mutate domain state.
- Treat captions, replies, tokens, signed URLs, uploads, and request/result bodies as sensitive.
- Activity records contain safe IDs, counts, changed-field names, status, timing, and links only.
- Keep secrets out of source, fixtures, logs, comments, snapshots, and tool error messages.

## MCP architecture

- The canonical endpoint is `/api/v1/mcp`; `/api/v1/mcp/` must behave identically.
- `MCP_SERVER_ENABLED` is the infrastructure kill switch.
- `MCP_TRANSPORT_BACKEND=legacy|sdk_v2` selects rollback-safe transport behavior.
- The official MCP SDK path is stateless Streamable HTTP and does not support JSON-RPC batches.
- Legacy batching remains available only while the legacy backend is selected.
- Keep tool names stable. Return typed structured content plus a useful text fallback.
- Tool discovery must apply the same policy gates as invocation; stale calls return `tool_disabled`.
- Machine-readable domain errors must be stable and must not reveal cross-tenant object existence.
- Resources and prompts are read-only. Prompts provide messages/guidance and never mutate data.

## Change workflow

- Large work belongs on a `codex/` branch with specs in `docs/superpowers/specs/`, plans in
  `docs/superpowers/plans/`, tests first, and reviewable commits by slice.
- Preserve unrelated user changes in a dirty worktree; never reset or overwrite them.
- Use application services for business rules and serializers/builders for REST/MCP parity.
- Add migrations and tests with every persistent-model change.
- Test tenant isolation across API-key/OAuth, role, workspace, and account-allowlist boundaries.
- Test confirmation expiry, payload mismatch, replay, concurrency, failure recovery, and redaction.
- Product-surface changes require a runtime smoke in addition to automated tests.
- External publish/reply smoke tests require the owner to approve the final confirmed action.

