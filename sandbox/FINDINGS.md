# Sandbox findings — run of 2026-09-25

**What ran:** the full stack in Docker:
- the backend on Microsoft's App Service Python 3.13 image
- the frontend built Vercel-style with `next build` and `next start`
- a Supabase CLI local stack (the same thing `supabase start` / Studio on :54323 runs)
- Neo4j 5 with APOC
- the mock LLM

It used the Route 49 XER, narrative, key sheet and DBE memo. The raw
report, with every error and warning deduplicated, is
[`reports/2026-09-25-REPORT.md`](reports/2026-09-25-REPORT.md).

**Result:** 34 of 37 E2E tests pass. The 3 failures are all real app issues (items 2–4 below).
- **Backend unit tests:** 315 pass on the App Service image.
- **`next build`:** succeeds, with one real warning.

Status legend: ✅ confirmed by the run · ⚠️ from code review, not exercised end to end.

## Fix first

1. ✅ **The Azure Startup Command in the deploy notes doesn't start the app.**
   `.github/workflows/azure-deploy-backend.yml` says to set
   `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
   - **What happens:** in the App Service runtime, Oryx exposes the `antenv`
     virtualenv only through `PYTHONPATH`, and there is no global `uvicorn`. The
     container exits with `uvicorn: not found` (exit 127).
   - **Fix:** use `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`, or
     gunicorn with `-k uvicorn.workers.UvicornWorker`.
   - **Check:** confirm what the NJDOT Web App actually has under
     Configuration → General settings. To reproduce the exact value, set
     `AZURE_STARTUP_COMMAND` in `sandbox/.env`.
2. ✅ **Anyone who knows the project UUID can use the session Q&A endpoints (no auth check).**
   `GET /api/session/messages/{id}` returns 200 without a token (test `04 ›
   @security session endpoints`). `/upload`, `/status` and `/query` don't read
   `Authorization` at all (`backend/app/api/session.py`). The review endpoints
   next to them do check ownership.
3. ✅ **Deleting a chat doesn't delete it.** The row vanishes from the sidebar,
   then comes back on reload. The database still has every "Delete me"
   conversation from the runs.
   - **Cause:** `migrations/001_conversations.sql` only creates SELECT and INSERT
     policies. Supabase answers the DELETE with 204 and deletes 0 rows.
   - **Same cause:** the `updated_at` bump after each message
     (`ChatInterface.tsx:271`) is also a silent no-op, so Recents ordering
     never changes.
   - **Fix:** add `for update` and `for delete using (auth.uid() = user_id)`
     policies on `conversations`.
4. ✅ **Changing your password logs you out.** The page says "Password
   updated! Redirecting you back…", then lands on `/login`.
   - **Cause:** `/api/auth/change-password` uses the Supabase admin API, which
     revokes the user's sessions. The next `getUser()` in `middleware.ts` gets
     `403 session_not_found`.
   - **Fix:** call `supabase.auth.updateUser({ password })` from the browser,
     which keeps the current session. Or, if it must stay server-side, sign the
     user back in and say so in the UI.
5. ✅ **Anyone who knows an email can take over the account.**
   `POST /api/auth/request-reset` returns a working `reset_token` to whoever
   asks, and nothing is emailed. The E2E test resets another account's password
   using only its email.

## Should fix

6. ✅ **`middleware.ts` is deprecated in Next 16.** `next build` warns: *The
   "middleware" file convention is deprecated. Please use "proxy" instead.*
   Rename it to `src/proxy.ts`.
7. ✅ **`/api/query` errors leak internals.** With OpenAI down, the client gets
   `500 {"detail":"Pipeline error [RuntimeError]: LLM completion failed
   [InternalServerError]: Error code: 500 - {...}"}`. That exposes the exception
   class and the upstream error body. The shape is fine (JSON, no hang, no
   traceback); only the text needs trimming.
8. ✅ **Five tables and one bucket have no DDL in git:** `review_projects`,
   `compliance_checks`, `session_messages`, `bdc_section_map`, `rate_limits`,
   and the public `pdfs` bucket.
   - A fresh Supabase can't be rebuilt from the repo.
     `sandbox/supabase/schema.sql` reconstructs all of them, and the whole app
     ran against that reconstruction. It's a good starting point for a real
     migration.
9. ✅ **The graph layer requires Neo4j APOC.** langchain-neo4j calls
   `apoc.meta.data()`. Without APOC, every review fails with "An unexpected
   error occurred" in the UI. Aura includes APOC; a self-hosted Neo4j must
   enable it. Worth writing into the README prerequisites.
10. ⚠️ **`scripts/ingest_bdc.py:283-284` writes the string `"None"` for missing
    dates.** A `date`-typed column would reject the insert.

## Noise worth cleaning up (from the logs)

- **Langfuse:** logs a WARNING on *every* LLM call when its keys are unset
  (127 lines in one run). Initialize it once, or check the keys before
  creating a client.
- **Neo4j:** logs `01N51`/`01N52` notifications ("relationship type
  CONSTRAINED_BY / COVERED_BY / MENTIONS does not exist") whenever a project
  has no nodes of that kind yet. Set the driver's
  `notifications_min_severity="OFF"` (or `WARNING` → `OFF` for these), or guard
  the queries.
- **Frontend ESLint:** 7 `no-explicit-any` errors and 2 unused variables.
  Vercel's build doesn't lint, so none of this blocks deploys.
- **pytest:** `scripts/test_db.py::test_connection` returns a bool, which
  triggers `PytestReturnNotNoneWarning`.

## Checked and fine

- **Auth and access:**
  - Every route guard in `middleware.ts` works.
  - Signup, login, logout and forgot-password work.
  - An `alg=none` forged JWT is rejected.
  - ES256 JWKS verification works, the same way as hosted Supabase.
  - Every protected review/conversation route returns 401 without a token.
- **CORS:**
  - localhost and `*.vercel.app` previews are allowed.
  - Random origins are refused.
- **Chat:**
  - The full DB write sequence works.
  - The collection filter reaches the backend.
  - Suggestion pills work.
  - The citation → `/api/pdf` modal works, and closes by button, Escape and backdrop.
  - Reopening a conversation from history works.
  - A backend error shows as an error bubble.
- **Review, with the Route 49 files:**
  - Storage uploads work.
  - SSE progress arrives and the results render.
  - Filter pills and collapsible sections work.
  - Download PDF works.
  - Document Q&A works: SSE, history, and the graph agent tool call through Cypher.
  - Re-run, the project list, reload from the DB, and delete all work.
  - The "no checks selected" guard works.
- **Checklist manager:** fork, toggle, add, edit, delete and reset all work.
- **Browser:** zero console errors or uncaught exceptions across all 37 tests.
- **HEAD requests:** the browser logs the `HEAD compliance_checks` requests as
  "aborted". That's a Chromium quirk for body-less HEAD fetches. All 249 got
  200 from Supabase, and each user's fork has the right row count.

## Not covered

- **Special Provision:** no Route 49 Special Provision PDF was available, so the
  21 SP-dependent checks correctly reported Missing without an LLM call. Put it
  at `e2e/fixtures/Route49-PSE-Special_Provision.pdf` to include it.
- **LLM output:** the mock only proves the wiring and contracts. Answer quality
  needs real keys (`OPENAI_BASE_URL=https://api.openai.com/v1` in `.env`).
