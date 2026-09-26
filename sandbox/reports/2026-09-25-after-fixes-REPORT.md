# NJDOT sandbox run report

Generated 2026-09-26T03:28:00.079Z

## Summary

**Tests:** 36 passed · 0 failed · 0 skipped · 36 total

| Source | Errors (distinct) | Warnings (distinct) |
|---|---|---|
| Browser (console + network) | 0 | 16 |
| Backend — Azure container | 0 + 0 tracebacks | 0 |
| Frontend — Vercel container | 0 | 0 |
| Mock LLM | 0 | 0 |
| Neo4j | 0 | 0 |
| Static checks (build / lint / pytest) | 0 | 1 |

## Failed tests

_none_

## Security findings & observations

- **security** — Reset needs only the email: /api/auth/request-reset returns a usable reset_token to whoever asks (backend/app/api/auth.py). Passing this test confirms anyone who knows an email can take over the account. <sub>(password flows › forgot password: email → new password → login with it)</sub>
- **note** — Special Provision PDF not in fixtures — slot left empty <sub>(Document Review — full run with the Route 49 files › upload → review (SSE) → results → Q&A → re-run → project list)</sub>
- **note** — no clickable (verified) citation pills in this result <sub>(Document Review — full run with the Route 49 files › upload → review (SSE) → results → Q&A → re-run → project list)</sub>
- **security** — backend/app/api/session.py: /upload, /status, /query and /messages take no Authorization at all — anyone holding a project UUID can read its Q&A history and query its private documents. <sub>(backend: auth on protected routes › @security session endpoints should require auth)</sub>
- **observed** — /api/query with OpenAI down → 500 {"detail":"The assistant could not answer right now. Please try again."} <sub>(backend: LLM failure handling › OpenAI outage on /api/query gives a clean JSON error, not a hang or traceback)</sub>
- **observed** — {"openai:query_expansion":6,"openai:embeddings":36,"openai:rag_answer":6,"openai:structured":92,"openai:entities":5,"openai:tool_call":1,"openai:cypher":1,"openai:tool_final":1,"openai:forced_500":9} <sub>(backend: LLM failure handling › the LLM was actually exercised (mock counters))</sub>

## Skipped tests

_none_

## Browser errors

_none_

## Browser warnings

- **×62** `request aborted/failed: HEAD http://127.0.0.1:54321/rest/v1/compliance_checks?select=id&user_id=eq.7f4dead7-3859-48e3-a05a-781222397f9a (net::ERR_ABORTED)`  
  <sub>in: Document Review — checklist manager › toggle, add, edit, delete, reset — each hits compliance_checks · Document Review — full run with the Route 49 files › no checks selected → Run Review explains why</sub>
- **×17** `request aborted/failed: GET http://localhost:3000/chat?_rsc=71u3d (net::ERR_ABORTED)`  
  <sub>in: route protection (middleware.ts) › signed-in /login, /signup, /forgot-password redirect to /chat · signup › new account signs up, lands on /chat and shows initials · login / logout › login → user menu → sign out → /chat is protected again · password flows › change password from the user menu +13 more</sub>
- **×15** `request aborted/failed: GET http://localhost:3000/chat?_rsc=5c339 (net::ERR_ABORTED)`  
  <sub>in: route protection (middleware.ts) › signed-in /login, /signup, /forgot-password redirect to /chat · login / logout › login → user menu → sign out → /chat is protected again · password flows › change password from the user menu · Smart Assistant chat › empty state: send disabled, citations placeholder, tabs +11 more</sub>
- **×3** `request aborted/failed: GET http://localhost:3000/login?_rsc=1r34m (net::ERR_ABORTED)`  
  <sub>in: landing page › renders and every link goes to the right place · landing page › static assets load (no broken images)</sub>
- **×3** `request aborted/failed: GET http://localhost:8000/api/pdf/SchedulingManual (net::ERR_ABORTED)`  
  <sub>in: Smart Assistant chat › citation → PDF modal opens /api/pdf and closes (button, Escape, backdrop)</sub>
- **×2** `request aborted/failed: GET http://localhost:3000/login?_rsc=p37cr (net::ERR_ABORTED)`  
  <sub>in: landing page › renders and every link goes to the right place · landing page › static assets load (no broken images)</sub>
- **×2** `request aborted/failed: GET http://localhost:3000/login?_rsc=1bjvx (net::ERR_ABORTED)`  
  <sub>in: landing page › renders and every link goes to the right place</sub>
- **×2** `request aborted/failed: GET http://localhost:3000/login?_rsc=ynvfz (net::ERR_ABORTED)`  
  <sub>in: signup › new account signs up, lands on /chat and shows initials · signup › signing up an existing email shows an error</sub>
- **×1** `request aborted/failed: GET http://localhost:3000/login?_rsc=1o94a (net::ERR_ABORTED)`  
  <sub>in: signup › mismatched passwords are rejected client-side</sub>
- **×1** `request aborted/failed: GET http://localhost:3000/chat?_rsc=6k3yh (net::ERR_ABORTED)`  
  <sub>in: signup › new account signs up, lands on /chat and shows initials</sub>
