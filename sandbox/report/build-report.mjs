#!/usr/bin/env node
// Builds sandbox/logs/REPORT.md — every error and warning from one sandbox
// run, in one place, deduplicated with counts:
//   · E2E results (failures, skips, security/observed annotations)
//   · browser console errors/warnings, failed + 4xx/5xx requests, per test
//   · backend (Azure container) ERROR/WARNING lines and full tracebacks
//   · frontend (Vercel container) server errors/warnings
//   · mock LLM internal errors / forced failures, Neo4j errors/warnings
// Usage: node sandbox/report/build-report.mjs   (./sandbox.sh test runs it)
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SANDBOX = path.resolve(HERE, "..");
const LOGS = path.join(SANDBOX, "logs");
const E2E = path.join(SANDBOX, "e2e");

const read = (p) => (fs.existsSync(p) ? fs.readFileSync(p, "utf8") : null);
// `docker compose logs --timestamps` lines look like
// "backend-1  | 2026-09-25T22:56:09.634Z <message>" — drop both parts.
const strip = (s) => s.replace(/^[\w.-]+\s+\|\s?/, "").replace(/^\d{4}-\d\d-\d\dT[\d:.]+Z\s?/, "");
const norm = (s) =>
  strip(s)
    .replace(/\d{4}-\d{2}-\d{2}[ T][\d:.,]+/g, "<ts>")
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, "<uuid>")
    .replace(/sandbox-\w+-\d+@/g, "sandbox-<user>@")
    .replace(/\b\d+(\.\d+)?\s?ms\b/g, "<n> ms")
    .replace(/token=[\w.-]+/g, "token=<jwt>")
    .trim();

class Bucket {
  constructor() { this.map = new Map(); }
  add(text, where) {
    const key = norm(text).slice(0, 600);
    const cur = this.map.get(key) ?? { count: 0, where: new Set(), sample: strip(text).trim() };
    cur.count++;
    if (where) cur.where.add(where);
    this.map.set(key, cur);
  }
  get size() { return this.map.size; }
  get total() { return [...this.map.values()].reduce((a, b) => a + b.count, 0); }
  md(limit = 200) {
    if (!this.size) return "_none_\n";
    return [...this.map.values()]
      .sort((a, b) => b.count - a.count)
      .slice(0, limit)
      .map((v) => {
        const where = v.where.size ? `  \n  <sub>in: ${[...v.where].slice(0, 4).join(" · ")}${v.where.size > 4 ? ` +${v.where.size - 4} more` : ""}</sub>` : "";
        return `- **×${v.count}** \`${v.sample.replace(/`/g, "'").slice(0, 400)}\`${where}`;
      })
      .join("\n") + "\n";
  }
}

// ── E2E results ──────────────────────────────────────────────────────────────
const results = JSON.parse(read(path.join(E2E, "results.json")) ?? "null");
const tests = [];
const walk = (suite, trail) => {
  for (const s of suite.suites ?? []) walk(s, [...trail, s.title]);
  for (const spec of suite.specs ?? []) {
    for (const t of spec.tests ?? []) {
      const r = t.results?.at(-1) ?? {};
      tests.push({
        title: [...trail, spec.title].filter(Boolean).join(" › "),
        file: spec.file,
        status: r.status ?? t.status ?? "unknown",
        ms: r.duration ?? 0,
        error: (r.errors ?? []).map((e) => (e.message ?? "").replace(/\x1b\[[0-9;]*m/g, "")).join("\n"),
        annotations: [...(t.annotations ?? []), ...(r.annotations ?? [])],
      });
    }
  }
};
if (results) for (const s of results.suites ?? []) walk(s, []);
const by = (st) => tests.filter((t) => t.status === st);

// ── Browser findings (written by the monitor fixture) ────────────────────────
const browserErrors = new Bucket();
const browserWarnings = new Bucket();
for (const line of (read(path.join(E2E, ".state", "findings.jsonl")) ?? "").split("\n")) {
  if (!line.trim()) continue;
  const f = JSON.parse(line);
  for (const e of f.errors) browserErrors.add(e, f.test);
  for (const w of f.warnings) browserWarnings.add(w, f.test);
}

// ── Service logs ─────────────────────────────────────────────────────────────
function scanPython(text) {
  const errors = new Bucket(), warnings = new Bucket(), tracebacks = new Bucket();
  const lines = text.split("\n").map(strip);
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/Traceback \(most recent call last\)/.test(l)) {
      const block = [l];
      while (++i < lines.length && (/^(\s|Traceback|During handling|The above exception)/.test(lines[i]) || lines[i] === "")) block.push(lines[i]);
      if (i < lines.length) block.push(lines[i]); // the "XError: message" line
      tracebacks.add(block.join("\n"));
      continue;
    }
    if (/\b(ERROR|CRITICAL)\b/.test(l)) errors.add(l);
    else if (/\bWARNING\b|\bWARN\b|DeprecationWarning|UserWarning/.test(l)) warnings.add(l);
  }
  return { errors, warnings, tracebacks };
}

