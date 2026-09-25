# Verification Log — API Error and Warning Logging Plan

Verifying [2026-09-15-api-error-warning-logging.md](2026-09-15-api-error-warning-logging.md) against the real codebase before executing it.

**Method:** 8 parallel verification agents (one per plan task, plus test-harness and cross-cutting), each exact-matching every quoted snippet against the real files and running the repo venv to check behavioral claims. Every reported mismatch then faced two adversarial refuters — one re-reading the file character-by-character, one attacking the severity — and survived only if neither could refute it. A completeness critic then enumerated every claim in the plan and reported what nobody checked. 41 agents, 0 errors, 154 distinct claims checked.

**Headline: no blockers. 2 major, 12 minor, 2 findings refuted and dropped.** The plan is applicable as written — every "Find this" snippet in all six tasks matches the real files character-for-character. But one task instruments code the server never runs, and one task cannot be executed in the order the plan implies.

| Area | Claims checked | Confirmed | Refuted |
|---|---|---|---|
| Task 1 — middleware | 11 | 2 minor | 0 |
| Task 2 — callback handler | 14 | 1 minor | 0 |
| Task 3 — callback wiring | 24 | 1 minor | 0 |
| Task 4 — outbound non-LLM | 24 | 1 major, 2 minor | 0 |
| Task 5 — ingestion | 22 | 2 minor | 0 |
| Task 6 — review degraded | 14 | 1 minor | 0 |
| **Test harness** | **14** | **0** | 0 |
| Cross-cutting | 31 | 1 major, 3 minor | 2 |

---

## 1. What held (the parts to trust)

The highest-risk area came back completely clean. **All 14 test-harness claims hold:**

- `from tests.logcapture import capture_logs` resolves from `backend/` **both** under `python -m pytest backend/tests` and as a plain script run, with no `__init__.py` needed (implicit namespace packages).
- No `conftest.py`, `pytest.ini`, `pyproject.toml`, `setup.cfg` or `tox.ini` anywhere that would change rootdir or `sys.path`.
- The existing suite **currently passes** — so "full suite stays green" is a meaningful gate, not a pre-broken one.
- `.venv/Scripts/python.exe -m pytest` → pytest 9.1.1. `backend/.venv` confirmed to lack pytest, as the plan says.
- `git check-ignore` confirms `backend/app/llm_logging.py` and `backend/tests/logcapture.py` **would** stage past the blanket-`*` gitignore.
- The `logging.Handler` capture design works: named-logger attachment captures without touching `propagate`.
- `TestClient(app, raise_server_exceptions=False)` returns a 500 rather than re-raising.

Also verified clean:

- **Every "Find this" snippet in all six tasks matches character-for-character.** This was the primary risk and it is fully discharged.
- `BaseCallbackHandler` has all five methods the plan overrides; `def on_llm_error(self, error, **kwargs)` is compatible with how LangChain invokes it; `exc_info=<exception instance>` produces a record with `exc_info` set.
- `has_graph` is assigned before the `invoke_config` construction in `session.py` (the `_run_name` refactor is safe).
- `filename` is in scope at the new `pdf.py` log lines; `p_num` is in scope at both `session_chunker.py` sites that reference it, and correctly *not* used at the outer handler.
- `_find_caption` has both `page_pdf` and `idx`; `_find_tables` has exactly one caller.
- Task 3's list of 7 LLM modules is exhaustive — no missed `get_langfuse_handler` call site.
- `_validate_owned_path("user-1/%2Fx/y", ...)` does fire the `except` branch and `("other-user/...", ...)` the prefix branch, as the tests assume.

---

## 2. Major findings

### M1 — Task 4's Supabase/Neo4j half instruments code the server never runs

**Severity: major.** `wrong-behavior`. `backend/app/database.py`, `backend/app/neo4j_client.py`.

The plan's Goal promises outbound coverage for "an OpenAI/Anthropic/**Supabase/Neo4j**/httpx call that failed", and Task 4 Steps 3–4 deliver it by adding `logger.error(...)` inside `Database.test_connection` and `Neo4jClient.test_connection`.

**Those functions are never called by the running server.** `Database.test_connection` has exactly one caller — `database.py:60`, inside that module's own `if __name__ == "__main__":` block. Identically for `Neo4jClient.test_connection` at `neo4j_client.py:69`. A repo-wide `rg --no-ignore "test_connection"` over `backend/` finds no other caller.

