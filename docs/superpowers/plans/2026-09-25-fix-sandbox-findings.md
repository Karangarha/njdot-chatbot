# Fix Sandbox Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every error and warning the production-parity sandbox found on 2026-09-25, except the deferred password-reset finding. Success means:
- `./sandbox/sandbox.sh test` shows every E2E test passing.
- The report has no Langfuse or Neo4j noise.
- The deploy notes no longer describe a startup command that crashes.

**Architecture:** No new subsystems. Each task is a targeted fix at the line the sandbox pointed to, plus the test that proves it:
- **Backend fixes:** each gets a test in `backend/tests/`, in the repo's style. Every file runs both under pytest and as a plain script. Tests use `unittest.mock.patch`, not pytest fixtures, and check for `HTTPException` with `try/except`.
- **Frontend fixes:** Next.js has no unit-test harness here, so each one is proven by the sandbox E2E test that currently fails.
- **Database fixes:** each ships as a numbered migration in `backend/migrations/`, and the sandbox applies it through `db-apply`.

**Tech Stack:** Python 3.11+ (App Service runs 3.13), FastAPI, supabase-py, PyJWT, langchain-neo4j 0.10, the neo4j driver, langfuse, pytest · Next.js 16.1, @supabase/ssr, supabase-js · Playwright (`sandbox/e2e`), Docker, Supabase CLI.

**Spec:** `sandbox/FINDINGS.md` and `sandbox/reports/2026-09-25-REPORT.md`. Both live on `origin/claude/ecstatic-knuth-8ib321`, and Task 1 Step 0 brings them onto the work branch.

**Supersedes:** `docs/superpowers/plans/2026-09-26-fix-sandbox-findings.md` (the cloud draft). This plan changes it in these ways:
- Task 2 now matches the user's decision to require sign-in for Q&A.
- The password-reset task is dropped (deferred).
- The Neo4j fix now keeps `liveness_check_timeout`.
- Every test is written out in full.
- It adds a Review Focus section.

## Decisions already made (2026-09-25, by the user)

1. **Document Q&A requires sign-in.** All four `/api/session/*` endpoints return 401 without a valid token and 403 when a signed-in user asks about someone else's project.
   - **UI impact: none.** `DocumentReview` renders only inside `/chat` (`ChatInterface.tsx:582`), and `middleware.ts` already sends signed-out visitors away from `/chat`. So every real Q&A user already has a token.
   - **What stays open:** the anonymous `POST /api/review` API flow still works. Only its Q&A needs a token now.
2. **Password reset (finding 5) stays deferred.** Do not touch `/api/auth/request-reset`, `/api/auth/reset-password`, `ForgotPasswordForm.tsx`, `requestPasswordReset`/`resetPassword`/`_authPost` in `api.ts`, or `_update_user_password` in `auth.py`. The E2E test that demonstrates the takeover stays as it is.

## Global Constraints

- **Work branch:** `fix/sandbox-findings`, cut from `custom-error-logs` with `origin/claude/ecstatic-knuth-8ib321` merged in (Task 1 Step 0). The `sandbox/` tooling exists only on that cloud branch. Its backend/frontend code is identical to `custom-error-logs`, so the merge only adds files.
- **Backend Python:** run it from the repo root with `.venv/Scripts/python.exe`. Test command: `.venv/Scripts/python.exe -m pytest backend/tests -q`.
- **Sandbox commands:** run them from Git Bash, with Docker Desktop running. The full check:
  1. `./sandbox/sandbox.sh up`. It rebuilds when backend or frontend code changed.
  2. `./sandbox/sandbox.sh test`.
  3. Confirm the task's target test turned green and nothing else regressed.
- **Never weaken an E2E assertion to make it pass.** The only E2E edits allowed are the ones this plan spells out: Task 4's, which follow removing an endpoint. If any other test looks wrong, stop and raise it.
- **Never log tokens, passwords or query strings.** This carries over from the 2026-09-15 logging plan.
- **Migrations are additive:** guarded `create policy`, `if not exists`, `on conflict do nothing`. They must be safe to run on the live Supabase, which may already have some of these objects.
- **New backend files need no `.gitignore` edits.** `backend/.gitignore` already un-ignores `app/*.py`, `tests/*`, `migrations/*` and `scripts/*`. Still, run `git status` before each commit and confirm the new files show up.
- **Commits:** one per task, with message `fix(<area>): <what>` and this attribution line:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Review Focus

These are the inputs most likely to hurt a real user that the findings don't directly test. Each one is pinned by a test in the task that owns the code.

1. **A signed-in user opens their own review's Document Q&A.** Progress, history and answers must all still work. Don't let the new auth give them a 401 on the SSE stream because the token arrives after the stream opens. Pinned by Task 2 Step 6 (SessionChat waits for the token) and the existing 03-review E2E Q&A step.
2. **User B opens user A's project UUID.** They must get 403, not A's chat history or answers from A's documents. Pinned by `test_other_users_project_is_403` in Task 2.
3. **An expired or garbage token.** It must get 401, not 500 and not open access. Pinned by `test_invalid_token_is_401` in Task 2.
4. **Supabase is unreachable during the ownership lookup.** The request must fail closed with 503, not treat the project as ownerless. Pinned by `test_db_down_fails_closed` in Task 2.
5. **Neo4j idle-connection handling survives the notification fix.** `liveness_check_timeout: 60` fixed a real production `SessionExpired` and must stay. Pinned by `test_driver_config_keeps_liveness_check` in Task 9.