function scanGeneric(text, errRe, warnRe) {
  const errors = new Bucket(), warnings = new Bucket();
  for (const l of text.split("\n")) {
    if (errRe.test(l)) errors.add(l);
    else if (warnRe.test(l)) warnings.add(l);
  }
  return { errors, warnings };
}

const backendLog = read(path.join(LOGS, "backend.log"));
const frontendLog = read(path.join(LOGS, "frontend.log"));
const mockLog = read(path.join(LOGS, "mock-llm.log"));
const neoLog = read(path.join(LOGS, "neo4j.log"));
const backend = backendLog !== null ? scanPython(backendLog) : null;
const frontend = frontendLog !== null ? scanGeneric(frontendLog, /⨯|\bError\b|ERR_|Unhandled|unreachable/i, /\bwarn(ing)?\b|⚠/i) : null;
const neo = neoLog !== null ? scanGeneric(neoLog, /\bERROR\b/, /\bWARN\b/) : null;
const mock = { errors: new Bucket(), warnings: new Bucket() };
const mockKinds = {};
for (const l of (mockLog ?? "").split("\n")) {
  const m = l.match(/\{.*\}$/);
  if (!m) continue;
  try {
    const j = JSON.parse(m[0]);
    if (j.kind) mockKinds[`${j.provider ?? "-"}:${j.kind.split(":")[0]}`] = (mockKinds[`${j.provider ?? "-"}:${j.kind.split(":")[0]}`] ?? 0) + 1;
    if (j.kind === "mock_error") mock.errors.add(`${j.endpoint}: ${j.error}`);
    if (j.kind === "forced_500") mock.warnings.add(`forced 500 on ${j.endpoint} (${j.provider}) — injected by a failure test`);
  } catch {}
}