Meanwhile no task in the plan touches a single **runtime** Supabase call (`db.table(...).execute()` in `api/conversations.py`, `api/session.py`, `api/review.py`, `ingestion/chunk_store.py`, `retrieval/bdc_matcher.py`) or runtime Neo4j call (`graph.query(...)` at `api/review.py:549,560,612,756` and `api/session.py:155,338,358`).

And the plan's own test passes by calling `Database.test_connection()` directly — proving nothing about production.

> **Consequence:** after the whole plan lands, a real Supabase or Neo4j failure still produces no `app.database` / `app.neo4j_client` line. The only thing that surfaces it is Task 1's generic middleware 500, which loses exactly the attribution Task 4's title promises.

**Fix — choose one:**
- **(a)** Rename that portion to what it is — "convert two dev-time connection probes from `print()` to `logging`" — and state plainly that runtime Supabase/Neo4j failures are covered only by the Task 1 middleware.
- **(b)** Add a step that logs at a real runtime call site. Cheapest real coverage: the existing bare `except` blocks in `review.py`'s `_build_static_doc_search_fn` and `ingestion/chunk_store.py`.

*Recommendation: (b) for `graph.query` in the review pipeline, (a) for the rest. Option (a) alone means the plan does not do what its own Goal line says.*

---

### M2 — Task 6 cannot run before Task 3, and says nothing about it

**Severity: major.** `ordering-dependency`. Plan document.

Task 6's Interfaces block lists one dependency — `capture_logs` from Task 1 — presenting the task as independent of Tasks 2–3. But its test calls a parameter only Task 3 creates:

```python
project_id="p1", user_id="user-1", callbacks=[],
```

The real signature today is `eval_engine.py:440-456`, whose last parameter is line 455: `    langfuse_handler,`. Verified empirically — `callbacks=[]` today raises `TypeError: _evaluate_one_check() got an unexpected keyword argument 'callbacks'`.

> **Consequence:** the plan header recommends `superpowers:subagent-driven-development`, which dispatches tasks declaring no dependency **in parallel**. Run Task 6 before Task 3 and the sixth test errors with a `TypeError` instead of the documented `len(records) == 0` failure — *and it still fails that way after Task 6's implementation step*, so the engineer chases a nonexistent bug.

**Fix:** add to Task 6's Interfaces — "Consumes: `_evaluate_one_check`'s `callbacks` parameter (renamed from `langfuse_handler` in Task 3) — **Task 6 must land after Task 3**", and amend Step 2's expectation to name the `TypeError` if Task 3 has not landed.

---

## 3. Minor findings

### Wrong behavior / false claims committed into source

**m1 — the CORS ordering rationale in Task 1's comment is false.** The *mechanical* half is right (verified: `add_middleware` does `user_middleware.insert(0, ...)`, so last-added is outermost, and registering the logger first leaves CORS outermost). The stated *consequence* is wrong: `build_middleware_stack` prepends `ServerErrorMiddleware` ahead of all user middleware, so it is outside CORS in **every** ordering. Measured with `Origin: http://localhost:3000` — plan's order: `/boom` → 500, `access-control-allow-origin = None`; reversed order: `/boom` → 500, `access-control-allow-origin = None`. **Identical.** A *handled* non-2xx does get the header in either order.
→ Keep the order and the first half of the comment; replace the "which is required because…" sentence with: *"Adding this one first leaves CORSMiddleware outermost, so a 4xx/5xx response still gets its CORS headers on the way out. Note an unhandled exception is re-raised past CORSMiddleware to Starlette's ServerErrorMiddleware, which sits outside all user middleware — that synthesized 500 carries no CORS headers in any ordering."*

**m2 — `on_retry` never fires in this app.** In langchain-core 1.4.9 `on_retry` is emitted from exactly one place: `language_models/llms.py:101,117`, inside `create_base_retry_decorator` — the legacy `BaseLLM` retry path. No chat model and no `Runnable.with_retry()` emits it. Verified empirically: a `RunnableLambda(...).with_retry(...)` invocation produced two `on_chain_error` records and zero `on_retry`. ChatOpenAI/ChatAnthropic retries happen inside the provider SDK, below the callback layer.
→ The plan's shipped docstring asserts *"the earliest signal that a provider is degrading"* and the test docstring says *"the only signal that OpenAI is degrading before `.with_fallbacks()` switches to Claude."* Both false. Either drop `on_retry` and its two tests, or soften both claims. `on_llm_error` — which *does* fire — remains the real fallback signal.