- **×1** `http 400 (expected or third-party): POST http://127.0.0.1:54321/auth/v1/token?grant_type=password -> 400`  
  <sub>in: login / logout › wrong password shows the Supabase error</sub>
- **×1** `request aborted/failed: POST http://127.0.0.1:54321/auth/v1/logout?scope=global (net::ERR_ABORTED)`  
  <sub>in: login / logout › login → user menu → sign out → /chat is protected again</sub>
- **×1** `request aborted/failed: GET http://localhost:3000/chat?_rsc=1kz0m (net::ERR_ABORTED)`  
  <sub>in: password flows › change password from the user menu</sub>
- **×1** `request aborted/failed: GET http://localhost:3000/chat?_rsc=1oolu (net::ERR_ABORTED)`  
  <sub>in: password flows › change password from the user menu</sub>
- **×1** `request aborted/failed: GET http://localhost:3000/chat?_rsc=1l8gh (net::ERR_ABORTED)`  
  <sub>in: password flows › forgot password: email → new password → login with it</sub>
- **×1** `http 500 (expected or third-party): POST http://localhost:8000/api/query -> 500`  
  <sub>in: Smart Assistant chat › backend error surfaces as a red error bubble</sub>

## Backend tracebacks

_none_

## Backend errors

_none_

## Backend warnings

_none_

## Frontend server errors

_none_

## Frontend server warnings

_none_

## Mock LLM

Calls by kind: `{}`

Errors:
_none_

Warnings:
_none_

## Neo4j errors

_none_

## Neo4j warnings

_none_

## Static checks

Backend unit tests: `332 passed in 6.20s`

Errors:
_none_

Warnings:
- **×1** `next build: ⚠ No build cache found. Please configure build caching for faster rebuilds. Read more: https://nextjs.org/docs/messages/no-cache`

## All tests

| Status | Test | Time |
|---|---|---|
| passed | landing page › renders and every link goes to the right place | 2.0s |
| passed | landing page › static assets load (no broken images) | 1.4s |
| passed | route protection (middleware.ts) › signed-out /chat redirects to /login | 0.5s |
| passed | route protection (middleware.ts) › signed-out /update-password bounces to /forgot-password | 0.5s |
| passed | route protection (middleware.ts) › signed-in /login, /signup, /forgot-password redirect to /chat | 2.2s |
| passed | signup › mismatched passwords are rejected client-side | 0.6s |
| passed | signup › new account signs up, lands on /chat and shows initials | 1.1s |
| passed | signup › signing up an existing email shows an error | 0.5s |
| passed | login / logout › wrong password shows the Supabase error | 0.7s |
| passed | login / logout › login → user menu → sign out → /chat is protected again | 1.2s |
| passed | password flows › change password from the user menu | 3.8s |
| passed | password flows › forgot password: email → new password → login with it | 1.6s |
| passed | password flows › forgot password for an unknown email does not reveal that it is unknown | 0.6s |
| passed | Smart Assistant chat › empty state: send disabled, citations placeholder, tabs | 1.1s |
| passed | Smart Assistant chat › ask with collection filter → full DB + API round trip | 2.0s |
| passed | Smart Assistant chat › suggestion pills send their question | 1.4s |
| passed | Smart Assistant chat › citation → PDF modal opens /api/pdf and closes (button, Escape, backdrop) | 1.6s |
| passed | Smart Assistant chat › history: reopen a conversation, New Chat clears it | 1.8s |
| passed | Smart Assistant chat › delete a conversation removes it (and it stays gone after reload) | 2.8s |
| passed | Smart Assistant chat › backend error surfaces as a red error bubble | 1.1s |
| passed | Document Review — upload form › Run Review is enabled only with schedule + narrative; remove file works | 1.4s |
| passed | Document Review — upload form › 'What gets checked' panel lists every category | 1.0s |
| passed | Document Review — checklist manager › toggle, add, edit, delete, reset — each hits compliance_checks | 2.6s |
| passed | Document Review — full run with the Route 49 files › upload → review (SSE) → results → Q&A → re-run → project list | 23.8s |
| passed | Document Review — full run with the Route 49 files › no checks selected → Run Review explains why | 9.6s |
| passed | backend: platform + contract › health and OpenAPI list every route the frontend calls | 0.2s |
| passed | backend: platform + contract › CORS: frontend origin allowed, random origin not | 0.0s |
| passed | backend: platform + contract › /api/query validation: blank → 400, missing → 422, JSON error bodies | 0.0s |
| passed | backend: platform + contract › /api/pdf serves reference PDFs and 404s unknown docs | 0.2s |
| passed | backend: auth on protected routes › conversations need a valid Supabase JWT | 0.2s |
| passed | backend: auth on protected routes › @security an unsigned (alg=none) JWT is rejected | 0.0s |
| passed | backend: auth on protected routes › review PDF + rerun reject missing tokens | 0.0s |
| passed | backend: auth on protected routes › reset-password rejects a forged token | 0.0s |
| passed | backend: auth on protected routes › @security session endpoints should require auth | 0.0s |
| passed | backend: LLM failure handling › OpenAI outage on /api/query gives a clean JSON error, not a hang or traceback | 4.0s |
| passed | backend: LLM failure handling › the LLM was actually exercised (mock counters) | 0.0s |
