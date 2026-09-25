#!/usr/bin/env bash
# NJDOT local sandbox driver. Run from anywhere: ./sandbox/sandbox.sh <cmd>
#
#   init       create sandbox/.env (auto-filled from `supabase status` if the
#              Supabase CLI is installed and your local stack is running)
#   doctor     check Docker, Supabase reachability, and which DB objects exist
#   db-apply   add any missing schema to your local Supabase (additive only)
#   up         build + start mock-llm, neo4j, backend (Azure image), frontend (Vercel build)
#   seed       upload reference PDFs, seed built-in checks, ingest scheduling
#              manual if missing  (seed --all ingests every collection)
#   test       run the Playwright E2E suite, then collect every service log
#   logs       save all service logs to sandbox/logs/
#   checks     next build warnings, eslint, backend pytest inside the images
#   report     re-collect logs and rebuild sandbox/logs/REPORT.md
#   down       stop the sandbox containers (your Supabase is never touched)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
ENV_FILE="$HERE/.env"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml" --env-file "$ENV_FILE")

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

load_env() {
  [[ -f "$ENV_FILE" ]] || die "sandbox/.env missing — run ./sandbox/sandbox.sh init"
  set -a; # shellcheck disable=SC1090
  source "$ENV_FILE"; set +a
}

psql_docker() {  # psql inside a throwaway container, against your local Supabase DB
  docker run --rm -i --add-host host.docker.internal:host-gateway postgres:15-alpine \
    psql "$SUPABASE_DB_URL_FROM_DOCKER" -v ON_ERROR_STOP=1 -X -q "$@"
}

cmd_init() {
  if [[ -f "$ENV_FILE" ]]; then say "sandbox/.env already exists — leaving it alone"; return; fi
  cp "$HERE/sandbox.env.example" "$ENV_FILE"
  if command -v supabase >/dev/null 2>&1; then
    say "Reading keys from 'supabase status' (run from the folder of your Supabase project)"
    local status
    if status="$(cd "${SUPABASE_PROJECT_DIR:-$REPO}" && supabase status -o env 2>/dev/null)"; then
      get() { grep -E "^$1=" <<<"$status" | head -1 | cut -d= -f2- | tr -d '"'; }
      local api anon service jwt db
      api="$(get API_URL)"; anon="$(get ANON_KEY)"; service="$(get SERVICE_ROLE_KEY)"
      jwt="$(get JWT_SECRET)"; db="$(get DB_URL)"
      [[ -n "$api" ]] && sed -i.bak "s#^SUPABASE_URL=.*#SUPABASE_URL=$api#" "$ENV_FILE"
      [[ -n "$api" ]] && sed -i.bak "s#^SUPABASE_URL_FROM_DOCKER=.*#SUPABASE_URL_FROM_DOCKER=${api/127.0.0.1/host.docker.internal}#" "$ENV_FILE"
      [[ -n "$anon" ]] && sed -i.bak "s#^SUPABASE_ANON_KEY=.*#SUPABASE_ANON_KEY=$anon#" "$ENV_FILE"
      [[ -n "$service" ]] && sed -i.bak "s#^SUPABASE_SERVICE_ROLE_KEY=.*#SUPABASE_SERVICE_ROLE_KEY=$service#" "$ENV_FILE"
      [[ -n "$jwt" ]] && sed -i.bak "s#^SUPABASE_JWT_SECRET=.*#SUPABASE_JWT_SECRET=$jwt#" "$ENV_FILE"
      [[ -n "$db" ]] && sed -i.bak "s#^SUPABASE_DB_URL_FROM_DOCKER=.*#SUPABASE_DB_URL_FROM_DOCKER=${db/127.0.0.1/host.docker.internal}#" "$ENV_FILE"
      rm -f "$ENV_FILE.bak"
      ok "filled Supabase values from supabase status"
    else
      bad "supabase status failed — set SUPABASE_PROJECT_DIR=<your supabase project folder> and re-run, or fill sandbox/.env by hand"
    fi
  else
    say "Supabase CLI not found — fill the Supabase values in sandbox/.env by hand"
  fi
  say "Created sandbox/.env — review it, then: ./sandbox/sandbox.sh doctor"
}

