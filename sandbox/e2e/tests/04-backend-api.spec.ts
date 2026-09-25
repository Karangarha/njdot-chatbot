import { randomUUID } from "node:crypto";
import { test, expect } from "./support/fixtures";
import { accessToken } from "./support/accounts";
import { env } from "./support/env";

// Direct HTTP checks against the Azure-shaped backend — the contract the
// frontend depends on, CORS as Vercel's origin sees it, auth on every
// protected route, and error-shape hygiene (JSON detail, never a traceback).

const api = (p: string) => `${env.apiUrl}${p}`;

test.describe("backend: platform + contract", () => {
  test("health and OpenAPI list every route the frontend calls", async ({ request }) => {
    expect((await request.get(api("/health"))).status()).toBe(200);
    const spec = await (await request.get(api("/openapi.json"))).json();
    const paths = Object.keys(spec.paths);
    for (const p of [
      "/api/query", "/api/pdf/{doc_name}", "/api/auth/request-reset", "/api/auth/reset-password",
      "/api/auth/change-password", "/api/conversations", "/api/review", "/api/review/{project_id}/status",
      "/api/review/{project_id}/rerun", "/api/review/{project_id}/pdf/{doc_type}", "/api/session/upload",
      "/api/session/status/{session_id}", "/api/session/query", "/api/session/messages/{session_id}",
    ]) expect(paths, p).toContain(p);
    expect((await request.get(api("/docs"))).status()).toBe(200);
  });

  test("CORS: frontend origin allowed, random origin not", async ({ request }) => {
    const pre = await request.fetch(api("/api/query"), {
      method: "OPTIONS",
      headers: { Origin: env.frontendUrl, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,authorization" },
    });
    expect(pre.status()).toBe(200);
    expect(pre.headers()["access-control-allow-origin"]).toBe(env.frontendUrl);
    expect(pre.headers()["access-control-allow-credentials"]).toBe("true");

    const vercelPreview = await request.fetch(api("/health"), { headers: { Origin: "https://njdot-git-feature-x.vercel.app" } });
    expect(vercelPreview.headers()["access-control-allow-origin"]).toBe("https://njdot-git-feature-x.vercel.app");

    const evil = await request.fetch(api("/health"), { headers: { Origin: "https://evil.example.com" } });
    expect(evil.headers()["access-control-allow-origin"]).toBeUndefined();
  });

  test("/api/query validation: blank → 400, missing → 422, JSON error bodies", async ({ request }) => {
    const blank = await request.post(api("/api/query"), { data: { query: "   " } });
    expect(blank.status()).toBe(400);
    expect((await blank.json()).detail).toMatch(/must not be empty/);
    const missing = await request.post(api("/api/query"), { data: {} });
    expect(missing.status()).toBe(422);
  });

  test("/api/pdf serves reference PDFs and 404s unknown docs", async ({ request }) => {
    const ok = await request.get(api("/api/pdf/SchedulingManual"));
    expect(ok.status(), "run ./sandbox/sandbox.sh seed to upload reference PDFs to the public 'pdfs' bucket").toBe(200);
    expect(ok.headers()["content-type"]).toContain("application/pdf");
    expect(ok.headers()["content-disposition"] ?? "").toContain("inline");
    const missing = await request.get(api(`/api/pdf/does-not-exist-${Date.now()}`));
    expect(missing.status()).toBe(404);
  });
});

test.describe("backend: auth on protected routes", () => {
  test("conversations need a valid Supabase JWT", async ({ request, main }) => {
    expect((await request.get(api("/api/conversations"))).status()).toBe(401);
    expect((await request.get(api("/api/conversations"), { headers: { Authorization: "Bearer not-a-jwt" } })).status()).toBe(401);
    const token = await accessToken(main);
    const ok = await request.get(api("/api/conversations"), { headers: { Authorization: `Bearer ${token}` } });
    expect(ok.status()).toBe(200);
    expect(Array.isArray(await ok.json())).toBe(true);
  });

  test("@security an unsigned (alg=none) JWT is rejected", async ({ request }) => {
    // app/auth.py decodes WITHOUT verifying the signature when JWKS fails and
    // SUPABASE_JWT_SECRET is unset. This proves the deployed config closes that.
    const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
    const forged = `${b64({ alg: "none", typ: "JWT" })}.${b64({ sub: randomUUID(), aud: "authenticated", exp: 9999999999 })}.`;
    const res = await request.get(api("/api/conversations"), { headers: { Authorization: `Bearer ${forged}` } });
    expect(res.status(), "forged token accepted — set SUPABASE_JWT_SECRET / check JWKS reachability").toBe(401);
  });

  test("change-password needs a token", async ({ request }) => {
    const res = await request.post(api("/api/auth/change-password"), { data: { new_password: "whatever123" } });
    expect(res.status()).toBe(401);
  });

  test("review PDF + rerun reject missing tokens", async ({ request }) => {
    const id = randomUUID();
    expect((await request.get(api(`/api/review/${id}/pdf/narrative`))).status()).toBe(401);
    expect((await request.post(api(`/api/review/${id}/rerun`), { multipart: { checks: "[]" } })).status()).toBe(401);
  });

  test("reset-password rejects a forged token", async ({ request }) => {
    const res = await request.post(api("/api/auth/reset-password"), { data: { reset_token: "forged", new_password: "Password123!" } });
    expect(res.status()).toBeGreaterThanOrEqual(400);
    expect(res.status()).toBeLessThan(500);
  });

  test("@security session endpoints should require auth", async ({ request }) => {
    test.info().annotations.push({
      type: "security",
      description:
        "backend/app/api/session.py: /upload, /status, /query and /messages take no Authorization at all — " +
        "anyone holding a project UUID can read its Q&A history and query its private documents.",
    });
    const id = randomUUID();
    const messages = await request.get(api(`/api/session/messages/${id}`));
    const query = await request.post(api("/api/session/query"), { data: { question: "hi", session_id: id } });
    expect.soft(messages.status(), "GET /api/session/messages without a token").toBe(401);
    expect.soft(query.status(), "POST /api/session/query without a token").toBe(401);
  });
});

test.describe("backend: LLM failure handling", () => {
  test.skip(!env.usingMockLlm, "needs the mock LLM to inject failures");

  test("OpenAI outage on /api/query gives a clean JSON error, not a hang or traceback", async ({ request }) => {
    await request.post(`${env.mockLlmUrl}/__fail`, { data: { openai: true } });
    try {
      const res = await request.post(api("/api/query"), { data: { query: "What is a working day?" }, timeout: 120_000 });
      const text = await res.text();
      expect(text).not.toMatch(/Traceback \(most recent call last\)/);
      if (res.status() !== 200) {
        expect(res.status()).toBeGreaterThanOrEqual(500);
        expect(() => JSON.parse(text)).not.toThrow();
      }
      test.info().annotations.push({ type: "observed", description: `/api/query with OpenAI down → ${res.status()} ${text.slice(0, 200)}` });
    } finally {
      await request.post(`${env.mockLlmUrl}/__fail`, { data: { openai: false } });
    }
  });

  test("the LLM was actually exercised (mock counters)", async ({ request }) => {
    const stats = await (await request.get(`${env.mockLlmUrl}/__stats`)).json();
    test.info().annotations.push({ type: "observed", description: JSON.stringify(stats.counts) });
    expect(stats.counts["openai:embeddings"] ?? 0).toBeGreaterThan(0);
    expect(stats.counts["mock_error"] ?? 0, "mock LLM hit an internal error — see logs/mock-llm.log").toBe(0);
  });
});
