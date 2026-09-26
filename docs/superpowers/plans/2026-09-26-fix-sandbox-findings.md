# Fix Sandbox Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every error and warning the production-parity sandbox found on 2026-09-25 (`sandbox/FINDINGS.md`, raw log in `sandbox/reports/2026-09-25-REPORT.md`). Success means `./sandbox/sandbox.sh test` shows 37/37 E2E passing and a clean report, and the deploy notes no longer describe a startup command that crashes.

**Architecture:** There are no new subsystems. Each task is a targeted fix at the line the sandbox pointed to, plus the test that proves it:
- **Backend fixes:** each gets a pytest in `backend/tests/`, using the repo's existing pattern: call the endpoint function directly and patch `get_db`.
- **Frontend fixes:** Next.js has no unit-test harness here, so each one is proven by the sandbox E2E test that currently fails (`sandbox/e2e/tests/*`). Those tests already assert the correct behaviour; they turn green when the fix lands.
- **Database fixes:** each ships as a numbered migration in `backend/migrations/`, and the sandbox applies it through `db-apply`.

**Tech Stack:**
- **Backend:** Python 3.11+ (App Service runs 3.13), FastAPI, supabase-py 2.28, PyJWT, langchain-neo4j 0.10, neo4j driver 6.2, langfuse 4.x, pytest.
- **Frontend:** Next.js 16.1, @supabase/ssr 0.8, supabase-js 2.97.
- **Sandbox:** Playwright 1.63 (`sandbox/e2e`).

## Global Constraints

- **Run backend Python from the repo root with `.venv/Scripts/python.exe`** (same as earlier plans). The equivalent in the sandbox is `./sandbox/sandbox.sh checks`, which runs pytest inside the App Service image.
- **Verify every task end to end in the sandbox** before ticking it off:
  1. `./sandbox/sandbox.sh up`. Rebuild when backend or frontend code changed; it runs `docker compose up --build` through `up`.
  2. `./sandbox/sandbox.sh test`.
  3. Check that the task's target test flipped to green and no other test regressed.
- **Never weaken an E2E assertion to make it pass.** The E2E tests encode production-correct behaviour. If one looks wrong, stop and raise it.
- **Access-control changes keep the existing "usable signed-out" design.** A project with no owner (the anonymous review flow) stays readable without a token, exactly as `GET /api/review/{id}/status` already behaves ([review.py:227-249](../../../backend/app/api/review.py:227)). Only owned projects get locked down.
- **Never log tokens, passwords or query strings.** This carries over from the 2026-09-15 logging plan.
- **Migrations are additive** (`create … if not exists`, guarded `create policy`) so they're safe to run on the live Supabase, which may already have some of these objects.
- **One commit per task.** Commit message: `fix(<area>): <what>`, plus the repo's usual attribution lines.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `backend/app/project_access.py` | `project_owner(project_id)` and `require_project_access(project_id, authorization)`. This is the one ownership rule shared by the review and session routers. It's a separate module so `session.py` doesn't import `review.py` internals at module load. |
| `backend/tests/test_session_auth.py` | Tests for Task 2. |
| `backend/tests/test_query_error_detail.py` | Tests for Task 7. |
| `backend/tests/test_observability_quiet.py` | Tests for Task 9. |
| `backend/tests/test_bdc_dates.py` | Tests for Task 11. |
| `backend/migrations/011_conversations_update_delete_policies.sql` | Task 3. |
| `backend/migrations/012_baseline_untracked_tables.sql` | Task 8. |
| `frontend/src/app/auth/confirm/route.ts` | Task 5: exchanges the emailed recovery `token_hash` for a session. |

**Modified:**
- **Backend:**
  - `.github/workflows/azure-deploy-backend.yml`
  - `backend/app/api/session.py`, `backend/app/api/query.py`, `backend/app/api/auth.py`
  - `backend/app/observability.py`, `backend/app/neo4j_client.py`
  - `backend/scripts/ingest_bdc.py`, `backend/scripts/test_db.py`
- **Frontend:**
  - `frontend/src/middleware.ts`, renamed to `frontend/src/proxy.ts`
  - `frontend/src/app/update-password/page.tsx`
  - `frontend/src/components/auth/ForgotPasswordForm.tsx`
  - `frontend/src/components/review/SessionChat.tsx`, `frontend/src/components/review/DocumentReview.tsx`
  - `frontend/src/components/chat/ChatInterface.tsx`
  - `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`
- **Docs and sandbox:**
  - `README.md`
  - `sandbox/e2e/tests/01-landing-auth.spec.ts`
  - `sandbox/supabase/schema.sql`
  - `sandbox/FINDINGS.md`

## Deliberately out of scope

- **Rate limiting on auth endpoints.** It's worth doing, but it isn't a sandbox finding.
- **Real-LLM answer quality.** The sandbox runs a mock LLM, so this plan fixes wiring, not answers.
- **Adding Authorization to `/api/query`** (finding 8, minor). The endpoint is intentionally anonymous today. Changing that is a product decision, not a bug fix.

## Decisions needed before starting

