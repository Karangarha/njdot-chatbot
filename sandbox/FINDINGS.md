# Findings so far (from reading the code — not yet confirmed by a sandbox run)

These came up while building the sandbox. The E2E suite has a test aimed at
each one, so your first `./sandbox/sandbox.sh test` run will confirm or clear
them in `logs/REPORT.md`.

## Security

1. **Anyone who knows an email can reset that account's password.**
   `POST /api/auth/request-reset` returns a working `reset_token` straight to
   the caller (`backend/app/api/auth.py:112-133`). No email is sent, so the
   step never proves the caller owns the inbox. The next step,
   `/api/auth/reset-password`, then sets any password.
   *Test:* `01 › forgot password` (passing it confirms the problem).
2. **Session Q&A endpoints have no auth.** `/api/session/upload`, `/status`,
   `/query` and `/messages/{id}` never read `Authorization`
   (`backend/app/api/session.py`). Anyone holding a project UUID can read that
   project's chat history and query its private documents.
   *Test:* `04 › @security session endpoints should require auth`.
3. **Unsigned JWTs are accepted if the config slips.** When JWKS
   verification fails and `SUPABASE_JWT_SECRET` is empty,
   `user_id_from_token` decodes the token **without checking the signature**
   (`backend/app/auth.py:86-92`).
   *Test:* `04 › @security an unsigned (alg=none) JWT is rejected`. It is also
   worth checking Azure's App Settings to confirm `SUPABASE_JWT_SECRET` is set.

## Database / RLS

4. **The frontend's conversation delete and `updated_at` update are likely
   silent no-ops.** `migrations/001_conversations.sql` only defines SELECT and
   INSERT policies. `ChatInterface.tsx:204` (delete) and `:271` (update) run
   under the user's session, so without UPDATE/DELETE policies Supabase
   answers 204 and changes 0 rows. The conversation then reappears after a
   reload.
   *Test:* `02 › delete a conversation … stays gone after reload`. This only
   holds if the live database has no extra policies beyond what's in git.
5. **Five tables and one bucket have no DDL in the repo:** `review_projects`,
   `compliance_checks`, `session_messages`, `bdc_section_map`, `rate_limits`,
   and the public `pdfs` storage bucket. Migration 009's header says
   migrations 003–006 and 008 were never committed. A fresh Supabase cannot
   be rebuilt from git today. `sandbox/supabase/schema.sql` reconstructs them
   from how the code uses them.
6. **`scripts/ingest_bdc.py` writes the string `"None"` for missing dates**
   (`str(header["bdc_date"])` at `:283-284`). A `date`-typed column would reject the insert.

## Minor

7. `frontend/src/lib/api.ts:17`'s comment says the collection is
   `"specs_2019_v2"`, but the UI sends `specs_2019`.
8. `/api/query` is called without an `Authorization` header, so the backend
   can't attribute or rate-limit chat queries to a user.
