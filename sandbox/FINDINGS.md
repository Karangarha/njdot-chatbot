# Sandbox findings — status after the fix pass (2026-09-25)

Plan: [`docs/superpowers/plans/2026-09-25-fix-sandbox-findings.md`](../docs/superpowers/plans/2026-09-25-fix-sandbox-findings.md).
The original triage of the 2026-09-25 run is in git history (this file before
this commit) and the raw report is [`reports/2026-09-25-REPORT.md`](reports/2026-09-25-REPORT.md).

**Clean re-run** (`down → db-apply → up → seed → test`):
- **E2E:** 36 of 36 pass. The old 37 minus `change-password needs a token`, whose endpoint was removed.
- **Browser:** 0 errors.
- **Backend log:** no Langfuse or `neo4j.notifications` lines. The only traceback is the intentional OpenAI-outage test.
- **Static checks:** ESLint 0 problems. `next build` shows no deprecation warning. Backend pytest 332 pass in the App Service image with no warnings.
- **Report:** after the fixes, [`reports/2026-09-25-after-fixes-REPORT.md`](reports/2026-09-25-after-fixes-REPORT.md). Its backend-log counts cover only the final restart; the E2E results and static checks are from the fixed code.

| # | Finding | Status |
|---|---|---|
| 1 | Azure Startup Command | ✅ Deploy notes fixed. **Human:** set `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` in the portal. |
| 2 | Session Q&A endpoints had no auth | ✅ Sign-in required everywhere, plus an ownership check (`app/project_access.py`). |
| 3 | Deleting a chat didn't delete it | ✅ Migration `supabase/migrations/20260101000011_…`: UPDATE/DELETE policies, and restores `messages` ON DELETE CASCADE. **Human:** applied by the migrations pipeline once enabled (see `supabase/README.md`). |
| 4 | Changing password logged you out | ✅ The page uses `updateUser`, and the admin endpoint is removed. |
| 5 | Password reset account takeover | ⏸ **Deferred by the owner.** Still open; fix it before public launch. |
| 6 | `middleware.ts` deprecated | ✅ Renamed to `proxy.ts`. |
| 7 | `/api/query` leaked internals | ✅ Generic 500 detail, for `/api/query` and `/api/debug`. |
| 8 | Tables with no DDL in git | ⛔ **Blocked:** needs the production schema read (plan Task 7 Step 1). `supabase/schema.sql` is known to be wrong for `rate_limits` and `bdc_section_map`, so no migration was written from it. |
| 9 | APOC requirement | ✅ Added to the README prerequisites. |
| 10 | `"None"` BDC dates | ✅ Stored as NULL; the chunk metadata is fixed too. **Human:** clean live rows (plan Task 10 Step 5). |
| — | Langfuse / Neo4j log noise, ESLint, pytest warning | ✅ All cleared. |

**Windows note:** `sandbox.sh seed` breaks under Git Bash's path conversion.
Run the seed step's `docker compose run` command yourself, with
`MSYS_NO_PATHCONV=1` and a `cygpath -m` mount path.
