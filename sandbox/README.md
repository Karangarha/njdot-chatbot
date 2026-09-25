# NJDOT local sandbox

Production-shaped copies of both apps, run on your machine against the
Supabase you already have locally, with an E2E suite that clicks through
every screen and logs every error and warning into one report.

| Piece | How it matches production |
|---|---|
| **Backend** (`:8000`) | Microsoft's own **App Service Linux Python 3.13** runtime image (`mcr.microsoft.com/appsvc/python`). The app is copied to `/home/site/wwwroot` like the deploy workflow's `backend/*` artifact. An Oryx-style `antenv` virtualenv is built from `requirements.txt`. Azure's `init_container.sh` launches the same Startup Command: `uvicorn app.main:app --host 0.0.0.0 --port 8000`. Env vars stand in for App Settings. |
| **Frontend** (`:3000`) | The Vercel Next.js flow on Node 22: `npm install`, then `next build` with `NEXT_PUBLIC_*` inlined **at build time**, then `next start`. It builds from `/vercel/path0` and sets `VERCEL=1`, `VERCEL_ENV=production` and `CI=1`. |
| **Supabase** | Your existing local Supabase (for example `supabase start` on `:54321`/`:54322`). The sandbox only adds what is missing; see `db-apply`. |
| **LLMs** (`:4010`) | A mock OpenAI and Anthropic API with deterministic answers, so runs cost nothing. It supports embeddings, `json_schema` structured output, tool calling, Anthropic `tool_use`, and failure injection. To use the real APIs, set real keys and base URLs in `.env`. |
| **Neo4j** (`:7688`, UI `:7475`) | Bundled Neo4j 5. To use Neo4j Desktop instead, set `NEO4J_URI=bolt://host.docker.internal:7687`. |

## Run it

Needs Docker Desktop, Node 20+, and your local Supabase running.

```bash
./sandbox/sandbox.sh init       # creates sandbox/.env (fills Supabase keys from `supabase status` if the CLI is installed)
./sandbox/sandbox.sh doctor     # checks Supabase reachability and lists which tables/functions/buckets exist
./sandbox/sandbox.sh db-apply   # adds ONLY missing schema (never drops/replaces) — see supabase/schema.sql
./sandbox/sandbox.sh up         # builds + starts everything (first build is slow; the Azure image is amd64)
./sandbox/sandbox.sh seed       # reference PDFs → public 'pdfs' bucket, built-in checks, scheduling-manual chunks (skips what exists)
./sandbox/sandbox.sh test       # Playwright suite → sandbox/logs/REPORT.md
./sandbox/sandbox.sh down
```

Then open http://localhost:3000 to click around by hand, with the same containers.

If `supabase status` isn't run from your Supabase project folder, set
`SUPABASE_PROJECT_DIR=/path/to/that/folder` before `init`, or fill `sandbox/.env`
by hand. Email confirmations must be off (`[auth.email] enable_confirmations = false`,
the local default), because the suite signs up fresh users on every run.

**Results of the first full run:** [`FINDINGS.md`](FINDINGS.md) (triaged) and
[`reports/2026-09-25-REPORT.md`](reports/2026-09-25-REPORT.md) (raw).

Extra commands:
- `./sandbox/sandbox.sh checks` runs `next build` (for build warnings),
  ESLint and the backend unit tests inside the production images. `test`
  runs it automatically.
- `AZURE_STARTUP_COMMAND` in `.env` reproduces the Web App's exact Startup
  Command.
- `PW_CHROMIUM_EXECUTABLE` tells Playwright to use an already-installed
  Chromium.

## What the suite covers (37 tests)

- **01 landing/auth:**
  - every landing link and image
  - `middleware.ts` redirects
  - signup: validation, success, duplicate email
  - login: wrong password
  - user menu and logout
  - change password, forgot password (both steps and "Start over"), unknown email
- **02 chat:**
  - send button states
  - collection filter reaches the request body
  - the full request order: `conversations` insert → `messages` ×2 → `/api/query` → `updated_at` PATCH
  - suggestion pills
  - citation → PDF modal, closed by button, Escape and backdrop
  - history reload and New Chat
  - delete conversation, **re-checked after a page reload**
  - backend error shown as an error bubble
- **03 review** (using the Route 49 XER, narrative, key sheet and DBE memo in `e2e/fixtures/`):
  - upload slot validation and removing files
  - utility plans
  - Checklist Manager: toggle/fork, add, edit, delete, reset
  - the full run:
    - Storage uploads
    - `POST /api/review`
    - SSE progress through to results
    - project saved to `review_projects`
    - filter pills and collapsible sections
    - Download PDF
    - citation pills
    - Document Q&A (session SSE, history, question)
    - Re-run
    - project in sidebar, then reload and delete
  - "no checks selected" guard
- **04 backend API:**
  - OpenAPI lists every route the frontend calls
  - CORS for localhost, Vercel previews, and a rejected random origin
  - validation errors
  - `/api/pdf`
  - 401 on every protected route
  - an unsigned `alg=none` JWT is rejected
  - session endpoint auth
  - OpenAI outage handling
  - the LLM was actually called

To include the Special Provision, put it at
`e2e/fixtures/Route49-PSE-Special_Provision.pdf`. The review test uploads it
when it's there and skips that slot when it isn't.

## Logs and the report

Every test records every browser console line, uncaught exception, request
(with status and timing) and failed request. These are attached to the HTML
report (`browser-log.txt`). A test **fails** on:
- any console error or uncaught exception
- any 5xx response
- any unexpected 4xx from the backend or Supabase
- any dropped request

Expected errors are allow-listed per test and still reported as warnings.

After the run, `sandbox/logs/` holds:

- `REPORT.md`: **every error and warning from the run, deduplicated with
  counts.** It covers failed tests, security findings, browser errors and
  warnings, backend (Azure container) tracebacks, errors and warnings,
  frontend server errors, mock-LLM errors and Neo4j warnings.
- `backend.log`, `frontend.log`, `mock-llm.log`, `neo4j.log`: raw container logs.

Rebuild the report any time with `./sandbox/sandbox.sh report`. The HTML report
with traces, screenshots and videos for failures is at `e2e/playwright-report/`.

## What's deliberately *not* like production

- The frontend container runs a tiny localhost→host forwarder
  (`frontend/localhost-forward.mjs`). This lets one `NEXT_PUBLIC_SUPABASE_URL`
  (`http://127.0.0.1:54321`) work both in your browser and in Next.js's
  server-side calls. On Vercel the URL is public, so it isn't needed there.
- The App Service container skips SSH and the diagnostics cron
  (`WEBSITE_SSH_ENABLED=0`, `WEBSITE_USE_DIAGNOSTIC_SERVER=false`).
- The LLM is mocked by default. Answers check wiring and contracts, not
  answer quality.