**m3 — Task 4's `logger.info("Supabase client initialized")` is dropped at startup.** `api/query.py:67` runs `_db = get_db()` at **module import** time, and `main.py` imports that router at line 41 — *before* `logging.basicConfig(...)` runs at `main.py:47-50`. At that moment the root logger is still WARNING with no handlers (uvicorn's dictConfig defines only `uvicorn*` loggers, no `root`), so the `INFO` record is not emitted at all — `logging.lastResort` handles WARNING and above only.
→ The `OK Supabase client initialized` line visible in uvicorn startup output today **disappears**. Fix: move `logging.basicConfig(...)` above the router-import block in `main.py`, or accept and note it.
→ *Side discovery:* `python backend/app/database.py` is already dead — it raises `ImportError: attempted relative import with no known parent package`, because the `sys.path` shim at the top of that file is inside a `'''...'''` string literal.

**m4/m5 — a stale comment at `eval_engine.py:686`.** `# review_span/langfuse_handler stay None and checks just don't trace.` is a fifth `langfuse_handler` reference Task 3's Files list omits. After Task 3 the name exists nowhere in the module, and the claim is also untrue — `build_callbacks` always returns at least one handler.
→ Add to Task 3 Step 3: update line 686 to `# review_span stays None and checks just don't trace (the logging callback still runs).`

### Wrong line numbers (navigational only — every snippet still matches)

| Plan cites | Actually | Fix |
|---|---|---|
| `main.py:66-82` | imports at 38-44, CORS at 64-82; line 66 is mid-list | `:38-44, :64-82` |
| `database.py:33,44-46` | 33 right; block is 41-47; imports 9-11 uncited | `:9-11,33,41-47` |
| `api/pdf.py:50-55` | 50-51 untouched; block is 52-61; imports 13-19 uncited | `:13-19,52-61` |
| `table_extractor.py:170-181` | hunk starts at 169 | `:169-181` |
| `session_chunker.py:289-294` | guard is 289-295 | `:289-295` |
| `session_chunker.py:319-321` | outer guard is 320-323 | `:320-323` |
| `eval_engine.py:483-499` (Task 6) | right at HEAD, but Task 3 adds a line → 484-500 | cite the symbol, not the number |

---

## 4. Found by the completeness critic

**s1 — CRLF vs LF, and the repo is not uniform.** Measured byte-exactly across all 17 modified files: **15 are CRLF, 2 are LF** (`backend/app/auth.py`, `backend/app/ingestion/session_chunker.py`). Every snippet in the plan is written LF-only.
> This is the single failure mode that would trip **every task at once** — and a blanket `\r\n` conversion would then break the two LF files, including Task 5's `session_chunker.py`.
→ Add to Global Constraints: name the two LF files explicitly, and require an edit tool that normalizes line endings.

**s2 — Task 4 misses `pdf.py`'s transport-failure branch.** Step 7's edits start at line 52, inside `if upstream.status_code != 200:`. The earlier branch at `pdf.py:40-45` — `except httpx.RequestError` → bare 502 raise — is never touched, and no test covers it.
→ Storage being unreachable (DNS, reset, timeout) produces no `app.api.pdf` line at all, only the generic middleware 502. That is exactly the case Task 4's Goal bullet promises to attribute. Fix: a fourth find/replace adding `logger.error("Storage unreachable for doc=%r file=%r: %s", doc_name, filename, exc, exc_info=True)` before the raise (`filename` is bound at `pdf.py:37`).

**s3 — one LLM-bearing `.invoke()` passes no config at all.** `graph_neo4j/tools.py:224`, `cypher_chain.invoke({"query": question})`. It is correctly absent from Task 3's seven-module list (it builds no callback list), and it is reached from inside `session.py`'s `agent.invoke(..., config=invoke_config)`, so LangChain's runnable-config contextvar *should* propagate the handler into it. **Nobody exercised that.** If propagation does not hold, a Cypher-chain LLM failure logs nothing — and Task 2's docstring claim *"Every LLM, tool and retriever call in this app goes through LangChain's callback system"* is false for the chat agent's main path.

---

## 5. Residual risk — what nobody checked

