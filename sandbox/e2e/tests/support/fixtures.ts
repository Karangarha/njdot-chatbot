import { test as base, expect, type Page, type Request } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { env, findingsFile } from "./env";
import { loadAccounts, type Account, type Accounts } from "./accounts";

/** One entry per browser-side event: console lines, uncaught errors, and
 *  every request with its outcome. Attached to each test's report. */
export type LogEntry = {
  t: number;
  kind: "console" | "pageerror" | "request" | "requestfailed";
  level?: string;
  text?: string;
  method?: string;
  url?: string;
  status?: number;
  ms?: number;
  resourceType?: string;
  postData?: string | null;
  failure?: string;
};

export class Monitor {
  readonly entries: LogEntry[] = [];
  private allowed: RegExp[] = [];
  private started = Date.now();

  /** Mark an expected error (matched against console text, error message or
   *  "<METHOD> <url> -> <status>" for requests) so it doesn't fail the test. */
  allow(...patterns: RegExp[]): void {
    this.allowed.push(...patterns);
  }

  /** Requests (finished or failed) whose URL matches, oldest first. */
  calls(match: RegExp | string, method?: string): LogEntry[] {
    const re = typeof match === "string" ? new RegExp(match.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) : match;
    return this.entries.filter(
      (e) => (e.kind === "request" || e.kind === "requestfailed") && re.test(e.url ?? "") && (!method || e.method === method),
    );
  }

  /** Like calls(), but waits (up to 15s) for a matching request to have
   *  finished — responses are logged on "requestfinished", which can land a
   *  moment after the UI has already reacted to them. */
  async call(match: RegExp | string, method?: string, which: "first" | "last" = "first"): Promise<LogEntry> {
    await expect
      .poll(() => this.calls(match, method).length, { message: `waiting for ${method ?? "any"} ${match}`, timeout: 15_000 })
      .toBeGreaterThan(0);
    const all = this.calls(match, method);
    return which === "last" ? all[all.length - 1] : all[0];
  }

  record(e: Omit<LogEntry, "t">): void {
    this.entries.push({ t: Date.now() - this.started, ...e });
  }

  private isAllowed(s: string): boolean {
    return this.allowed.some((re) => re.test(s));
  }

  /** Everything that should fail the test. */
  problems(): string[] {
    const out: string[] = [];
    const ours = (url = "") => url.startsWith(env.apiUrl) || url.startsWith(env.supabaseUrl) || url.startsWith(env.frontendUrl);
    for (const e of this.entries) {
      if (e.kind === "pageerror" && !this.isAllowed(e.text ?? "")) out.push(`uncaught page error: ${e.text}`);
      if (e.kind === "console" && e.level === "error") {
        // Chrome echoes every 4xx/5xx as "Failed to load resource" — those
        // are judged by the request rules below instead.
        if (/Failed to load resource/.test(e.text ?? "")) continue;
        if (!this.isAllowed(e.text ?? "")) out.push(`console.error: ${e.text}`);
      }
      if (e.kind === "request" && e.status !== undefined) {
        const line = `${e.method} ${e.url} -> ${e.status}`;
        if (this.isAllowed(line)) continue;
        if (e.status >= 500) out.push(`server error: ${line}`);
        else if (e.status >= 400 && ours(e.url)) out.push(`unexpected ${e.status}: ${line}`);
      }
      if (e.kind === "requestfailed" && ours(e.url)) {
        // ERR_ABORTED = navigation cancelled it or an EventSource was closed
        // on purpose after "ready" — normal browser behaviour, logged only.
        if (/ERR_ABORTED|NS_BINDING_ABORTED/.test(e.failure ?? "")) continue;
        const line = `${e.method} ${e.url} failed: ${e.failure}`;
        if (!this.isAllowed(line)) out.push(`request failed: ${line}`);
      }
    }
    return out;
  }

  /** Non-fatal but worth reviewing: console warnings, expected 4xx, aborted
   *  requests, and anything a test explicitly allowed. */
  warnings(): string[] {
    const out: string[] = [];
    for (const e of this.entries) {
      if (e.kind === "console" && (e.level === "warning" || e.level === "warn")) out.push(`console.warn: ${e.text}`);
      if (e.kind === "console" && e.level === "error" && this.isAllowed(e.text ?? "")) out.push(`console.error (expected): ${e.text}`);
      if (e.kind === "pageerror" && this.isAllowed(e.text ?? "")) out.push(`page error (expected): ${e.text}`);
      if (e.kind === "request" && e.status !== undefined && e.status >= 400) {
        const line = `${e.method} ${e.url} -> ${e.status}`;
        if (this.isAllowed(line) || e.status < 500) out.push(`http ${e.status} (expected or third-party): ${line}`);
      }
      if (e.kind === "requestfailed") out.push(`request aborted/failed: ${e.method} ${e.url} (${e.failure})`);
    }
    return out;
  }