(A sixth, for Task 8: with Langfuse keys set, tracing must still turn on. It's pinned by `test_configured_langfuse_still_builds_handler`.)

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `backend/app/project_access.py` | `require_project_access(project_id, authorization) -> str`. This is the one sign-in + ownership rule for `/api/session/*`. It's a separate module so `session.py` doesn't import `review.py` at load. |
| `backend/tests/test_session_auth.py` | Task 2 |
| `backend/tests/test_query_error_detail.py` | Task 6 |
| `backend/tests/test_observability_quiet.py` | Task 8 |
| `backend/tests/test_neo4j_driver_config.py` | Task 9 |
| `backend/tests/test_bdc_dates.py` | Task 10 |
| `backend/migrations/011_conversations_update_delete_policies.sql` | Task 3 |
| `backend/migrations/012_baseline_untracked_tables.sql` | Task 7 |

**Modified:**
- **Deploy:** `.github/workflows/azure-deploy-backend.yml` (Task 1)
- **Backend:**
  - `backend/app/api/session.py` (Task 2)
  - `backend/app/api/query.py` (Task 6)
  - `backend/app/api/auth.py` (Task 4)
  - `backend/app/observability.py` (Task 8)
  - `backend/app/neo4j_client.py` (Task 9)
  - `backend/scripts/ingest_bdc.py` (Task 10)
  - `backend/scripts/test_db.py` (Task 11)
- **Frontend:**
  - `frontend/src/components/review/SessionChat.tsx` (Task 2, Task 11)
  - `frontend/src/app/update-password/page.tsx` and `frontend/src/lib/api.ts` (Task 4)
  - `frontend/src/middleware.ts` renamed to `frontend/src/proxy.ts` (Task 5)
  - `frontend/src/lib/types.ts`, `frontend/src/components/review/DocumentReview.tsx` and `frontend/src/components/chat/ChatInterface.tsx` (Task 11)
- **Sandbox:**
  - `sandbox/sandbox.sh` (Task 3)
  - `sandbox/supabase/schema.sql` (Task 7)
  - `sandbox/e2e/tests/01-landing-auth.spec.ts` and `sandbox/e2e/tests/04-backend-api.spec.ts` (Task 4 only)
  - `sandbox/frontend/Dockerfile` (Task 5, comment only)
  - `sandbox/FINDINGS.md` (final step)
- **Docs:** `README.md` (Task 11)

## Deliberately out of scope

- **Finding 5, password reset returns a usable token.** Deferred by the user (see Decisions). It's the one remaining known account-takeover path, so raise it again before public launch.
- **Rate limiting on auth endpoints.** Not a sandbox finding.
- **Real-LLM answer quality.** The sandbox uses a mock LLM.
- **Auth on `/api/query`.** The public spec chat is intentionally anonymous.

---

### Task 1: Branch setup, and an Azure startup command that actually starts

**Why:** `.github/workflows/azure-deploy-backend.yml` says to set `uvicorn app.main:app …` as the Startup Command. In the App Service Python 3.13 runtime, Oryx exposes the `antenv` virtualenv only through `PYTHONPATH`, so `uvicorn` isn't on `PATH`. The container exits 127 with `uvicorn: not found`. The sandbox reproduced this with Microsoft's own image.

**Files:**
- Modify: `.github/workflows/azure-deploy-backend.yml` (the Startup Command comment near lines 18–21)
- Delete: `docs/superpowers/plans/2026-09-26-fix-sandbox-findings.md` (the draft this plan replaces)

**Interfaces:** Produces the `fix/sandbox-findings` branch with `sandbox/` present. Every later task depends on it.

- [ ] **Step 0: Create the work branch.**

  ```bash
  git fetch origin
  git switch -c fix/sandbox-findings custom-error-logs
  git merge --no-edit origin/claude/ecstatic-knuth-8ib321
  ls sandbox/FINDINGS.md sandbox/sandbox.sh
  ```

  Expected: the merge has no conflicts (it only adds `sandbox/**` and a plan file), and both files exist. Next:
  1. `cp sandbox/sandbox.env.example sandbox/.env`, and fill it in per `sandbox/README.md`.
  2. Run `./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test` once to record the baseline: 34 pass, 3 fail (items 2–4 of FINDINGS).

- [ ] **Step 1: Reproduce the crash.**

  ```bash
  AZURE_STARTUP_COMMAND="uvicorn app.main:app --host 0.0.0.0 --port 8000" ./sandbox/sandbox.sh up
  docker logs njdot-sandbox-backend-1 2>&1 | tail -3
  ```

  Expected: `uvicorn: not found`, and `up` fails its health wait.

- [ ] **Step 2: Fix the notes.** Open the workflow file, find the numbered comment that tells you to set the Startup Command, and replace that numbered item with:

  ```yaml
  #   2. Azure Portal -> NJDOT -> Configuration -> General settings ->
  #      Startup Command:
  #        python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  #      NOT the bare "uvicorn ...": Oryx only puts the antenv virtualenv on
  #      PYTHONPATH, never its bin/ on PATH, and the runtime image has no
  #      global uvicorn, so the bare command exits "uvicorn: not found".
  ```

  Keep the item number that's already there if it isn't `2`.

- [ ] **Step 3: Verify with the default command.**

  ```bash
  ./sandbox/sandbox.sh up
  curl -s localhost:8000/health
  ```

  Expected: `{"status":"ok"}`.

- [ ] **Step 4: Hand off to a human.** In the PR description, ask whoever has portal access to:
  1. Read NJDOT → Configuration → General settings → Startup Command.
  2. Set it to the `python -m uvicorn …` value if it isn't already.
  3. Restart the app.
  4. Confirm `GET https://njdot-f2f6a4baaedyhbfx.eastus-01.azurewebsites.net/health` returns `{"status":"ok"}`.

  Git can't do this step. Don't block the rest of the plan on it.

- [ ] **Step 5: Commit.**

  ```bash
  git rm docs/superpowers/plans/2026-09-26-fix-sandbox-findings.md
  git add .github/workflows/azure-deploy-backend.yml docs/superpowers/plans/2026-09-25-fix-sandbox-findings.md
  git commit -m "fix(deploy): document a startup command the App Service runtime can run"
  ```

---

### Task 2: Require sign-in and project ownership on `/api/session/*`

**Why:** `backend/app/api/session.py` never reads `Authorization`. With only a UUID, anyone can:
- read a project's chat history (`GET /messages/{id}`)
- query its private documents (`POST /query`)
- watch its progress (`GET /status/{id}`)
- re-trigger ingestion (`POST /upload`)

**Test that proves it:** `sandbox/e2e/tests/04-backend-api.spec.ts › @security session endpoints should require auth` (currently red). It sends a random UUID with no token and expects 401 from `/messages` and `/query`. This plan makes that pass **unchanged**.

**Rule** (user decision, 2026-09-25):
- No token, or an invalid or expired one → **401**.
- The project has an owner who isn't the caller → **403**.
- The Supabase lookup fails → **503** (fail closed).
- Otherwise → allowed. That covers the caller's own project, and a project with no recorded owner (an anonymous API review, or a standalone session upload).

  `# ponytail:` the ownerless case is open to any signed-in user who holds the UUID. No UI creates such projects today. Record an owner at upload time if that changes.

**Files:**
- Create: `backend/app/project_access.py`
- Modify: `backend/app/api/session.py`:
  - the fastapi import (line 43)
  - `upload_session` (~403)
  - `session_status` (~463)
  - `session_query` (~773)
  - `get_session_messages` (~919)
- Modify: `frontend/src/components/review/SessionChat.tsx:174-192` (the SSE effect)
- Create: `backend/tests/test_session_auth.py`

**Interfaces:**
- Consumes:
  - `app.auth.user_id_from_token(authorization: str | None) -> str`, which raises `HTTPException(401)` on a missing, invalid or expired token.
  - `app.database.get_db()`.
  - `app.api.review._review_progress: Dict[str, Dict[str, Any]]`. Its `"user_id"` key is set when a review is queued (`review.py:1443`).
- Produces: `app.project_access.require_project_access(project_id: str, authorization: str | None) -> str`. It returns the caller's user id, or raises `HTTPException` with 401, 403 or 503.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_session_auth.py`:

  ```python
  """backend/tests/test_session_auth.py

  Sign-in + ownership on /api/session/* (sandbox finding 2). Mocks Supabase
  and token decoding; no network.

  Runnable two ways:
      python backend/tests/test_session_auth.py
      python -m pytest backend/tests/test_session_auth.py
  """

  from __future__ import annotations

  import asyncio
  import sys
  from pathlib import Path
  from unittest.mock import patch

  from fastapi import HTTPException

  _ROOT = Path(__file__).resolve().parent.parent
  if str(_ROOT) not in sys.path:
      sys.path.insert(0, str(_ROOT))

  from app import project_access  # noqa: E402
  from app.api.review import _review_progress  # noqa: E402
  from app.api.session import QueryRequest, get_session_messages, session_query  # noqa: E402

  OWNER, OTHER = "owner-uuid", "other-uuid"
  PID = "11111111-1111-1111-1111-111111111111"


  class _FakeQuery:
      def __init__(self, data):
          self._data = data

      def select(self, *a, **k):
          return self

      def eq(self, *a, **k):
          return self

      def limit(self, *a, **k):
          return self

      def execute(self):
          return type("Executed", (), {"data": self._data})()


  class _FakeDB:
      def __init__(self, rows):
          self._rows = rows

      def table(self, name):
          return _FakeQuery(self._rows)


  class _DownDB:
      def table(self, name):
          raise ConnectionError("supabase unreachable")


  def _status(fn, *args, **kwargs):
      """Run fn; return the HTTPException status it raised, or None."""
      try:
          result = fn(*args, **kwargs)
          if asyncio.iscoroutine(result):
              asyncio.run(result)
      except HTTPException as exc:
          return exc.status_code
      return None


  def setup_function(_fn=None):
      _review_progress.pop(PID, None)


  def test_no_token_is_401_even_for_unknown_project():
      with patch("app.project_access.get_db", return_value=_FakeDB([])):
          assert _status(project_access.require_project_access, PID, None) == 401


  def test_invalid_token_is_401():
      with patch("app.project_access.get_db", return_value=_FakeDB([])):
          assert _status(project_access.require_project_access, PID, "Bearer not-a-jwt") == 401


  def test_other_users_project_is_403():
      with patch("app.project_access.get_db", return_value=_FakeDB([{"user_id": OWNER}])), \
           patch("app.project_access.user_id_from_token", return_value=OTHER):
          assert _status(project_access.require_project_access, PID, "Bearer x") == 403


  def test_owner_is_allowed_from_db():
      with patch("app.project_access.get_db", return_value=_FakeDB([{"user_id": OWNER}])), \
           patch("app.project_access.user_id_from_token", return_value=OWNER):
          assert project_access.require_project_access(PID, "Bearer x") == OWNER


  def test_owner_is_allowed_from_in_process_store_without_db():
      # A review that just finished isn't in review_projects yet.
      _review_progress[PID] = {"status": "ready", "user_id": OWNER}
      with patch("app.project_access.get_db", return_value=_DownDB()), \
           patch("app.project_access.user_id_from_token", return_value=OWNER):
          assert project_access.require_project_access(PID, "Bearer x") == OWNER


  def test_ownerless_project_allowed_for_signed_in_user():
      with patch("app.project_access.get_db", return_value=_FakeDB([])), \
           patch("app.project_access.user_id_from_token", return_value=OTHER):
          assert project_access.require_project_access(PID, "Bearer x") == OTHER


  def test_standalone_upload_skips_owner_lookup():
      # project_id="" must not query review_projects (uuid column rejects '').
      with patch("app.project_access.get_db", return_value=_DownDB()), \
           patch("app.project_access.user_id_from_token", return_value=OTHER):
          assert project_access.require_project_access("", "Bearer x") == OTHER


  def test_db_down_fails_closed():
      with patch("app.project_access.get_db", return_value=_DownDB()), \
           patch("app.project_access.user_id_from_token", return_value=OWNER):
          assert _status(project_access.require_project_access, PID, "Bearer x") == 503


  def test_messages_endpoint_requires_token():
      with patch("app.project_access.get_db", return_value=_FakeDB([])):
          assert _status(get_session_messages, PID, authorization=None) == 401


  def test_query_endpoint_requires_token():
      req = QueryRequest(question="hi", session_id=PID)
      with patch("app.project_access.get_db", return_value=_FakeDB([])):
          assert _status(session_query, req, authorization=None) == 401


  if __name__ == "__main__":
      failures = 0
      for name, fn in sorted(globals().items()):
          if name.startswith("test_") and callable(fn):
              try:
                  setup_function(fn)
                  fn()
                  print(f"PASS {name}")
              except Exception as exc:  # noqa: BLE001
                  failures += 1
                  print(f"FAIL {name}: {exc}")
      total = sum(1 for n in globals() if n.startswith("test_"))
      print(f"\n{total - failures}/{total} passed")
  ```

  **Note on `test_invalid_token_is_401`:** `user_id_from_token` raises 401 for `"not-a-jwt"` on every branch: JWKS, the HS256 secret, and unverified decode. If the local `.env` points `SUPABASE_URL` at a live JWKS, the lookup still fails on the malformed token and falls through to 401.

- [ ] **Step 2: Run them and watch them fail.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests/test_session_auth.py -q
  ```

  Expected: collection error, `ModuleNotFoundError: No module named 'app.project_access'`.

- [ ] **Step 3: Create `backend/app/project_access.py`.**

  ```python
  """Sign-in + ownership rule for everything keyed by a review project id
  (/api/session/*).

  Every caller must be signed in. A project with an owner is usable only by
  that owner. The owner is looked up in the in-process review store first
  (a review that just finished isn't in review_projects yet -- the frontend
  inserts that row after /api/session/upload), then in review_projects.
  """

  from __future__ import annotations

  import logging
  from typing import Optional

  from fastapi import HTTPException

  from app.auth import user_id_from_token
  from app.database import get_db

  logger = logging.getLogger(__name__)


  def _project_owner(project_id: str) -> Optional[str]:
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


  def require_project_access(project_id: str, authorization: Optional[str]) -> str:
      """Return the caller's user id, or raise 401 (not signed in),
      403 (someone else's project) or 503 (ownership lookup failed)."""
      caller = user_id_from_token(authorization)
      # Empty id = standalone upload (no project yet). Skip the lookup:
      # querying review_projects.id = '' errors on the uuid column -> 503.
      owner = _project_owner(project_id) if project_id else None
      if owner and owner != caller:
          raise HTTPException(status_code=403, detail="This project does not belong to you")
      return caller
  ```

  The token check runs **before** the DB lookup, so a signed-out request never touches Supabase. That's why `test_no_token_is_401_even_for_unknown_project` needs no owner.

- [ ] **Step 4: Wire it into `session.py`.**
  - Change line 43 to:

    ```python
    from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
    ```

  - Add `from app.project_access import require_project_access` with the other `app.` imports.
  - `upload_session`: add a last parameter, `authorization: Optional[str] = Header(default=None),`. Make the first line of the function body (before the `utility_plan_bytes_list = …` line):

    ```python
    require_project_access(project_id or "", authorization)
    ```

    An empty id has no owner, so a standalone upload just needs a valid token.
  - `session_status`: change the signature to `async def session_status(session_id: str, token: Optional[str] = None) -> StreamingResponse:`, and make the first body line:

    ```python
    require_project_access(session_id, f"Bearer {token}" if token else None)
    ```

    The token comes as a query parameter because EventSource can't send headers. It's the same pattern as `review_status` (`review.py:207`).
  - `session_query`: change to `async def session_query(req: QueryRequest, authorization: Optional[str] = Header(default=None)) -> dict:`, and make the first body line `require_project_access(req.session_id, authorization)`.
  - `get_session_messages`: change to `async def get_session_messages(session_id: str, authorization: Optional[str] = Header(default=None)) -> dict:`, and make the first body line `require_project_access(session_id, authorization)`.
  - Add one sentence to each of those four docstrings: "Requires a signed-in caller; see app.project_access."

- [ ] **Step 5: Run the backend tests.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests -q
  ```

  Expected: all pass, including the 10 new tests. If `test_chat_similarity_floor.py` fails, check that its AST walk of `session_query` still finds `retrieve_sp_chunks`. Adding a parameter must not change that.

- [ ] **Step 6: Frontend SSE.** In `SessionChat.tsx`, replace the SSE effect (the block starting `// ── SSE: stream ingestion progress`) with:

  ```tsx
  // ── SSE: stream ingestion progress ────────────────────────────────────────
  // /api/session/status requires a signed-in caller. EventSource can't send
  // headers, so the token rides as ?token= (same as /api/review/{id}/status).
  // Wait for the token: DocumentReview fetches it asynchronously after
  // sessionId is set, and opening without it is a guaranteed 401.
  useEffect(() => {
    if (!sessionId || !authToken) return

    const url = `${apiBase}/api/session/status/${sessionId}?token=${encodeURIComponent(authToken)}`
    const es  = new EventSource(url)

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as ProgressEvent
        setProgress(data)
        if (data.status === 'ready' || data.status === 'error') {
          es.close()
        }
      } catch { /* ignore parse errors */ }
    }

    es.onerror = () => es.close()

    return () => es.close()
  }, [sessionId, apiBase, authToken])
  ```

  `messages` (line ~204) and `query` (line ~234) already send `authHeaders(...)`. `DocumentReview.tsx:608` already sends the Bearer header on `/api/session/upload` when signed in, which is always the case inside `/chat`. No other frontend change is needed.

- [ ] **Step 7: Verify in the sandbox.**

  ```bash
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test -g "session endpoints|upload → review"
  ```

  Expected:
  - `@security session endpoints should require auth` turns **green**.
  - `upload → review (SSE) → results → Q&A → …` stays green. That covers Review Focus 1: the SSE, history and question all return 200.
  - `sandbox/logs/REPORT.md` shows no 401 on `/api/session/status`.

- [ ] **Step 8: Commit.**

  ```bash
  git add backend/app/project_access.py backend/app/api/session.py backend/tests/test_session_auth.py frontend/src/components/review/SessionChat.tsx
  git commit -m "fix(session): require sign-in and project ownership on Document Q&A endpoints"
  ```

---

### Task 3: Let users delete and update their own conversations

**Why:** `backend/migrations/001_conversations.sql` defines only `conversations_select` and `conversations_insert`. Two frontend writes that run under the user's session are silently dropped: PostgREST answers 204 and changes 0 rows.
- the delete in `ChatInterface.tsx`, `handleDeleteConversation`
- the `updated_at` bump after each message, at `ChatInterface.tsx:271`

The result: deleted chats come back, and Recents never re-orders.

**Test that proves it:** `02-chat.spec.ts › delete a conversation removes it (and it stays gone after reload)` (currently red).

**Files:**
- Create: `backend/migrations/011_conversations_update_delete_policies.sql`
- Modify: `sandbox/sandbox.sh` (`cmd_db_apply`)

**Interfaces:** Produces the migration loop in `cmd_db_apply`. Task 7 relies on it to apply `012_*.sql`.

- [ ] **Step 1: Confirm the failure.**

  ```bash
  ./sandbox/sandbox.sh test -g "delete a conversation"
  ```

  Expected: FAIL, the conversation is back after reload.

- [ ] **Step 2: Write the migration** `backend/migrations/011_conversations_update_delete_policies.sql`:

  ```sql
  -- 011: conversations need UPDATE and DELETE policies.
  -- 001 only granted SELECT/INSERT, so the frontend's delete
  -- (ChatInterface.tsx handleDeleteConversation) and its updated_at bump
  -- after each message matched 0 rows under RLS and silently did nothing.
  -- Messages go with their conversation via ON DELETE CASCADE, so messages
  -- needs no DELETE policy. Idempotent: safe to re-run on live.
  do $$
  begin
    if not exists (select 1 from pg_policies
                   where schemaname = 'public' and tablename = 'conversations'
                     and policyname = 'conversations_update') then
      create policy "conversations_update" on public.conversations
        for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies
                   where schemaname = 'public' and tablename = 'conversations'
                     and policyname = 'conversations_delete') then
      create policy "conversations_delete" on public.conversations
        for delete using (auth.uid() = user_id);
    end if;
  end $$;
  ```

  Before relying on the cascade, check it: run `grep -n "on delete cascade" backend/migrations/001_conversations.sql`. If `messages.conversation_id` has no `on delete cascade`, stop and raise it. The delete would then fail on the FK instead of succeeding.

- [ ] **Step 3: Make the sandbox apply numbered migrations.** In `sandbox/sandbox.sh` `cmd_db_apply`, insert this right after the `conversations / messages present` block and before the `sandbox/supabase/schema.sql` line:

  ```bash
  # Numbered migrations from 011 up are idempotent — apply them every time.
  for f in "$REPO"/backend/migrations/0[1-9][1-9]_*.sql; do
    [[ -e "$f" ]] || continue
    n=$(basename "$f" | cut -d_ -f1)
    (( 10#$n >= 11 )) || continue
    say "migration $(basename "$f")"
    psql_docker < "$f"
  done
  ```

- [ ] **Step 4: Apply and verify.**

  ```bash
  ./sandbox/sandbox.sh db-apply && ./sandbox/sandbox.sh test -g "delete a conversation|ask with collection filter"
  ```

  Expected: both PASS. Then prove it's idempotent, and that the `using` clause still blocks other users:

  ```bash
  ./sandbox/sandbox.sh db-apply   # second run: prints the migration line, no errors
  source sandbox/.env
  docker exec -i supabase_db_njdot-chatbot psql -U postgres -At <<'SQL'
  begin;
  set local role authenticated;
  set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-000000000000"}';
  with d as (delete from conversations returning 1) select count(*) from d;
  rollback;
  SQL
  ```

  Expected: `0`. A stranger deletes nothing, and the rollback keeps the data. If the container name differs, take it from `docker ps --format '{{.Names}}' | grep supabase_db`.

- [ ] **Step 5: Live follow-through (human).** Paste the migration into the Supabase Dashboard → SQL Editor, run it, then run:

  ```sql
  select policyname, cmd from pg_policies where tablename = 'conversations';
  ```

  Expected: 4 rows. Record them in the PR.

- [ ] **Step 6: Commit.**

  ```bash
  git add backend/migrations/011_conversations_update_delete_policies.sql sandbox/sandbox.sh
  git commit -m "fix(db): add update/delete RLS policies on conversations"
  ```

---

### Task 4: Changing your password must not log you out

**Why:** `/update-password` calls `POST /api/auth/change-password`, which updates the password through the Supabase **admin** API. GoTrue revokes the user's sessions, so the next `getUser()` in the middleware gets `403 session_not_found`. The user lands on `/login`, despite the "Redirecting you back…" message. `supabase.auth.updateUser({ password })` from the browser keeps the current session.

**Test that proves it:** `01-landing-auth.spec.ts › change password from the user menu` (currently red; it expects `/chat`).

**Files:**
- Modify: `frontend/src/app/update-password/page.tsx`
- Modify: `frontend/src/lib/api.ts` (remove only `changePassword`, lines ~77–79)
- Modify: `backend/app/api/auth.py` (remove only `ChangePasswordBody` and the `/api/auth/change-password` handler, ~lines 167–190)
- Modify: `sandbox/e2e/tests/01-landing-auth.spec.ts` (the change-password test's endpoint assertion)
- Modify: `sandbox/e2e/tests/04-backend-api.spec.ts` (the OpenAPI route list, and the `change-password needs a token` test)

**Keep, because the deferred reset flow uses them:** `_update_user_password`, `_authPost`, `requestPasswordReset`, `resetPassword`, `/request-reset` and `/reset-password`.

- [ ] **Step 1: Confirm the failure.**

  ```bash
  ./sandbox/sandbox.sh test -g "change password"
  ```

  Expected: FAIL, received `/login`.

- [ ] **Step 2: Rewrite the page's submit.** In `update-password/page.tsx`:
  - Delete `import { changePassword } from '@/lib/api'`.
  - Delete the `accessToken` state line.
  - In the mount effect, change `if (token) { setAccessToken(token); setStage('ready') }` to `if (token) { setStage('ready') }`.
  - Replace the `try` block in `handleSubmit` with:

  ```tsx
  try {
    // updateUser keeps the current session. The old backend route used the
    // admin API, which revokes every session and bounced the user to /login.
    const { error: updateError } = await createClient().auth.updateUser({ password })
    if (updateError) throw updateError
    setStage('success')
    setTimeout(() => router.push('/chat'), 2000)
  } catch (err) {
    setError(err instanceof Error ? err.message : 'Password update failed. Please try again.')
  } finally {
    setIsLoading(false)
  }
  ```

- [ ] **Step 3: Delete the dead paths.**
  - In `frontend/src/lib/api.ts`, remove the `changePassword` function.
  - In `backend/app/api/auth.py`, remove the `ChangePasswordBody` model and the `@router.post("/api/auth/change-password")` handler.
  - Leave the module docstring's description of the reset flow as it is. Delete only sentences that describe change-password.
  - Then run:

    ```bash
    grep -rn "changePassword\|change-password\|ChangePasswordBody" frontend/src backend/app
    ```

    Expected: no output.

- [ ] **Step 4: Update the two E2E assertions that referenced the removed endpoint.** This is the only allowed E2E edit, because the endpoint no longer exists.
  - In `01-landing-auth.spec.ts`, `change password from the user menu`:
    - Replace `const call = await monitor.call("/api/auth/change-password", "POST"); expect(call.status).toBe(200);` with:

      ```ts
      const call = await monitor.call(/\/auth\/v1\/user$/, "PUT")
      expect(call.status).toBe(200)
      ```

    - Delete the `monitor.allow(/auth\/v1\/user -> 403/)` line and the 3-line comment above it. That 403 must no longer happen.
    - Keep the final `toBe("/chat")` assertion unchanged.
  - In `04-backend-api.spec.ts`:
    - Remove `"/api/auth/change-password", ` from the OpenAPI route list.
    - Delete the whole `test("change-password needs a token", …)` block.

- [ ] **Step 5: Verify.**

  ```bash
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test -g "change password|password|login|OpenAPI"
  ```

  Expected: all green, and the user ends on `/chat`.

- [ ] **Step 6: Hosted setting (human).** In Supabase Dashboard → Authentication → Providers → Email, check **Secure password change**. If it's ON, `updateUser` returns `reauthentication_needed` for sessions older than about 24 hours, and the `catch` above shows that message. Record the setting in the PR.

- [ ] **Step 7: Commit.**

  ```bash
  git add frontend/src/app/update-password/page.tsx frontend/src/lib/api.ts backend/app/api/auth.py sandbox/e2e/tests/01-landing-auth.spec.ts sandbox/e2e/tests/04-backend-api.spec.ts
  git commit -m "fix(auth): change password via updateUser so the session survives"
  ```

---

### Task 5: Rename `middleware.ts` to `proxy.ts` (Next 16)

**Why:** `next build` warns: *The "middleware" file convention is deprecated. Please use "proxy" instead.*

**Files:**
- Rename: `frontend/src/middleware.ts` → `frontend/src/proxy.ts`
- Modify: `sandbox/frontend/Dockerfile` (the one comment that names `middleware.ts`)

- [ ] **Step 1: Rename.**

  ```bash
  git mv frontend/src/middleware.ts frontend/src/proxy.ts
  ```

  In `proxy.ts`:
  - Change `export async function middleware(request: NextRequest)` to `export async function proxy(request: NextRequest)`.
  - In the header comment, change "middleware" to "proxy".
  - Leave `export const config` unchanged.

  Then fix the comment in `sandbox/frontend/Dockerfile`, and run:

  ```bash
  grep -rn "middleware" frontend/src sandbox/frontend
  ```

  Expected: no references to the file. A comment that uses "middleware" as a general term is fine.

- [ ] **Step 2: Build check.**

  ```bash
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh checks
  grep -i "deprecated" sandbox/logs/static-next-build.log
  ```

  Expected: no output from the grep, and the build still lists the proxy.

- [ ] **Step 3: Behaviour check.**

  ```bash
  ./sandbox/sandbox.sh test -g "redirect|login|protected"
  ```

  Expected: all green. This covers signed-out `/chat` → `/login`, signed-in auth pages → `/chat`, and `/update-password` bouncing.

- [ ] **Step 4: Commit.**

  ```bash
  git add -A frontend/src/proxy.ts frontend/src/middleware.ts sandbox/frontend/Dockerfile
  git commit -m "chore(frontend): rename middleware.ts to proxy.ts for Next 16"
  ```

---

### Task 6: Don't leak exception text from `/api/query` and `/api/debug`

**Why:** When the LLM fails, clients get `500 {"detail":"Pipeline error [RuntimeError]: LLM completion failed [InternalServerError]: Error code: 500 - {...}"}`. That exposes the exception class and the upstream provider body. `logger.exception(...)` already writes the full detail to the server log (`query.py:270`, `query.py:380`).

**Files:**
- Modify: `backend/app/api/query.py`:
  - lines 269–274 (`query_endpoint`)
  - lines 379–384 (`debug_endpoint`)
  - the module docstring, lines 19–20
- Create: `backend/tests/test_query_error_detail.py`

**Interfaces:** Consumes the module singletons `app.api.query._expander` (`.expand_and_search`, the first call in `query_endpoint`'s `try`) and `app.api.query._hybrid` (`.search`, the first call in `debug_endpoint`'s `try`). Also consumes `app.models.QueryRequest(query: str, collection: str | None = None)`.

- [ ] **Step 1: Write the failing test.** Create `backend/tests/test_query_error_detail.py`:

  ```python
  """backend/tests/test_query_error_detail.py

  /api/query and /api/debug must not echo exception text to the client
  (sandbox finding 7). The full error still goes to the server log.

  Runnable two ways:
      python backend/tests/test_query_error_detail.py
      python -m pytest backend/tests/test_query_error_detail.py
  """

  from __future__ import annotations

  import asyncio
  import sys
  from pathlib import Path
  from unittest.mock import patch

  from fastapi import HTTPException

  _ROOT = Path(__file__).resolve().parent.parent
  if str(_ROOT) not in sys.path:
      sys.path.insert(0, str(_ROOT))

  from app.api import query as query_module  # noqa: E402
  from app.models import QueryRequest  # noqa: E402

  SECRET = "Error code: 500 - {'upstream': 'secret body'}"
  GENERIC = "The assistant could not answer right now. Please try again."


  def _detail_of(coro):
      try:
          asyncio.run(coro)
      except HTTPException as exc:
          return exc.status_code, exc.detail
      raise AssertionError("expected HTTPException")


  def test_query_endpoint_hides_exception_text():
      with patch.object(query_module._expander, "expand_and_search", side_effect=RuntimeError(SECRET)):
          status, detail = _detail_of(query_module.query_endpoint(QueryRequest(query="What is a working day?")))
      assert status == 500
      assert detail == GENERIC
      assert "secret" not in detail and "RuntimeError" not in detail


  def test_debug_endpoint_hides_exception_text():
      with patch.object(query_module._hybrid, "search", side_effect=RuntimeError(SECRET)):
          status, detail = _detail_of(query_module.debug_endpoint(QueryRequest(query="What is a working day?")))
      assert status == 500
      assert detail == GENERIC
      assert "secret" not in detail and "RuntimeError" not in detail


  if __name__ == "__main__":
      failures = 0
      for name, fn in sorted(globals().items()):
          if name.startswith("test_") and callable(fn):
              try:
                  fn()
                  print(f"PASS {name}")
              except Exception as exc:  # noqa: BLE001
                  failures += 1
                  print(f"FAIL {name}: {exc}")
      total = sum(1 for n in globals() if n.startswith("test_"))
      print(f"\n{total - failures}/{total} passed")
  ```

  `debug_endpoint` wraps `_hybrid.search` in `partial(...)` inside the `try`, after the patch is active, so the patched method is the one that runs.

- [ ] **Step 2: Run it and watch it fail.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests/test_query_error_detail.py -q
  ```

  Expected: 2 failed, with the detail containing `Pipeline error [RuntimeError]: …`.

- [ ] **Step 3: Fix both handlers.**
  - In `query_endpoint`, replace the `raise HTTPException(...)` under `logger.exception("Pipeline error …")` with:

    ```python
            raise HTTPException(
                status_code=500,
                detail="The assistant could not answer right now. Please try again.",
            ) from exc
    ```

  - In `debug_endpoint`, make the same replacement under `logger.exception("Debug pipeline error …")`.
  - In the module docstring (lines 19–20), change "the ``detail`` string includes the exception type and message so the …" to say that `detail` is a fixed generic message, and that the exception is logged server-side with its traceback. Delete the rest of that sentence.

- [ ] **Step 4: Verify.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests -q
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test -g "OpenAI outage|red error bubble"
  ```

  Expected: all pass. In `sandbox/logs/REPORT.md`, the `observed` note on the outage test shows the generic detail.

- [ ] **Step 5: Commit.**

  ```bash
  git add backend/app/api/query.py backend/tests/test_query_error_detail.py
  git commit -m "fix(query): return a generic 500 detail; keep specifics in the log"
  ```

---

### Task 7: Commit the schema for the tables git doesn't have

**Why:** The code uses `review_projects`, `compliance_checks`, `session_messages`, `bdc_section_map`, `rate_limits` and the public `pdfs` bucket, but the repo has no DDL for any of them. A fresh Supabase can't be rebuilt from git. `sandbox/supabase/schema.sql` reconstructs them, and the whole app ran end to end against that reconstruction.

**Files:**
- Create: `backend/migrations/012_baseline_untracked_tables.sql`
- Modify: `sandbox/supabase/schema.sql` (reduced to a pointer)

**Interfaces:** Consumes Task 3's migration loop in `cmd_db_apply`, which applies `012_*.sql` automatically.

- [ ] **Step 1: Diff against production first (human, read-only).** Run this in the live project's SQL Editor, and paste the output into the PR:

  ```sql
  select table_name, column_name, data_type, is_nullable, column_default
  from information_schema.columns
  where table_schema = 'public'
    and table_name in ('review_projects','compliance_checks','session_messages','bdc_section_map','rate_limits')
  order by table_name, ordinal_position;

  select tablename, policyname, cmd, qual, with_check from pg_policies
  where tablename in ('review_projects','compliance_checks','session_messages');

  select id, public from storage.buckets;
  ```

  Wherever live differs from `sandbox/supabase/schema.sql` (types, defaults, extra columns, policy names), **live wins**. The migration must describe production. Note the data type of `bdc_section_map.bdc_date` too, because Task 10 Step 5 depends on it.

- [ ] **Step 2: Write `012_baseline_untracked_tables.sql`.**
  - Copy the tables, indexes, RLS, policies and bucket sections of `sandbox/supabase/schema.sql`, and correct them to match Step 1.
  - Make every statement additive:
    - `create table if not exists`
    - `create index if not exists`
    - `alter table … enable row level security` (safe to repeat)
    - each `create policy` wrapped in the same `if not exists (select 1 from pg_policies where schemaname = … and tablename = … and policyname = …)` guard that Task 3 uses
    - buckets through `insert into storage.buckets … on conflict (id) do nothing`
  - Head the file with:

  ```sql
  -- 012: baseline DDL for tables/buckets that existed only in the live
  -- Supabase (created by hand; migrations 003-006 and 008 were never
  -- committed). Reconstructed in sandbox/supabase/schema.sql, corrected
  -- against production on <date> (see PR). Every statement is guarded, so
  -- running this on production is a no-op.
  ```

- [ ] **Step 3: Prove it rebuilds from scratch.** Use a **throwaway** local Supabase project, never the linked one.
  1. `supabase db reset`
  2. Apply `backend/sql/local_setup.sql`, then migrations `001`, `002`, `009`, `010`, `011`, `012` in order.
  3. Apply `backend/sql/storage_review_files_policies.sql`.
  4. Run `./sandbox/sandbox.sh doctor`. Expected: every object ✓.
  5. Run `./sandbox/sandbox.sh seed && ./sandbox/sandbox.sh test`. Expected: nothing fails because of schema.

  Then run `db-apply` a second time. Expected: no errors (idempotent).

- [ ] **Step 4: Point the sandbox at the migration.** Replace the tables and bucket body of `sandbox/supabase/schema.sql` with:

  ```sql
  -- Superseded by backend/migrations/012_baseline_untracked_tables.sql,
  -- which cmd_db_apply applies through its numbered-migration loop.
  select 1;
  ```

  Keep anything in the file that isn't in 012 and is sandbox-only, such as seed helpers.

- [ ] **Step 5: Commit.**

  ```bash
  git add backend/migrations/012_baseline_untracked_tables.sql sandbox/supabase/schema.sql
  git commit -m "feat(db): commit baseline DDL for tables that only existed in the live database"
  ```

---

### Task 8: Stop Langfuse from logging a warning on every LLM call

**Why:** With `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` unset, every `CallbackHandler(...)` makes langfuse log an authentication WARNING. That was 127 lines in one run, burying real warnings in the Azure log stream.

**Files:**
- Modify: `backend/app/observability.py`
- Create: `backend/tests/test_observability_quiet.py`

**Interfaces:**
- Produces: the three public functions' contracts are unchanged. `get_langfuse_client()`, `new_trace_id(seed: str) -> Optional[str]` and `get_langfuse_handler(trace_id=None, parent_span_id=None)` each return `None` when unconfigured.
- Consumes: `tests/logcapture.capture_logs(logger_name, level=logging.WARNING)`.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_observability_quiet.py`:

  ```python
  """backend/tests/test_observability_quiet.py

  With Langfuse unconfigured, tracing must be skipped silently instead of
  warning on every LLM call; with it configured, tracing must still turn on.

  Runnable two ways:
      python backend/tests/test_observability_quiet.py
      python -m pytest backend/tests/test_observability_quiet.py
  """

  from __future__ import annotations

  import logging
  import os
  import sys
  from pathlib import Path
  from unittest.mock import patch

  _ROOT = Path(__file__).resolve().parent.parent
  if str(_ROOT) not in sys.path:
      sys.path.insert(0, str(_ROOT))

  from app import observability  # noqa: E402
  from tests.logcapture import capture_logs  # noqa: E402

  _UNSET = {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}
  _SET = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test"}


  def test_unconfigured_langfuse_returns_none_without_warning():
      with patch.dict(os.environ, _UNSET), \
           capture_logs("langfuse") as lf_records, \
           capture_logs("app.observability") as our_records:
          for _ in range(3):
              assert observability.get_langfuse_handler() is None
              assert observability.new_trace_id("seed") is None
              assert observability.get_langfuse_client() is None
      assert lf_records == [], [r.getMessage() for r in lf_records]
      assert our_records == [], [r.getMessage() for r in our_records]


  def test_configured_langfuse_still_builds_handler():
      sentinel = object()
      with patch.dict(os.environ, _SET), \
           patch("langfuse.langchain.CallbackHandler", return_value=sentinel):
          assert observability.get_langfuse_handler() is sentinel


  if __name__ == "__main__":
      failures = 0
      for name, fn in sorted(globals().items()):
          if name.startswith("test_") and callable(fn):
              try:
                  fn()
                  print(f"PASS {name}")
              except Exception as exc:  # noqa: BLE001
                  failures += 1
                  print(f"FAIL {name}: {exc}")
      total = sum(1 for n in globals() if n.startswith("test_"))
      print(f"\n{total - failures}/{total} passed")
  ```

  If `from tests.logcapture import …` fails to import when run as a plain script, copy the import form that `backend/tests/test_llm_logging.py` uses for `logcapture`, and use exactly that.

- [ ] **Step 2: Run it and watch it fail.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests/test_observability_quiet.py -q
  ```

  Expected: `test_unconfigured_…` FAILS, because a handler object is returned and/or langfuse logs a warning. `test_configured_…` passes.

- [ ] **Step 3: Fix.** In `backend/app/observability.py`:
  - Add `import os` next to `import logging`.
  - Add this function above `get_langfuse_client`:

  ```python
  _tracing_off_logged = False


  def _langfuse_configured() -> bool:
      """True only when both keys are set. Unset keys mean tracing is
      intentionally off -- skip Langfuse entirely rather than let its SDK
      warn about missing credentials on every LLM call."""
      global _tracing_off_logged
      if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
          return True
      if not _tracing_off_logged:
          _tracing_off_logged = True
          logger.info("Langfuse keys not set; LangChain tracing is off")
      return False
  ```

  - Make this the first line of the body of `get_langfuse_client`, `new_trace_id` and `get_langfuse_handler`:

    ```python
        if not _langfuse_configured():
            return None
    ```

  - Leave every existing `except` branch unchanged. Those are real failures when tracing *is* configured.
  - Add one line to the module docstring: "Unset keys → every function returns None without touching the SDK."

  The INFO line is below the test's WARNING capture level, so the test still passes. It's logged once per process, and that's intended.

- [ ] **Step 4: Verify.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests -q
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test
  grep -ci "langfuse" sandbox/logs/backend.log
  ```

  Expected: tests pass, and the grep prints `0` or `1` (the INFO line).

- [ ] **Step 5: Commit.**

  ```bash
  git add backend/app/observability.py backend/tests/test_observability_quiet.py
  git commit -m "fix(observability): skip Langfuse entirely when it isn't configured"
  ```

---

### Task 9: Silence Neo4j "does not exist yet" notifications, and keep the liveness check

**Why:** The graph queries reference relationship types and properties that only exist once a project has that data (`CONSTRAINED_BY`, `COVERED_BY`, `MENTIONS`, …). The server returns `01N51`/`01N52` UNRECOGNIZED notifications, and the driver logs them as WARNING on every review.

**Watch out:** `neo4j_client.py` **already** passes `driver_config={"liveness_check_timeout": 60}`. That fixed a real production `SessionExpired` after about 4 minutes idle. Add the new key to the same dict. Do **not** replace the dict.

**Files:**
- Modify: `backend/app/neo4j_client.py` (the `driver_config=` argument, ~line 43)
- Create: `backend/tests/test_neo4j_driver_config.py`

**Interfaces:** Consumes `app.neo4j_client.Neo4jClient.get_graph()` and its class attribute `_instance`, plus `app.config.config.NEO4J_URI` / `NEO4J_PASSWORD`.

- [ ] **Step 1: Check the driver API.**

  ```bash
  .venv/Scripts/python.exe -c "import neo4j, inspect; from neo4j import GraphDatabase; print(neo4j.__version__); print('notifications_disabled_classifications' in inspect.getsource(neo4j._conf))"
  ```

  Expected: a version of 5.22 or later, and `True`. If it prints `False`, the key is `notifications_disabled_categories` on this driver. Use that name with `["UNRECOGNIZED"]` in Steps 2 and 4.

- [ ] **Step 2: Write the failing test.** Create `backend/tests/test_neo4j_driver_config.py`:

  ```python
  """backend/tests/test_neo4j_driver_config.py

  The Neo4j singleton must (a) keep liveness_check_timeout -- it fixed a
  production SessionExpired after idle -- and (b) disable the UNRECOGNIZED
  notification class that floods the log on every review (sandbox noise).

  Runnable two ways:
      python backend/tests/test_neo4j_driver_config.py
      python -m pytest backend/tests/test_neo4j_driver_config.py
  """

  from __future__ import annotations

  import sys
  from pathlib import Path
  from unittest.mock import patch

  _ROOT = Path(__file__).resolve().parent.parent
  if str(_ROOT) not in sys.path:
      sys.path.insert(0, str(_ROOT))

  from app import neo4j_client  # noqa: E402


  def _driver_config():
      neo4j_client.Neo4jClient._instance = None
      with patch.object(neo4j_client, "Neo4jGraph") as graph_cls, \
           patch.object(neo4j_client.config, "NEO4J_URI", "bolt://x:7687"), \
           patch.object(neo4j_client.config, "NEO4J_PASSWORD", "pw"):
          neo4j_client.Neo4jClient.get_graph()
      neo4j_client.Neo4jClient._instance = None
      return graph_cls.call_args.kwargs["driver_config"]


  def test_driver_config_keeps_liveness_check():
      assert _driver_config()["liveness_check_timeout"] == 60


  def test_driver_config_disables_unrecognized_notifications():
      assert _driver_config()["notifications_disabled_classifications"] == ["UNRECOGNIZED"]


  if __name__ == "__main__":
      failures = 0
      for name, fn in sorted(globals().items()):
          if name.startswith("test_") and callable(fn):
              try:
                  fn()
                  print(f"PASS {name}")
              except Exception as exc:  # noqa: BLE001
                  failures += 1
                  print(f"FAIL {name}: {exc}")
      total = sum(1 for n in globals() if n.startswith("test_"))
      print(f"\n{total - failures}/{total} passed")
  ```

- [ ] **Step 3: Run it.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests/test_neo4j_driver_config.py -q
  ```

  Expected: `…keeps_liveness_check` PASSES, and `…disables_unrecognized_notifications` FAILS with a KeyError.

- [ ] **Step 4: Fix.** Replace the single line `driver_config={"liveness_check_timeout": 60},` with the version below. Keep the long comment above it about liveness:

  ```python
                  driver_config={
                      "liveness_check_timeout": 60,
                      # 01N51/01N52 ("relationship type / property does not
                      # exist") fire on every review for graph features a
                      # project has no data for yet -- expected, not
                      # actionable. Other notification classes still log.
                      "notifications_disabled_classifications": ["UNRECOGNIZED"],
                  },
  ```

- [ ] **Step 5: Verify.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests -q
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh test -g "upload → review"
  grep -c "neo4j.notifications" sandbox/logs/backend.log
  ```

  Expected: tests pass, the review E2E test stays green, and the grep prints `0`.

- [ ] **Step 6: Commit.**

  ```bash
  git add backend/app/neo4j_client.py backend/tests/test_neo4j_driver_config.py
  git commit -m "fix(neo4j): disable UNRECOGNIZED notifications, keep liveness check"
  ```

---

### Task 10: Don't write the string `"None"` for missing BDC dates

**Why:** `scripts/ingest_bdc.py:283-284` stores `str(header["bdc_date"])` and `str(header["effective_date"])`. `bdc_date` is `None` when the header has no date (line 118), and `str(None)` becomes `"None"`. A `date` column rejects the insert, and a text column sorts it wrongly.

**Files:**
- Modify: `backend/scripts/ingest_bdc.py` (add `_iso` near `_parse_date` at line 77, and use it at lines 283–284)
- Create: `backend/tests/test_bdc_dates.py`

**Interfaces:** Produces `scripts.ingest_bdc._iso(d: date | None) -> str | None`.

**Import note:** importing `scripts.ingest_bdc` runs `load_dotenv(backend/.env)` and builds `openai.OpenAI(api_key=config.OPENAI_API_KEY)` at module level. That raises when no key is set, so the test sets a dummy `OPENAI_API_KEY` before importing.

- [ ] **Step 1: Write the failing test.** Create `backend/tests/test_bdc_dates.py`:

  ```python
  """backend/tests/test_bdc_dates.py

  Missing BDC dates must be stored as NULL, not the string "None".

  Runnable two ways:
      python backend/tests/test_bdc_dates.py
      python -m pytest backend/tests/test_bdc_dates.py
  """

  from __future__ import annotations

  import os
  import sys
  from datetime import date
  from pathlib import Path

  _ROOT = Path(__file__).resolve().parent.parent
  if str(_ROOT) not in sys.path:
      sys.path.insert(0, str(_ROOT))

  os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")  # module builds an OpenAI client at import

  from scripts.ingest_bdc import _iso  # noqa: E402


  def test_missing_date_is_none_not_string():
      assert _iso(None) is None


  def test_date_is_iso_string():
      assert _iso(date(2025, 3, 1)) == "2025-03-01"


  if __name__ == "__main__":
      failures = 0
      for name, fn in sorted(globals().items()):
          if name.startswith("test_") and callable(fn):
              try:
                  fn()
                  print(f"PASS {name}")
              except Exception as exc:  # noqa: BLE001
                  failures += 1
                  print(f"FAIL {name}: {exc}")
      total = sum(1 for n in globals() if n.startswith("test_"))
      print(f"\n{total - failures}/{total} passed")
  ```

  If `from scripts.ingest_bdc import …` fails because `backend/scripts` has no `__init__.py`, add `sys.path.insert(0, str(_ROOT / "scripts"))` and write `from ingest_bdc import _iso` instead. Don't add an `__init__.py`.

- [ ] **Step 2: Run it and watch it fail.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests/test_bdc_dates.py -q
  ```

  Expected: `ImportError: cannot import name '_iso'`.

- [ ] **Step 3: Fix.** Add this directly below `_parse_date`:

  ```python
  def _iso(d: date | None) -> str | None:
      """ISO date string, or None (→ SQL NULL) when the BDC header had none."""
      return d.isoformat() if d else None
  ```

  In the `bdc_section_map` insert, replace the two `str(...)` lines with:

  ```python
              "bdc_date":            _iso(header["bdc_date"]),
              "effective_date":      _iso(header["effective_date"]),
  ```

- [ ] **Step 4: Run it and watch it pass.**

  ```bash
  .venv/Scripts/python.exe -m pytest backend/tests/test_bdc_dates.py -q
  ```

  Expected: 2 passed.

- [ ] **Step 5: Clean the live data (human).** Skip this if Task 7 Step 1 showed `bdc_date` is type `date`, since then no `"None"` rows can exist. Otherwise, run this in the SQL Editor and record the row counts in the PR:

  ```sql
  update bdc_section_map set bdc_date = null       where bdc_date = 'None';
  update bdc_section_map set effective_date = null where effective_date = 'None';
  ```

- [ ] **Step 6: Commit.**

  ```bash
  git add backend/scripts/ingest_bdc.py backend/tests/test_bdc_dates.py
  git commit -m "fix(ingest_bdc): store missing BDC dates as NULL, not \"None\""
  ```

---

### Task 11: Lint errors, pytest warning, and the APOC prerequisite

**Why:** These are the report's remaining items:
- 7 ESLint `no-explicit-any` errors
- 2 unused variables
- a `PytestReturnNotNoneWarning` from `scripts/test_db.py::test_connection`
- APOC isn't listed as a prerequisite, even though every review fails without it (`langchain-neo4j` calls `apoc.meta.data()`)

**Files:**
- Modify:
  - `frontend/src/lib/types.ts:37`
  - `frontend/src/components/review/DocumentReview.tsx`:
    - lines 55 and 59 (`key_map`, `estimate`)
    - line ~190 (`CitationPill` `authToken`)
    - line ~472 (`didParseCell`)
  - `frontend/src/components/review/SessionChat.tsx:~207` (`(m: any)`)
  - `frontend/src/components/chat/ChatInterface.tsx:~936` (unused `index`)
- Modify: `backend/scripts/test_db.py`
- Modify: `README.md`

The line numbers come from `sandbox/logs/static-eslint.log` on the 2026-09-25 run. Re-read that log after Task 2, because Task 2 shifts `SessionChat.tsx` lines.

- [ ] **Step 1: Get the current list.**

  ```bash
  ./sandbox/sandbox.sh checks && cat sandbox/logs/static-eslint.log
  ```

  Expected: 9 problems, at the locations listed above give or take a few lines.

- [ ] **Step 2: Fix each one with the narrowest honest type.**
  - **`types.ts:37`, `review_result: any`:**
    1. Move the `ReviewResult` interface out of `DocumentReview.tsx` into `types.ts` and export it.
    2. Change the field to `review_result: ReviewResult | null`.
    3. In `DocumentReview.tsx`, `import type { …, ReviewResult } from '@/lib/types'`.
  - **`DocumentReview.tsx:55` and `:59`:** replace each `any` in the `key_map` and `estimate` shapes with `Record<string, unknown>`. For example, `extraction: Record<string, unknown>`, and the same for `region` and `cost_gap`. If a use site then fails to compile because it reads a property, narrow it at that use site with `as { field?: string }`. Don't reintroduce `any`.
  - **`DocumentReview.tsx:~472`, `didParseCell: (data: any)`:**
    - Change it to `didParseCell: (data: CellHookData)`, with `import type { CellHookData } from 'jspdf-autotable'`.
    - If `jspdf-autotable` doesn't export `CellHookData` in the installed version, run `grep -n "CellHookData\|export" frontend/node_modules/jspdf-autotable/dist/index.d.ts | head` and use the exported hook-data type it shows.
  - **`SessionChat.tsx:~207`, `(m: any)`:** change it to `(m: { role: Message['role']; content: string; sources?: Source[] })`. `Message` and `Source` are already declared in that file (lines ~17 and ~27).
  - **Unused `authToken` in `CitationPill` (`DocumentReview.tsx:~190`):**
    1. Remove it from the destructure and the prop type.
    2. Remove `authToken={authToken}` from the `<CitationPill …/>` call inside `CheckCard`.
    3. Leave `CheckCard`'s own `authToken` prop, which still feeds `PDFViewerModal`.
  - **Unused `index` in `CitationCard` (`ChatInterface.tsx:~936`):** remove it from the destructure and the prop type, and remove `index={…}` from its call site.

- [ ] **Step 3: Verify the lint and build.**

  ```bash
  ./sandbox/sandbox.sh up && ./sandbox/sandbox.sh checks
  cat sandbox/logs/static-eslint.log
  ```

  Expected: `✖ 0 problems` or no output, and `next build` still succeeds.

- [ ] **Step 4: Fix `test_db.py`.** Rename `def test_connection():` to `def check_connection():`, and change the `__main__` call to `check_connection()`. Pytest stops collecting it, and running it directly behaves the same.

  ```bash
  .venv/Scripts/python.exe -m pytest backend -q -W error::pytest.PytestReturnNotNoneWarning --co -q | tail -1
  ```

  Expected: the collection count, with no `test_db.py::test_connection` in it.

- [ ] **Step 5: README.**
  - Under Prerequisites, change the Neo4j bullet to say the instance **must have APOC**:
    - **Aura:** includes it.
    - **Neo4j Desktop:** install the APOC plugin.
    - **Docker:** set `NEO4J_PLUGINS='["apoc"]'`.

    Explain why: `langchain-neo4j` calls `apoc.meta.data()`, and without it every review fails with "An unexpected error occurred".
  - Add a line pointing to `sandbox/README.md` for the production-parity sandbox.

- [ ] **Step 6: Verify end to end.**

  ```bash
  ./sandbox/sandbox.sh test
  tail -3 sandbox/logs/static-pytest.log
  ```

  Expected: the E2E run is all green, and `static-pytest.log` ends `N passed` with no warnings summary.

- [ ] **Step 7: Commit.**

  ```bash
  git add frontend/src/lib/types.ts frontend/src/components/review/DocumentReview.tsx frontend/src/components/review/SessionChat.tsx frontend/src/components/chat/ChatInterface.tsx backend/scripts/test_db.py README.md
  git commit -m "chore: fix lint errors, pytest collection warning, document APOC requirement"
  ```

---

## Final verification

- [ ] **Rebuild and run everything from clean.**

  ```bash
  ./sandbox/sandbox.sh down
  ./sandbox/sandbox.sh db-apply
  ./sandbox/sandbox.sh up
  ./sandbox/sandbox.sh seed
  ./sandbox/sandbox.sh test
  ```

- [ ] **Open `sandbox/logs/REPORT.md`.** Expected:
  - **Tests:** 36 passed, 0 failed. That's the original 37 minus the `change-password needs a token` test Task 4 deleted.
  - **Browser errors:** none.
  - **Backend tracebacks:** only the intentional OpenAI-outage test.
  - **Backend warnings:** no `langfuse` or `neo4j.notifications` lines.
  - **Static checks:** 0 ESLint problems, no pytest warnings summary, and no `middleware` deprecation. The only acceptable warning is `next build: No build cache found`, which is a sandbox artifact.
- [ ] **Update the findings.** Replace `sandbox/FINDINGS.md` with a short note: "All resolved on <date> except finding 5 (password reset, deferred by owner), plan: docs/superpowers/plans/2026-09-25-fix-sandbox-findings.md". Copy the new report to `sandbox/reports/<date>-REPORT.md`, then commit both.
- [ ] **Production follow-through that git can't do.** List it in the PR:
  - Azure Startup Command (Task 1 Step 4).
  - Migrations 011 and 012 run on the live Supabase (Task 3 Step 5, Task 7).
  - The Secure password change setting recorded (Task 4 Step 6).
  - `bdc_section_map` cleanup (Task 10 Step 5).
  - Finding 5 is still open: raise it before public launch.