1. **Manual verification step 4** (junk `OPENAI_API_KEY` → `app.llm` ERRORs, review still completes via Claude). Needs live credentials. This is the plan's own *"single clearest confirmation that Tasks 2 and 3 work"* — and it is coupled to m2 above: a junk key produces `on_llm_error` (which fires), not the retry line (which never does).
2. **Manual verification step 6** (no `token=<jwt>` in console during a live SSE stream). Needs a running server + signed-in session.
3. **`test_outbound_logging.py` (7 tests) and `test_review_degraded_logging.py` (6 tests) were never executed** against the planned implementations. Preconditions were all confirmed — patch targets resolve, control flow reaches each asserted branch — but the final assertions are unobserved. Test counts are arithmetically correct.
4. **Whether the runnable-config contextvar reaches `tools.py:224`** (see s3). Needs a run, not a read.
5. **Langfuse trace-parenting side effect.** Task 3 replaces `get_langfuse_handler(...) if project_id else None` with an unconditional `build_callbacks(trace_id=... if project_id else None)`, so a Langfuse handler with `trace_id=None` is now constructed where previously none was. Whether that creates orphan traces in the real Langfuse project is unverified.
6. **Suite-scale interaction between the six new test files** — one file's `capture_logs` level mutation leaking into another was never exercised.

---

## 6. Refuted and dropped

Both refuted findings claimed Tasks 5 and 6 are out of the plan's stated scope. Both were wrong: the plan says so in three places — the Architecture paragraph (*"Everything else (Supabase, Neo4j, raw httpx, PDF parsing, review gating) gets an explicit `logger.warning`/`logger.error`…"*), the Global Constraints level policy (*"an extraction returned nothing, a table was skipped, a check downgraded to Missing"*), and each task's own preamble. Recorded here so it is not re-litigated.

---

## 7. Recommendation for the re-plan

The plan is sound; it is also bigger than it needs to be. Ranked by value per unit of work:

**Keep, merged into one task — Tasks 2 + 3 (LLM callbacks).** Highest value in the whole plan. One handler class makes every LLM failure in the app log itself, including the OpenAI→Claude fallback that is completely invisible today. They were only split because Task 2 is testable alone; merging costs nothing and removes a handoff. Drop `on_retry` (m2) — it is dead code here.

**Keep as-is — Task 1 (middleware).** ~30 lines, covers every route present and future. Fix the false CORS comment (m1) and consider moving `basicConfig` above the router imports (m3), which also fixes the dropped startup line.

**Cut down — Task 4.** The JWKS, httpx-admin and `pdf.py` halves are real outbound coverage; keep them, plus the missing transport branch (s2). The Supabase/Neo4j half is dead code (M1) — either relabel it honestly or replace it with one runtime `graph.query` call site.

**Keep, must follow the LLM task — Task 6.** Real value: these are the branches that make a check silently report Missing. Just needs the dependency declared (M2).

**Optional — Task 5 (ingestion).** In scope, correctly specified, lowest value of the six. The natural thing to drop if you want this to land in one sitting.

That is **4 tasks instead of 6**, with the two that produce most of the benefit (middleware + LLM callbacks) landing first and independently.

---

## 8. Outcome

All six tasks were executed rather than re-planned — with the corrections above folded in. Run in three dependency waves (1/2/4/5 parallel → 3 → 6), followed by an independent adversarial audit of the full diff.

**Result: 153 tests passing** (was 143 before this work; 33 new tests), all 19 touched modules import cleanly including `app.main`.

Every finding in this document was applied except two, both deliberate:

- **M1** was addressed by doing *both* halves rather than choosing: the dev-time probes keep their `print()`→`logging` conversion but their docstrings now say plainly that the running server never calls them, **and** real runtime coverage was added at `ingestion/chunk_store.py` — the one Supabase site both `/api/review` and `/api/session` funnel chunk writes through, which previously failed with no attribution at all. Two new tests cover it.
- **The `auth.py` per-request WARNING volume risk** (a legacy-HS256 Supabase project would log on every authenticated request) is left as-is. This deployment issues ES256, so the branch is quiet here; noted rather than pre-optimized.

The audit also closed two of the residual risks in §5 by running them: LangChain's callback contextvar **does** reach the config-less `cypher_chain.invoke` at `graph_neo4j/tools.py:224` (risk #4), and the new middleware does **not** interfere with the SSE or `BackgroundTasks` paths.

Still open, unchanged: manual verification steps 4 and 6 need live credentials and a browser session.

One hygiene item worth a separate decision: `.gitattributes` has `*.sql text eol=lf` but nothing for `*.py`, so line-ending normalization currently depends on each machine's `core.autocrlf`. Adding `*.py text eol=lf` would make it deterministic — deliberately not bundled into this branch.