cmd_doctor() {
  load_env
  say "Docker"
  docker info >/dev/null 2>&1 && ok "daemon running" || die "Docker is not running"
  say "Supabase at $SUPABASE_URL"
  if curl -fsS -m 5 "$SUPABASE_URL/auth/v1/health" -H "apikey: $SUPABASE_ANON_KEY" >/dev/null; then ok "auth reachable"; else bad "auth NOT reachable"; fi
  if curl -fsS -m 5 "$SUPABASE_URL/rest/v1/" -H "apikey: $SUPABASE_ANON_KEY" >/dev/null; then ok "rest reachable"; else bad "rest NOT reachable"; fi
  if curl -fsS -m 5 "$SUPABASE_URL/auth/v1/.well-known/jwks.json" | grep -q '"kty"'; then
    ok "JWKS published (asymmetric JWTs, like hosted Supabase)"
  else
    bad "no JWKS keys — backend will verify with SUPABASE_JWT_SECRET (HS256)"
  fi
  say "Database objects the app needs"
  psql_docker -At <<'SQL' | while IFS='|' read -r name present; do [[ "$present" == t ]] && ok "$name" || bad "$name (missing — ./sandbox/sandbox.sh db-apply)"; done
select 'table '||t, to_regclass('public.'||t) is not null from unnest(array[
  'chunks','session_chunks','conversations','messages','review_projects',
  'compliance_checks','session_messages','bdc_section_map','rate_limits']) t
union all
select 'function '||f, exists(select 1 from pg_proc where proname=f) from unnest(array[
  'match_chunks','keyword_search_chunks','match_session_chunks','keyword_search_session_chunks']) f
union all
select 'bucket '||b, exists(select 1 from storage.buckets where id=b) from unnest(array['review-files','pdfs']) b
union all
select 'rls policies on '||t, exists(select 1 from pg_policies where tablename=t) from unnest(array[
  'conversations','messages','review_projects','compliance_checks']) t;
SQL
  say "Data"
  psql_docker -At -c "select collection||': '||count(*)||' chunks' from chunks group by collection order by 1" 2>/dev/null | sed 's/^/  · /' || true
  psql_docker -At -c "select 'built-in checks: '||count(*) from compliance_checks where user_id is null" 2>/dev/null | sed 's/^/  · /' || true
}

cmd_db_apply() {
  load_env
  say "Applying missing schema to $SUPABASE_DB_URL_FROM_DOCKER (additive only)"
  has() { [[ "$(psql_docker -At -c "select $1")" == t ]]; }

  if ! has "to_regclass('public.chunks') is not null and to_regclass('public.session_chunks') is not null and exists(select 1 from pg_proc where proname='keyword_search_session_chunks')"; then
    say "base RAG tables/functions missing → backend/sql/local_setup.sql"
    psql_docker < "$REPO/backend/sql/local_setup.sql"
  else ok "chunks / session_chunks / search functions present"; fi

  if ! has "to_regclass('public.conversations') is not null"; then
    say "conversations missing → migrations 001 + 002"
    psql_docker < "$REPO/backend/migrations/001_conversations.sql"
    psql_docker < "$REPO/backend/migrations/002_conversations_updated_at.sql"
  else ok "conversations / messages present"; fi

  say "sandbox/supabase/schema.sql (tables with no DDL in the repo, buckets, RLS)"
  psql_docker < "$HERE/supabase/schema.sql"

  if ! has "exists(select 1 from pg_policies where tablename='objects' and policyname='review_files_insert_own')"; then
    say "review-files storage policies missing → backend/sql/storage_review_files_policies.sql"
    psql_docker < "$REPO/backend/sql/storage_review_files_policies.sql"
  else ok "review-files storage policies present"; fi
  ok "done — re-run doctor to confirm"
}