1. **Password reset email (Task 5).** The fix has Supabase email the reset link. Hosted Supabase's built-in mailer only delivers to members of the project's organisation and is heavily rate-limited, so production needs a **custom SMTP provider** configured in Supabase (Dashboard → Authentication → Emails → SMTP Settings). Locally, the Supabase CLI captures mail in Mailpit (http://127.0.0.1:54324). If SMTP can't be set up yet, ship Tasks 1–4 and 6–12 first. Task 5 is independent.
2. **Azure Startup Command (Task 1).** Someone with portal access has to read the current value on the NJDOT Web App. Only the portal shows what production actually runs.

---

### Task 1: Azure startup command that actually starts

**Why:** The deploy notes tell you to set `uvicorn app.main:app …` as the Startup Command. In the App Service Python 3.13 runtime, Oryx exposes the `antenv` virtualenv only through `PYTHONPATH`, so `uvicorn` is not on `PATH` and the container exits 127 with `uvicorn: not found`. The sandbox reproduced this with Microsoft's own image.

**Files:**
- Modify: `.github/workflows/azure-deploy-backend.yml:18-21`

- [ ] **Step 1: Check what production runs.** In the Azure Portal, open NJDOT → Configuration → General settings → Startup Command, and record the value in the PR description. If it's the bare `uvicorn …`, production is only up because of something else, so check the Log stream for `uvicorn: not found`. Change the setting as part of this task.

- [ ] **Step 2: Reproduce in the sandbox.**

  ```bash
  AZURE_STARTUP_COMMAND="uvicorn app.main:app --host 0.0.0.0 --port 8000" ./sandbox/sandbox.sh up
  docker logs njdot-sandbox-backend-1 2>&1 | tail -3
  ```

  Expected: `/opt/startup/startup.sh: 25: uvicorn: not found`, and `up` fails its health wait.

- [ ] **Step 3: Fix the notes.** Replace the comment block at lines 18–21 with:

  ```yaml
  #   2. Azure Portal -> NJDOT -> Configuration -> General settings ->
  #      Startup Command:
  #        python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  #      NOT the bare "uvicorn ...": Oryx only puts the antenv virtualenv on
  #      PYTHONPATH, never its bin/ on PATH, and the runtime image has no
  #      global uvicorn, so the bare command exits "uvicorn: not found".
  ```

- [ ] **Step 4: Set the same value in the portal.** Restart the Web App and confirm `GET https://njdot-f2f6a4baaedyhbfx.eastus-01.azurewebsites.net/health` returns `{"status":"ok"}`.

- [ ] **Step 5: Verify in the sandbox with the default command.**

  ```bash
  ./sandbox/sandbox.sh up
  curl -s localhost:8000/health
  ```

  Expected: `{"status":"ok"}`.

- [ ] **Step 6: Commit.** `fix(deploy): document a startup command the App Service runtime can run`

---

### Task 2: Require project ownership on the session (Document Q&A) endpoints

**Why:** `backend/app/api/session.py` never reads `Authorization`. With only a project UUID, anyone can:
- `GET /messages/{id}`: read the chat history
- `POST /query`: query the private documents
- `GET /status/{id}`: watch progress
- `POST /upload` with `project_id`: re-trigger ingestion

**Test that proves it:** `sandbox/e2e/tests/04-backend-api.spec.ts › @security session endpoints should require auth` (currently red).

**Rule** (identical to `review_status` today):
- Owner exists and no token → 401.
- Owner exists and the token belongs to someone else → 403.
- No owner (anonymous review, or a standalone session) → allowed.

**Files:**
- Create: `backend/app/project_access.py`
- Modify: `backend/app/api/session.py` (the upload, status, query and messages handlers at lines ~403, 463, 773, 919)
- Modify: `frontend/src/components/review/SessionChat.tsx:174-192` (the SSE URL carries `?token=`)
- Create: `backend/tests/test_session_auth.py`

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_session_auth.py`:

  ```python
  """Ownership checks on /api/session/* (Task 2 of 2026-09-26 plan)."""
  from __future__ import annotations
  import asyncio, sys
  from pathlib import Path
  from unittest.mock import patch
  import pytest
  from fastapi import HTTPException

  _ROOT = Path(__file__).resolve().parent.parent
  if str(_ROOT) not in sys.path:
      sys.path.insert(0, str(_ROOT))

  from app import project_access  # noqa: E402
  from app.api.session import get_session_messages  # noqa: E402

  OWNER, OTHER, PID = "owner-uuid", "other-uuid", "11111111-1111-1111-1111-111111111111"

  def _run(coro):
      return asyncio.run(coro)

  def test_owned_project_without_token_is_401():
      with patch.object(project_access, "project_owner", return_value=OWNER):
          with pytest.raises(HTTPException) as e:
              project_access.require_project_access(PID, None)
      assert e.value.status_code == 401

  def test_owned_project_other_user_is_403():
      with patch.object(project_access, "project_owner", return_value=OWNER), \
           patch.object(project_access, "user_id_from_token", return_value=OTHER):
          with pytest.raises(HTTPException) as e:
              project_access.require_project_access(PID, "Bearer x")
      assert e.value.status_code == 403

  def test_owner_is_allowed():
      with patch.object(project_access, "project_owner", return_value=OWNER), \
           patch.object(project_access, "user_id_from_token", return_value=OWNER):
          assert project_access.require_project_access(PID, "Bearer x") == OWNER

  def test_anonymous_project_stays_open():
      with patch.object(project_access, "project_owner", return_value=None):
          assert project_access.require_project_access(PID, None) is None

  def test_messages_endpoint_enforces_access():
      with patch.object(project_access, "project_owner", return_value=OWNER):
          with pytest.raises(HTTPException) as e:
              _run(get_session_messages(PID, authorization=None))
      assert e.value.status_code == 401
  ```

- [ ] **Step 2: Run them and watch them fail.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests/test_session_auth.py -q
  ```

  Expected: `ModuleNotFoundError: app.project_access`.

- [ ] **Step 3: Create `backend/app/project_access.py`.**

  ```python
  """One ownership rule for everything keyed by a review project id.

  A project with an owner (signed-in review) is readable only by that owner.
  A project with no owner (the signed-out review flow, or a standalone
  session upload) stays open — the same rule review_status has always used.
  """
  from __future__ import annotations

  import logging
  from typing import Optional

  from fastapi import HTTPException

  from app.auth import user_id_from_token
  from app.database import get_db

  logger = logging.getLogger(__name__)


  def project_owner(project_id: str) -> Optional[str]:
      """Owner user_id, from the in-process review store first (a review that
      just finished isn't in review_projects yet — the frontend inserts that
      row after /api/session/upload), then from review_projects."""
      from app.api.review import _review_progress  # lazy: avoid import cycle

      owner = (_review_progress.get(project_id) or {}).get("user_id")
      if owner:
          return owner
      try:
          rows = (
              get_db().table("review_projects").select("user_id")
              .eq("id", project_id).limit(1).execute().data
          ) or []
      except Exception as exc:
          # Fail closed: an unreachable DB must not turn into open access.
          logger.error("Ownership lookup for project_id=%s failed: %s", project_id, exc, exc_info=True)
          raise HTTPException(status_code=503, detail="Could not verify project access.") from exc
      return rows[0].get("user_id") if rows else None


  def require_project_access(project_id: str, authorization: Optional[str]) -> Optional[str]:
      """Raise 401/403 unless the caller may use this project. Returns the
      caller's user id (None for an anonymous project)."""
      owner = project_owner(project_id)
      if owner is None:
          return None
      caller = user_id_from_token(authorization)  # raises 401 on missing/invalid
      if caller != owner:
          raise HTTPException(status_code=403, detail="This project does not belong to you")
      return caller
  ```

- [ ] **Step 4: Wire it into `session.py`.** Add `Header` and `Query` to the fastapi import, and `from app.project_access import require_project_access`. Then change each handler:

  - `upload_session`: add `authorization: Optional[str] = Header(default=None)`. At the top of the `if project_id:` branch, call `require_project_access(project_id, authorization)`.
  - `session_status(session_id: str, token: Optional[str] = Query(default=None))`:
    - Call `require_project_access(session_id, f"Bearer {token}" if token else None)` before building the `StreamingResponse`.
    - The token comes as a query parameter because EventSource can't send headers, the same as `review_status`.
  - `session_query(req: QueryRequest, authorization: Optional[str] = Header(default=None))`: call `require_project_access(req.session_id, authorization)` first.
  - `get_session_messages(session_id: str, authorization: Optional[str] = Header(default=None))`: call `require_project_access(session_id, authorization)` first.

- [ ] **Step 5: Frontend.** In `SessionChat.tsx`, make the SSE effect depend on the token and pass it along:

  ```ts
  useEffect(() => {
    if (!sessionId) return
    const qs  = authToken ? `?token=${encodeURIComponent(authToken)}` : ''
    const es  = new EventSource(`${apiBase}/api/session/status/${sessionId}${qs}`)
    // ...unchanged handlers...
    return () => es.close()
  }, [sessionId, apiBase, authToken])
  ```

  The token arrives asynchronously from `DocumentReview`'s `getSession()`. If the first attempt (without a token) gets a 401, `onerror` closes it, and the effect re-opens with the token. `messages` and `query` already send `authHeaders(...)`. Confirm that `DocumentReview.tsx`'s `/api/session/upload` call sends the Bearer header when signed in; it does today.

- [ ] **Step 6: Run the tests.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests -q
  ```

  Expected: all pass, including the 5 new tests.

- [ ] **Step 7: Run it in the sandbox.**

  ```bash
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test -g "session endpoints|upload → review"
  ```

  Expected:
  - `@security session endpoints should require auth` turns green.
  - The full review test, which includes Document Q&A, stays green.

- [ ] **Step 8: Commit.** `fix(session): require project ownership on Document Q&A endpoints`

---

### Task 3: Let users delete and update their own conversations

**Why:** `migrations/001_conversations.sql` defines only SELECT and INSERT policies on `conversations`. Two frontend writes that run under the user's session are silently dropped: PostgREST answers 204 and changes 0 rows.
- `ChatInterface.tsx:204` (delete)
- `ChatInterface.tsx:271` (`updated_at` bump)

The sandbox confirmed every "Delete me" row is still in the database. Deleted chats reappear, and Recents never re-orders.

**Test that proves it:** `02-chat.spec.ts › delete a conversation removes it (and it stays gone after reload)` (currently red).

**Files:**
- Create: `backend/migrations/011_conversations_update_delete_policies.sql`
- Modify: `sandbox/sandbox.sh` (`cmd_db_apply` also applies `backend/migrations/011_*.sql`; see Step 3)

- [ ] **Step 1: Write the migration.**

  ```sql
  -- 011: conversations need UPDATE and DELETE policies.
  -- 001 only granted SELECT/INSERT, so the frontend's delete
  -- (ChatInterface.tsx handleDeleteConversation) and its updated_at bump
  -- after each message matched 0 rows under RLS and silently did nothing.
  do $$
  begin
    if not exists (select 1 from pg_policies where tablename = 'conversations' and policyname = 'conversations_update') then
      create policy "conversations_update" on conversations
        for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'conversations' and policyname = 'conversations_delete') then
      create policy "conversations_delete" on conversations
        for delete using (auth.uid() = user_id);
    end if;
  end $$;
  ```

  Messages go with the conversation through `on delete cascade`, so `messages` needs no DELETE policy.

- [ ] **Step 2: Confirm the failure before applying it.**

  ```bash
  ./sandbox/sandbox.sh test -g "delete a conversation"
  ```

  Expected: FAIL, "conversation came back after reload".

- [ ] **Step 3: Apply it in the sandbox.** In `sandbox/sandbox.sh` `cmd_db_apply`, after the "conversations missing" block, add a loop over `backend/migrations/*.sql` that applies every file whose numeric prefix is ≥ 011. They're idempotent, so re-running is safe. Then run:

  ```bash
  ./sandbox/sandbox.sh db-apply && ./sandbox/sandbox.sh test -g "delete a conversation|ask with collection filter"
  ```

  Expected: both PASS.

- [ ] **Step 4: Apply it to the live Supabase.** Paste the file into Dashboard → SQL Editor and run it. Confirm with `select policyname, cmd from pg_policies where tablename = 'conversations';`, which should list 4 policies.

- [ ] **Step 5: Commit.** `fix(db): add update/delete RLS policies on conversations`

---

### Task 4: Changing your password must not log you out

**Why:** `/update-password` calls `POST /api/auth/change-password`, which updates the password through the Supabase **admin** API. GoTrue then revokes the user's sessions, so the next `getUser()` in the middleware gets `403 session_not_found` and the user lands on `/login`, despite the "Redirecting you back…" message. Calling `supabase.auth.updateUser({ password })` from the browser changes the password and keeps the current session.

**Test that proves it:** `01-landing-auth.spec.ts › change password from the user menu` (currently red; it expects `/chat`).

**Files:**
- Modify: `frontend/src/app/update-password/page.tsx:41-63`
- Modify: `frontend/src/lib/api.ts` (remove `changePassword`)
- Modify: `backend/app/api/auth.py` (remove `/api/auth/change-password`, `ChangePasswordBody`)
- Modify: `sandbox/e2e/tests/01-landing-auth.spec.ts` (the test asserted the old endpoint; see Step 4)
- Modify: `sandbox/e2e/tests/04-backend-api.spec.ts` (drop `change-password needs a token` and the `/api/auth/change-password` entry in the OpenAPI route list)

- [ ] **Step 1: Confirm the failure.**

  ```bash
  ./sandbox/sandbox.sh test -g "change password"
  ```

  Expected: FAIL, received `/login`.

- [ ] **Step 2: Replace the submit handler** in `update-password/page.tsx`:

  ```tsx
  setIsLoading(true)
  try {
    const supabase = createClient()
    const { error: updateError } = await supabase.auth.updateUser({ password })
    if (updateError) throw updateError
    setStage('success')
    setTimeout(() => router.push('/chat'), 2000)
  } catch (err) {
    setError(err instanceof Error ? err.message : 'Password update failed. Please try again.')
  } finally {
    setIsLoading(false)
  }
  ```

  Also remove the `accessToken` state and the `changePassword` import. The `getSession()` check on mount stays as it is.

- [ ] **Step 3: Delete the backend endpoint.** Remove `/api/auth/change-password` and `ChangePasswordBody` from `backend/app/api/auth.py`, and `changePassword` from `api.ts`. It no longer has callers, and it's the path that revokes sessions.

- [ ] **Step 4: Update the E2E assertions** that referenced the endpoint.
  - In `01-landing-auth.spec.ts`, replace `const call = await monitor.call("/api/auth/change-password", "POST"); expect(call.status).toBe(200);` with:

    ```ts
    const call = await monitor.call(/\/auth\/v1\/user$/, "PUT")
    expect(call.status).toBe(200)
    ```

    Also delete the `monitor.allow(/auth\/v1\/user -> 403/)` line, since that 403 must no longer happen.
  - In `04-backend-api.spec.ts`, remove the `change-password needs a token` test, and remove `/api/auth/change-password` from the OpenAPI route list.

- [ ] **Step 5: Verify.**

  ```bash
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test -g "change password|password flows|login"
  ```

  Expected: all green, and the user ends on `/chat`.

- [ ] **Step 6: Check the hosted setting.** In Dashboard → Authentication → Providers → Email, if **Secure password change** is ON, `updateUser` needs a recent login and returns `reauthentication_needed` for older sessions. Either leave it OFF, or surface that error message; the `catch` above already displays it.

- [ ] **Step 7: Commit.** `fix(auth): change password via updateUser so the session survives`

---

### Task 5: Password reset must prove email ownership

**Why:** `POST /api/auth/request-reset` returns a working `reset_token` to whoever asks, and nothing is emailed. Anyone who knows an address can take over that account; the sandbox did exactly that. The fix uses Supabase's own recovery flow:
- Supabase emails a one-time link.
- The link returns through `/auth/confirm`, which turns it into a recovery session.
- `/update-password` then works as in Task 4.

**Prerequisite:** Decision 1 above (SMTP for hosted Supabase).

**Files:**
- Create: `frontend/src/app/auth/confirm/route.ts`
- Modify: `frontend/src/components/auth/ForgotPasswordForm.tsx` (collapse the two stages into "send link" and "check your email")
- Modify: `frontend/src/lib/api.ts` (remove `requestPasswordReset`, `resetPassword`, `_authPost` if unused)
- Modify: `backend/app/api/auth.py` (remove `/request-reset`, `/reset-password`; the module becomes empty, so remove the router from `main.py`)
- Modify: `sandbox/e2e/tests/01-landing-auth.spec.ts` (forgot-password tests read the email from Mailpit)
- Modify: `sandbox/e2e/tests/04-backend-api.spec.ts` (remove the `reset-password rejects a forged token` test and the two reset routes from the OpenAPI list)

- [ ] **Step 1: Add the confirm route.** This is the Supabase SSR pattern for email OTP links.

  ```ts
  // frontend/src/app/auth/confirm/route.ts
  import { type EmailOtpType } from '@supabase/supabase-js'
  import { NextResponse, type NextRequest } from 'next/server'
  import { createClient } from '@/lib/supabase/server'

  export async function GET(request: NextRequest) {
    const { searchParams } = new URL(request.url)
    const token_hash = searchParams.get('token_hash')
    const type = searchParams.get('type') as EmailOtpType | null
    const next = searchParams.get('next') ?? '/update-password'
    const safeNext = next.startsWith('/') && !next.startsWith('//') ? next : '/update-password'

    if (token_hash && type) {
      const supabase = await createClient()
      const { error } = await supabase.auth.verifyOtp({ type, token_hash })
      if (!error) return NextResponse.redirect(new URL(safeNext, request.url))
    }
    return NextResponse.redirect(new URL('/forgot-password?error=link', request.url))
  }
  ```

  Check `frontend/src/lib/supabase/server.ts` for the exact export name and whether it is `async`, and match it.

- [ ] **Step 2: Rewrite `ForgotPasswordForm` submit.**

  ```ts
  const supabase = createClient()
  const { error } = await supabase.auth.resetPasswordForEmail(email, {
    redirectTo: `${window.location.origin}/auth/confirm?next=/update-password`,
  })
  // Always show the same message, whether or not the address exists.
  setStage('sent')   // "If an account exists for that email, we've sent a reset link."
  ```

  Delete the `password` stage, its fields and "Start over". If `?error=link` is present, show "That reset link is invalid or has expired. Request a new one."

- [ ] **Step 3: Configure the recovery email template to use `token_hash`.**
  - **Hosted:** Dashboard → Authentication → Emails → Reset Password. Set the link to `{{ .SiteURL }}/auth/confirm?token_hash={{ .TokenHash }}&type=recovery&next=/update-password`.
  - **Local:** set the same in `supabase/config.toml` under `[auth.email.template.recovery]`.
  - **Redirect URLs:** add the Vercel production URL and `http://localhost:3000/**` under Authentication → URL Configuration.

- [ ] **Step 4: Remove the backend reset endpoints** and `requestPasswordReset`/`resetPassword` from `api.ts`. Remove `auth_router` from `backend/app/main.py` if `auth.py` has no routes left, then delete the file.

- [ ] **Step 5: Rewrite the forgot-password E2E tests** to read the link from Mailpit (`GET http://127.0.0.1:54324/api/v1/search?query=to:<email>`, then `GET /api/v1/message/<ID>` and take the first `/auth/confirm?...` URL from `.Text`). The flow becomes:
  1. Request the link.
  2. Open it.
  3. Land on `/update-password`.
  4. Set the password.
  5. Land on `/chat`.
  6. Log out.
  7. Log in with the new password.

  The "unknown email" test asserts that the same "If an account exists…" message is shown, and that no mail arrives.
  - **Sandbox:** add `MAILPIT_URL=http://127.0.0.1:54324` to `sandbox.env.example`.
  - **Test annotation:** remove the `security` annotation from the old test.

- [ ] **Step 6: Verify.**

  ```bash
  ./sandbox/sandbox.sh test -g "password"
  ```

  Expected: all password tests green. Also run `curl -s -X POST localhost:8000/api/auth/request-reset`, which should now return 404.

- [ ] **Step 7: Commit.** `fix(auth): use emailed Supabase recovery links instead of returning reset tokens`

---

### Task 6: Rename `middleware.ts` to `proxy.ts` (Next 16)

**Why:** `next build` warns: *The "middleware" file convention is deprecated. Please use "proxy" instead.*

**Files:**
- Rename: `frontend/src/middleware.ts` → `frontend/src/proxy.ts`

- [ ] **Step 1:** `git mv frontend/src/middleware.ts frontend/src/proxy.ts`. Rename the exported function from `middleware` to `proxy`, keep `export const config` unchanged, and update the header comment. Also update the one comment in `sandbox/frontend/Dockerfile` that names `middleware.ts`.

- [ ] **Step 2:** Run `./sandbox/sandbox.sh up && ./sandbox/sandbox.sh checks`, then `grep -i middleware sandbox/logs/static-next-build.log`. Expected: no deprecation warning, and the build still lists `ƒ Proxy (Middleware)`.

- [ ] **Step 3:** Run `./sandbox/sandbox.sh test -g "route protection|login"`. Expected: all redirects still green.

- [ ] **Step 4: Commit.** `chore(frontend): rename middleware.ts to proxy.ts for Next 16`

---

### Task 7: Don't leak exception text from `/api/query`

**Why:** When the LLM fails, clients receive `500 {"detail":"Pipeline error [RuntimeError]: LLM completion failed [InternalServerError]: Error code: 500 - {...}"}`, which includes the internal exception class and the upstream provider body. The full detail already goes to the server log ([query.py:270](../../../backend/app/api/query.py:270)).

**Files:**
- Modify: `backend/app/api/query.py:269-274`
- Create: `backend/tests/test_query_error_detail.py`

- [ ] **Step 1: Write the failing test.** Patch the module-level `_llm.complete` to raise `RuntimeError("secret upstream body")`, and patch the retrieval step to return one fake chunk (follow `tests/test_chat_similarity_floor.py` for how the query pipeline is stubbed). Call `query_endpoint(QueryRequest(query="x"))` and assert:
  - `HTTPException.status_code == 500`
  - `"secret upstream body" not in exc.detail`
  - `exc.detail == "The assistant could not answer right now. Please try again."`

- [ ] **Step 2:** Run it and confirm it FAILS: the detail contains the secret.

- [ ] **Step 3: Fix.**

  ```python
  except Exception as exc:                              # noqa: BLE001
      logger.exception("Pipeline error for query=%r collection=%r", query, collection)
      raise HTTPException(
          status_code=500,
          detail="The assistant could not answer right now. Please try again.",
      ) from exc
  ```

  Apply the same change to the matching `debug_endpoint` handler further down the file, if it formats `exc` into `detail`.

- [ ] **Step 4:** Run `pytest backend/tests -q`, then `./sandbox/sandbox.sh test -g "OpenAI outage|red error bubble"`. Expected: green. The error bubble test mocks its own message, so it's unaffected.

- [ ] **Step 5: Commit.** `fix(query): return a generic 500 detail; keep specifics in the log`

---

### Task 8: Commit the schema for the tables git doesn't have

**Why:** `review_projects`, `compliance_checks`, `session_messages`, `bdc_section_map`, `rate_limits` and the public `pdfs` bucket are used by the code but have no DDL in the repo. Migration 009's header says 003–006 and 008 never got committed. A fresh Supabase can't be rebuilt from git. `sandbox/supabase/schema.sql` reconstructs them, and the whole app ran end to end against it.

**Files:**
- Create: `backend/migrations/012_baseline_untracked_tables.sql`
- Modify: `sandbox/supabase/schema.sql` (reduce to a pointer; see Step 4)

- [ ] **Step 1: Diff against the real thing first.** In the live project's SQL editor, run:

  ```sql
  select table_name, column_name, data_type, is_nullable, column_default
  from information_schema.columns
  where table_schema = 'public'
    and table_name in ('review_projects','compliance_checks','session_messages','bdc_section_map','rate_limits')
  order by table_name, ordinal_position;
  select tablename, policyname, cmd, qual, with_check from pg_policies
  where tablename in ('review_projects','compliance_checks');
  select id, public from storage.buckets;
  ```

  Save the output in the PR. Wherever live differs from `sandbox/supabase/schema.sql` (types, defaults, extra columns, policy names), **live wins**. The migration must describe production, not the reconstruction.

- [ ] **Step 2: Write `012_baseline_untracked_tables.sql`.** Start from the tables, indexes, RLS, policies and bucket sections of `sandbox/supabase/schema.sql`, corrected to match Step 1. Keep every statement additive (`if not exists`, guarded policies, `on conflict do nothing`) so running it on production is a no-op. Head it with a comment explaining why it exists.

- [ ] **Step 3: Prove it rebuilds from scratch.** `supabase db reset` wipes the local database, so run this on a throwaway local project, not yours.
  1. Reset: `supabase db reset`.
  2. Apply `backend/sql/local_setup.sql`, then migrations `001`, `002`, `009`, `010`, `011`, `012` in order.
  3. Run `./sandbox/sandbox.sh doctor`. Expected: every object ✓.
  4. Run `./sandbox/sandbox.sh seed && ./sandbox/sandbox.sh test`. Expected: nothing fails because of schema.

- [ ] **Step 4: Point the sandbox at the migration.**
  - Replace the table and bucket body of `sandbox/supabase/schema.sql` with a comment that points to `backend/migrations/012_*`.
  - Make `cmd_db_apply` apply migrations 011 and 012 (the loop from Task 3 already covers this).

- [ ] **Step 5: Commit.** `feat(db): commit baseline DDL for tables that only existed in the live database`

---

### Task 9: Stop Langfuse from logging a warning on every LLM call

**Why:** With `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` unset, each `CallbackHandler(...)` logs `Authentication error: Langfuse client initialized without public_key` at WARNING. That was 127 lines in one sandbox run, burying real warnings in the Azure log stream.

**Files:**
- Modify: `backend/app/observability.py`
- Create: `backend/tests/test_observability_quiet.py`

- [ ] **Step 1: Write the failing test.** With both env vars removed (`monkeypatch.delenv`), capture logs using `tests/logcapture.py`. Call `get_langfuse_handler()`, `new_trace_id("x")` and `get_langfuse_client()`. Assert that all three return `None` and that **zero** records at WARNING or above were emitted, including from the `langfuse` logger.

- [ ] **Step 2:** Run it and confirm it FAILS: the handler is built and langfuse warns.

- [ ] **Step 3: Fix.** Add a single gate that the three functions check first:

  ```python
  import os

  def _langfuse_configured() -> bool:
      return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
  ```

  - Put `if not _langfuse_configured(): return None` at the top of `get_langfuse_client`, `new_trace_id` and `get_langfuse_handler`.
  - Log one INFO line, once per process, saying tracing is off. Use a module-level flag.
  - Leave the existing `except` branches alone. Those are real failures when tracing *is* configured.

- [ ] **Step 4:** Run `pytest backend/tests -q`, then `./sandbox/sandbox.sh test` and `grep -c "langfuse" sandbox/logs/backend.log`. Expected: 0 or 1 (the INFO line).

- [ ] **Step 5: Commit.** `fix(observability): skip Langfuse entirely when it isn't configured`

---

### Task 10: Silence Neo4j "does not exist yet" notifications

**Why:** The graph queries reference relationship types and properties that only exist once a project has that data (`CONSTRAINED_BY`, `COVERED_BY`, `MENTIONS`, `confidence`, and so on). The server returns `01N51`/`01N52` UNRECOGNIZED notifications, which the driver logs as WARNING on every review. They're harmless and noisy.

**Files:**
- Modify: `backend/app/neo4j_client.py:26-40`

- [ ] **Step 1: Check the API.** Run this inside the backend image:

  ```bash
  docker run --rm --entrypoint sh njdot-sandbox-backend -c \
    "grep -n 'driver_config' antenv/lib/python3.13/site-packages/langchain_neo4j/graphs/neo4j_graph.py; \
     grep -rn 'notifications_disabled_classifications' antenv/lib/python3.13/site-packages/neo4j/_conf.py"
  ```

  This confirms that `Neo4jGraph` accepts `driver_config`, and that the driver supports `notifications_disabled_classifications`. If `driver_config` is missing, build the `GraphDatabase.driver(...)` yourself and pass it in, following the langchain-neo4j 0.10 constructor.

- [ ] **Step 2: Fix.** Pass this to the `Neo4jGraph(...)` constructor:

  ```python
  driver_config={
      # 01N51/01N52 ("relationship type / property does not exist") fire on
      # every review for graph features a project has no data for yet —
      # expected, not actionable. Other notification classes still log.
      "notifications_disabled_classifications": ["UNRECOGNIZED"],
  },
  ```

- [ ] **Step 3:** Run `./sandbox/sandbox.sh test -g "upload → review"`, then `grep -c "neo4j.notifications" sandbox/logs/backend.log`. Expected: 0. The review test must stay green.

- [ ] **Step 4: Commit.** `fix(neo4j): disable UNRECOGNIZED notifications for not-yet-created graph types`

---

### Task 11: Don't write the string `"None"` for missing BDC dates

**Why:** `scripts/ingest_bdc.py:283-284` stores `str(header["bdc_date"])` and `str(header["effective_date"])`. When `_parse_date` returns `None`, that becomes the literal string `"None"`. A `date`-typed column rejects the insert, and a text column sorts it wrongly in `bdc_matcher` (`order by bdc_date`).

**Files:**
- Modify: `backend/scripts/ingest_bdc.py:283-284`
- Create: `backend/tests/test_bdc_dates.py`

- [ ] **Step 1: Write the failing test.** Extract a helper `_iso(d: date | None) -> str | None`, and test that `_iso(None) is None` and `_iso(date(2025, 3, 1)) == "2025-03-01"`. Until the helper exists, the test fails on import.

- [ ] **Step 2: Fix.**

  ```python
  def _iso(d):
      return d.isoformat() if d else None
  ...
  "bdc_date":       _iso(header["bdc_date"]),
  "effective_date": _iso(header["effective_date"]),
  ```

- [ ] **Step 3:** Run `pytest backend/tests/test_bdc_dates.py -q`. Expected: PASS.

- [ ] **Step 4: Clean existing data.** Run on live, and record the affected row count in the PR:

  ```sql
  update bdc_section_map set bdc_date = null where bdc_date = 'None';
  update bdc_section_map set effective_date = null where effective_date = 'None';
  ```

  Skip this if Step 1 of Task 8 shows these columns are `date`, since then no `"None"` rows can exist.

- [ ] **Step 5: Commit.** `fix(ingest_bdc): store missing BDC dates as NULL, not "None"`

---

### Task 12: Lint, pytest warning, and docs

**Why:** These are the remaining items from the report:
- 7 ESLint `no-explicit-any` errors
- 2 unused variables
- a `PytestReturnNotNoneWarning`
- APOC isn't listed as a prerequisite anywhere, even though every review fails without it

**Files:**
- Modify: `frontend/src/lib/types.ts:37`, `frontend/src/components/review/DocumentReview.tsx:55,59,190,472`, `frontend/src/components/review/SessionChat.tsx:207`, `frontend/src/components/chat/ChatInterface.tsx:936`
- Modify: `backend/scripts/test_db.py`
- Modify: `README.md`

- [ ] **Step 1: Fix the ESLint `any`s** with the narrowest honest types:
  - `types.ts:37` `review_result: any`: `review_result: ReviewResult | null`. Move the `ReviewResult` interface from `DocumentReview.tsx` into `types.ts` and import it back.
  - `DocumentReview.tsx:55,59`: `key_map?: { extraction: Record<string, unknown>; region: Record<string, unknown> } | null`, and the same shape for `estimate` with `cost_gap`.
  - `DocumentReview.tsx:472` `didParseCell: (data: any)`: `(data: CellHookData)`, imported from `jspdf-autotable`.
  - `SessionChat.tsx:207` `(m: any)`: `(m: { role: 'user' | 'assistant'; content: string; sources?: Source[] })`, using the file's existing source type.
  - Unused `authToken` (`DocumentReview.tsx:190`, `CitationPill` props): remove it from the destructure and the prop type, then fix the one call site.
  - Unused `index` (`ChatInterface.tsx:936`, `CitationCard`): same treatment.

- [ ] **Step 2:** Run `./sandbox/sandbox.sh up && ./sandbox/sandbox.sh checks`, then `cat sandbox/logs/static-eslint.log`. Expected: `✖ 0 problems`, or no output. `next build` must still pass.

- [ ] **Step 3: `test_db.py`.** Pytest collects `scripts/test_db.py::test_connection` because of its name, and it returns a bool. Rename the function to `check_connection` and update its `__main__` caller, so pytest stops collecting it. Keep the script's behaviour when run directly.

- [ ] **Step 4: README.** Under Prerequisites, change the Neo4j bullet to say that the Neo4j instance **must have APOC** (Aura includes it; for Desktop, install the APOC plugin; for Docker, set `NEO4J_PLUGINS='["apoc"]'`), because `langchain-neo4j` calls `apoc.meta.data()` and every review fails without it. Add a line pointing to `sandbox/README.md` for the production-parity sandbox.

- [ ] **Step 5:** Run `./sandbox/sandbox.sh test`. Expected: `static-pytest.log` ends `N passed` with no warnings summary.

- [ ] **Step 6: Commit.** `chore: fix lint errors, pytest collection warning, document APOC requirement`

---

## Final verification

- [ ] Rebuild and run everything from clean:

  ```bash
  ./sandbox/sandbox.sh down
  ./sandbox/sandbox.sh db-apply
  ./sandbox/sandbox.sh up
  ./sandbox/sandbox.sh seed
  ./sandbox/sandbox.sh test
  ```

- [ ] Open `sandbox/logs/REPORT.md`. Expected:
  - **Tests:** 37 passed, 0 failed. The count may shift by the tests Tasks 4–5 removed or rewrote; none may fail.
  - **Browser errors:** none.
  - **Backend tracebacks:** only the intentional OpenAI-outage test.
  - **Backend warnings:** no `langfuse` or `neo4j.notifications` lines.
  - **Static checks:** 0 errors; the only warning is `next build: No build cache found`, which is a sandbox artifact.
- [ ] Replace `sandbox/FINDINGS.md` with a short "all resolved on <date>" note linking this plan. Commit the new report as `sandbox/reports/<date>-REPORT.md`.
- [ ] Production follow-through that git can't do:
  - Azure Startup Command (Task 1).
  - Migrations 011 and 012 run on the live Supabase (Tasks 3 and 8).
  - SMTP, recovery template and redirect URLs configured (Task 5).
  - `bdc_section_map` cleanup (Task 11).