// ── Static checks (./sandbox.sh checks) ─────────────────────────────────────
const nextBuild = read(path.join(LOGS, "static-next-build.log"));
const eslint = read(path.join(LOGS, "static-eslint.log"));
const pytest = read(path.join(LOGS, "static-pytest.log"));
const staticWarnings = new Bucket();
const staticErrors = new Bucket();
for (const l of (nextBuild ?? "").split("\n")) {
  if (/⚠|warn/i.test(l)) staticWarnings.add(`next build: ${l.trim()}`);
  if (/Failed to compile|Type error|⨯/.test(l)) staticErrors.add(`next build: ${l.trim()}`);
}
let eslintFile = "";
for (const l of (eslint ?? "").split("\n")) {
  if (/^\//.test(l)) eslintFile = l.replace(/^.*\/src\//, "src/");
  const m = l.match(/^\s+(\d+:\d+)\s+(error|warning)\s+(.*)$/);
  if (m) (m[2] === "error" ? staticErrors : staticWarnings).add(`eslint ${eslintFile}:${m[1]} ${m[3].replace(/\s{2,}/g, "  ")}`);
}
const pytestSummary = (pytest ?? "").trim().split("\n").pop() ?? "";
if (/failed|error/i.test(pytestSummary)) staticErrors.add(`pytest: ${pytestSummary}`);
for (const m of (pytest ?? "").matchAll(/^\s+\S+: (\w*Warning): (.*)$/gm)) staticWarnings.add(`pytest ${m[1]}: ${m[2]}`);

// ── Write the report ─────────────────────────────────────────────────────────
const row = (name, e, w) => `| ${name} | ${e ?? "–"} | ${w ?? "–"} |`;
const seen = new Set();
const annotations = tests
  .flatMap((t) => t.annotations.map((a) => ({ ...a, test: t.title })))
  .filter((a) => {
    const k = `${a.type}|${a.description}|${a.test}`;
    return seen.has(k) ? false : seen.add(k);
  });
const out = [];
out.push(`# NJDOT sandbox run report`, "", `Generated ${new Date().toISOString()}`, "");
out.push(`## Summary`, "");
out.push(
  results
    ? `**Tests:** ${by("passed").length} passed · ${by("failed").length + by("timedOut").length} failed · ${by("skipped").length} skipped · ${tests.length} total`
    : "**Tests:** no results.json — the suite did not run",
  "",
);
out.push("| Source | Errors (distinct) | Warnings (distinct) |", "|---|---|---|");
out.push(row("Browser (console + network)", browserErrors.size, browserWarnings.size));
out.push(row("Backend — Azure container", backend ? `${backend.errors.size} + ${backend.tracebacks.size} tracebacks` : "no log", backend?.warnings.size));
out.push(row("Frontend — Vercel container", frontend?.errors.size ?? "no log", frontend?.warnings.size));
out.push(row("Mock LLM", mockLog ? mock.errors.size : "no log", mock.warnings.size));
out.push(row("Neo4j", neo?.errors.size ?? "no log", neo?.warnings.size));
out.push(row("Static checks (build / lint / pytest)", nextBuild || eslint || pytest ? staticErrors.size : "not run", staticWarnings.size), "");

out.push(`## Failed tests`, "");
const failed = [...by("failed"), ...by("timedOut")];
out.push(failed.length ? failed.map((t) => `### ✗ ${t.title}\n<sub>${t.file} · ${(t.ms / 1000).toFixed(1)}s</sub>\n\n\`\`\`\n${t.error.slice(0, 2500)}\n\`\`\`\n`).join("\n") : "_none_\n");

out.push(`## Security findings & observations`, "");
out.push(annotations.length ? annotations.map((a) => `- **${a.type}** — ${a.description} <sub>(${a.test})</sub>`).join("\n") + "\n" : "_none_\n");

out.push(`## Skipped tests`, "");
out.push(by("skipped").length ? by("skipped").map((t) => `- ${t.title}`).join("\n") + "\n" : "_none_\n");

out.push(`## Browser errors`, "", browserErrors.md());
out.push(`## Browser warnings`, "", browserWarnings.md());
if (backend) {
  out.push(`## Backend tracebacks`, "");
  out.push(backend.tracebacks.size ? [...backend.tracebacks.map.values()].map((v) => `**×${v.count}**\n\`\`\`\n${v.sample.slice(0, 4000)}\n\`\`\``).join("\n\n") + "\n" : "_none_\n");
  out.push(`## Backend errors`, "", backend.errors.md());
  out.push(`## Backend warnings`, "", backend.warnings.md());
}
if (frontend) out.push(`## Frontend server errors`, "", frontend.errors.md(), `## Frontend server warnings`, "", frontend.warnings.md());
out.push(`## Mock LLM`, "", `Calls by kind: \`${JSON.stringify(mockKinds)}\``, "", `Errors:`, mock.errors.md(), `Warnings:`, mock.warnings.md());
if (neo) out.push(`## Neo4j errors`, "", neo.errors.md(), `## Neo4j warnings`, "", neo.warnings.md());
out.push(`## Static checks`, "", pytest ? `Backend unit tests: \`${pytestSummary}\`` : "_./sandbox.sh checks not run_", "", "Errors:", staticErrors.md(), "Warnings:", staticWarnings.md());
out.push(`## All tests`, "", "| Status | Test | Time |", "|---|---|---|");
for (const t of tests) out.push(`| ${t.status} | ${t.title} | ${(t.ms / 1000).toFixed(1)}s |`);

fs.mkdirSync(LOGS, { recursive: true });
fs.writeFileSync(path.join(LOGS, "REPORT.md"), out.join("\n") + "\n");
console.log(`report: ${path.join(LOGS, "REPORT.md")}`);