cmd_up() {
  load_env
  say "Building and starting the sandbox (first build takes a while)"
  "${COMPOSE[@]}" up -d --build --wait
  "${COMPOSE[@]}" ps
  say "Frontend  $FRONTEND_URL"
  say "Backend   $API_URL  (docs: $API_URL/docs)"
  say "Mock LLM  $MOCK_LLM_URL/__stats"
  say "Neo4j     http://localhost:7475"
}

cmd_seed() {
  load_env
  "${COMPOSE[@]}" run --rm --no-deps \
    -v "$HERE/seed:/sandbox-seed:ro" \
    --entrypoint /home/site/wwwroot/antenv/bin/python \
    backend /sandbox-seed/seed.py "$@"
}

cmd_logs() {
  load_env
  mkdir -p "$HERE/logs"
  # SANDBOX_LOGS_SINCE (set by `test`) limits logs to the current run.
  local since=()
  [[ -n "${SANDBOX_LOGS_SINCE:-}" ]] && since=(--since "$SANDBOX_LOGS_SINCE")
  for svc in backend frontend mock-llm neo4j; do
    "${COMPOSE[@]}" logs --no-color --timestamps "${since[@]}" "$svc" > "$HERE/logs/$svc.log" 2>&1 || true
  done
  ok "logs written to sandbox/logs/"
}

cmd_checks() {  # static checks inside the production-shaped images → logs/static-*.log
  load_env
  mkdir -p "$HERE/logs"
  say "next build (Vercel image) — build-time warnings"
  docker run --rm njdot-sandbox-frontend sh -c 'rm -rf .next && npm run build 2>&1' > "$HERE/logs/static-next-build.log" 2>&1 || true
  say "eslint (not run by Vercel's build, reported for completeness)"
  docker run --rm njdot-sandbox-frontend sh -c 'npx eslint src 2>&1' > "$HERE/logs/static-eslint.log" 2>&1 || true
  say "backend unit tests (App Service image)"
  docker run --rm --entrypoint sh njdot-sandbox-backend -c \
    'cd /home/site/wwwroot && antenv/bin/pip install -q pytest >/dev/null 2>&1; antenv/bin/python -m pytest -q --ignore=tests/integration -p no:cacheprovider 2>&1' \
    > "$HERE/logs/static-pytest.log" 2>&1 || true
}

cmd_test() {
  load_env
  cd "$HERE/e2e"
  [[ -d node_modules ]] || npm ci
  npx playwright install chromium
  local rc=0
  export SANDBOX_LOGS_SINCE
  SANDBOX_LOGS_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  SANDBOX_COMPOSE="docker compose -f $HERE/docker-compose.yml --env-file $ENV_FILE" \
    npx playwright test "$@" || rc=$?
  cmd_logs
  cmd_checks
  node "$HERE/report/build-report.mjs"
  say "Every error + warning: sandbox/logs/REPORT.md  ← send this back for triage"
  say "HTML report: npx --prefix sandbox/e2e playwright show-report sandbox/e2e/playwright-report"
  say "Per-test browser console + network logs: sandbox/e2e/test-results/"
  return $rc
}

cmd_down() {
  load_env
  "${COMPOSE[@]}" down "$@"
}

case "${1:-}" in
  init) cmd_init ;;
  doctor) cmd_doctor ;;
  db-apply) cmd_db_apply ;;
  up) cmd_up ;;
  seed) shift; cmd_seed "$@" ;;
  test) shift; cmd_test "$@" ;;
  logs) cmd_logs ;;
  checks) cmd_checks ;;
  report) cmd_logs; node "$HERE/report/build-report.mjs" ;;
  down) shift; cmd_down "$@" ;;
  *) sed -n '2,17p' "$0"; exit 1 ;;
esac