  format(): string {
    return this.entries
      .map((e) => {
        const t = `+${(e.t / 1000).toFixed(2)}s`.padStart(9);
        switch (e.kind) {
          case "console":
            return `${t} [console.${e.level}] ${e.text}`;
          case "pageerror":
            return `${t} [PAGE ERROR] ${e.text}`;
          case "requestfailed":
            return `${t} [FAILED] ${e.method} ${e.url} (${e.failure})`;
          default:
            return `${t} ${String(e.status).padEnd(3)} ${e.method?.padEnd(6)} ${e.url} ${e.ms !== undefined ? `(${e.ms} ms)` : ""}`;
        }
      })
      .join("\n");
  }
}

function attach(page: Page, m: Monitor): void {
  const startTimes = new Map<Request, number>();
  // Status as soon as headers arrive: a request can still end up "failed"
  // (e.g. ERR_ABORTED when the page navigates away mid-body) after the
  // server has answered, and that answer is what the tests check.
  const statuses = new Map<Request, number>();
  page.on("response", (res) => statuses.set(res.request(), res.status()));
  page.on("console", (msg) => m.record({ kind: "console", level: msg.type(), text: msg.text() }));
  page.on("pageerror", (err) => m.record({ kind: "pageerror", text: `${err.name}: ${err.message}` }));
  page.on("request", (req) => startTimes.set(req, Date.now()));
  page.on("requestfinished", async (req) => {
    const res = await req.response().catch(() => null);
    const t0 = startTimes.get(req);
    m.record({
      kind: "request",
      method: req.method(),
      url: req.url(),
      status: res?.status(),
      ms: t0 ? Date.now() - t0 : undefined,
      resourceType: req.resourceType(),
      postData: req.method() === "GET" ? undefined : safePostData(req),
    });
  });
  page.on("requestfailed", (req) =>
    m.record({
      kind: "requestfailed",
      method: req.method(),
      url: req.url(),
      status: statuses.get(req),
      failure: req.failure()?.errorText,
      resourceType: req.resourceType(),
    }),
  );
}

function safePostData(req: Request): string | null {
  try {
    const d = req.postData();
    if (!d) return null;
    // Keep request bodies readable in the report, but never store passwords.
    return d.replace(/("(?:password|new_password)"\s*:\s*")[^"]*"/g, '$1***"').slice(0, 2000);
  } catch {
    return null; // multipart with binary file parts
  }
}

type Fixtures = { monitor: Monitor; accounts: Accounts; main: Account };

export const test = base.extend<Fixtures>({
  monitor: [
    async ({ context }, use, testInfo) => {
      const m = new Monitor();
      context.on("page", (p) => attach(p, m));
      for (const p of context.pages()) attach(p, m);
      await use(m);
      await testInfo.attach("browser-log.txt", { body: m.format(), contentType: "text/plain" });
      await testInfo.attach("browser-log.json", { body: JSON.stringify(m.entries, null, 2), contentType: "application/json" });
      const problems = m.problems();
      const warnings = m.warnings();
      // Every error and warning from every test lands in one file, which
      // sandbox/report/build-report.mjs turns into logs/REPORT.md.
      fs.mkdirSync(path.dirname(findingsFile), { recursive: true });
      fs.appendFileSync(
        findingsFile,
        JSON.stringify({ test: testInfo.titlePath.slice(1).join(" › "), file: path.basename(testInfo.file), status: testInfo.status, errors: problems, warnings }) + "\n",
      );
      if (testInfo.status === testInfo.expectedStatus) {
        expect(problems, `browser console / network problems:\n${problems.join("\n")}`).toEqual([]);
      }
    },
    { auto: true },
  ],
  accounts: async ({}, use) => use(loadAccounts()),
  main: async ({ accounts }, use) => use(accounts.main),
});

export { expect };

/** Log in through the real LoginForm and land on /chat. */
export async function login(page: Page, a: Account): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email address").fill(a.email);
  await page.getByLabel("Password", { exact: true }).fill(a.password);
  await page.getByRole("button", { name: "Sign In" }).click();
  await page.waitForURL("**/chat");
  await expect(page.getByRole("button", { name: "User menu" })).toBeVisible();
}
