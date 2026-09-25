import { test, expect, login } from "./support/fixtures";
import type { Page } from "@playwright/test";

const INPUT = "Ask about specifications, procedures, or scheduling…";

async function ask(page: Page, question: string) {
  await page.getByPlaceholder(INPUT).fill(question);
  const answered = page.waitForResponse((r) => r.url().endsWith("/api/query") && r.request().method() === "POST", { timeout: 60_000 });
  await page.getByPlaceholder(INPUT).press("Enter");
  const res = await answered;
  expect(res.status(), await res.text()).toBe(200);
  return res.json();
}

async function openSidebar(page: Page) {
  await page.getByRole("button", { name: "Open sidebar" }).click();
}

test.describe("Smart Assistant chat", () => {
  test.beforeEach(async ({ page, main }) => login(page, main));

  test("empty state: send disabled, citations placeholder, tabs", async ({ page }) => {
    await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
    await page.getByPlaceholder(INPUT).fill("   ");
    await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
    await expect(page.getByText("Source citations appear here after your first question.")).toBeVisible();
    await expect(page.getByRole("combobox")).toHaveValue("");
    // Tab switching
    await page.getByRole("button", { name: "Document Review" }).click();
    await expect(page.getByRole("button", { name: "Run Review" })).toBeVisible();
    await page.getByRole("button", { name: "Smart Assistant" }).click();
    await expect(page.getByPlaceholder(INPUT)).toBeVisible();
  });

  test("ask with collection filter → full DB + API round trip", async ({ page, monitor }) => {
    await page.getByRole("combobox").selectOption("scheduling");
    const q = "What is the required update frequency for the progress schedule?";
    const body = await ask(page, q);

    // Request body carried the collection
    const post = await monitor.call("/api/query", "POST");
    expect(JSON.parse(post.postData ?? "{}")).toEqual({ query: q, collection: "scheduling" });
    // Response contract (frontend/src/lib/types.ts QueryResponse)
    expect(typeof body.answer).toBe("string");
    expect(Array.isArray(body.citations)).toBe(true);
    expect(Array.isArray(body.bdc_alerts)).toBe(true);
    expect(typeof body.response_time_ms).toBe("number");

    // UI: question bubble, answer, metadata line
    await expect(page.getByText(q).first()).toBeVisible();
    await expect(page.getByText(/ms · \d+ source/)).toBeVisible();

    // Supabase writes the ChatInterface makes, in order
    const restWrites = monitor.entries
      .filter((e) => e.kind === "request" && /\/rest\/v1\/(conversations|messages)/.test(e.url ?? "") && e.method !== "GET")
      .map((e) => `${e.method} ${new URL(e.url!).pathname.split("/").pop()} ${e.status}`);
    expect(restWrites.slice(0, 4)).toEqual([
      "POST conversations 201",
      "POST messages 201",
      "POST messages 201",
      "PATCH conversations 204",
    ]);

    // New conversation appears in Recents with the question as title
    await openSidebar(page);
    await expect(page.getByText("Recents")).toBeVisible();
    await expect(page.locator("aside").getByText(q.slice(0, 40))).toBeVisible();
  });

  test("suggestion pills send their question", async ({ page, monitor }) => {
    const res = page.waitForResponse((r) => r.url().endsWith("/api/query"), { timeout: 60_000 });
    await page.getByRole("button", { name: "Contract Execution Timeline" }).click();
    expect((await res).status()).toBe(200);
    const sent = JSON.parse((await monitor.call("/api/query", "POST")).postData ?? "{}");
    expect(sent.query).toBe("How many days does a contractor have to execute the contract after award?");
    expect(sent.collection).toBeNull();
    // The other three pills are present
    for (const t of ["Working Day Definition", "RCA Concrete Composition", "Density Verification"]) {
      await expect(page.getByRole("button", { name: t })).toBeVisible();
    }
  });

  test("citation → PDF modal opens /api/pdf and closes (button, Escape, backdrop)", async ({ page, monitor }) => {
    await page.getByRole("combobox").selectOption("scheduling");
    const body = await ask(page, "What must the baseline schedule submission include?");
    test.skip(body.citations.length === 0, "no citations returned — run ./sandbox/sandbox.sh seed so the scheduling manual is ingested");

    const viewPdf = page.getByRole("button", { name: "View PDF →" }).first();
    for (const close of ["button", "escape", "backdrop"] as const) {
      const pdf = page.waitForResponse((r) => r.url().includes("/api/pdf/"), { timeout: 30_000 });
      await viewPdf.click();
      const res = await pdf;
      expect(res.status(), `GET ${res.url()}`).toBe(200);
      expect(res.headers()["content-type"]).toContain("application/pdf");
      const frame = page.locator('iframe[title^="PDF:"]');
      await expect(frame).toBeVisible();
      if (close === "button") await page.getByRole("button", { name: "Close" }).click();
      if (close === "escape") await page.keyboard.press("Escape");
      if (close === "backdrop") await page.mouse.click(5, 5);
      await expect(frame).toBeHidden();
    }
    expect(monitor.calls("/api/pdf/").length).toBeGreaterThanOrEqual(3);
  });

  test("history: reopen a conversation, New Chat clears it", async ({ page, monitor }) => {
    const q = `History check ${Date.now()}`;
    await ask(page, q);
    await page.getByRole("button", { name: "Open sidebar" }).click();
    await page.getByRole("button", { name: "New Chat" }).click();
    // The sidebar stays in the DOM (translated off-screen), so only count
    // occurrences outside it: the chat thread itself must be empty.
    await expect(page.getByText(q)).toHaveCount(await page.locator("aside").getByText(q).count());

    await openSidebar(page);
    await page.locator("aside").getByRole("button", { name: new RegExp(q) }).click();
    await expect(page.getByText(q).first()).toBeVisible();
    expect((await monitor.call(/\/rest\/v1\/messages\?.*conversation_id=eq\./, "GET", "last")).status).toBe(200);
  });

  test("delete a conversation removes it (and it stays gone after reload)", async ({ page, monitor }) => {
    const q = `Delete me ${Date.now()}`;
    await ask(page, q);
    await openSidebar(page);
    const row = page.locator("aside div.group", { hasText: q.slice(0, 40) });
    const sidebar = page.locator("aside");
    await row.hover();
    await row.getByRole("button", { name: "Delete conversation" }).click();
    // The confirm prompt replaces the row's title, so look for it in the sidebar.
    await expect(sidebar.getByText("Delete?")).toBeVisible();
    await sidebar.getByRole("button", { name: "No", exact: true }).click();
    await row.hover();
    await row.getByRole("button", { name: "Delete conversation" }).click();
    await sidebar.getByRole("button", { name: "Yes", exact: true }).click();
    await expect(page.locator("aside").getByText(q.slice(0, 40))).toHaveCount(0);
    expect((await monitor.call(/\/rest\/v1\/conversations\?/, "DELETE")).status).toBeLessThan(300);

    // The real test: is the row actually gone in the database? A missing RLS
    // DELETE policy makes Supabase answer 204 while deleting nothing.
    await page.reload();
    await openSidebar(page);
    await expect(
      page.locator("aside").getByText(q.slice(0, 40)),
      "conversation came back after reload — the DELETE matched 0 rows (no RLS delete policy on conversations?)",
    ).toHaveCount(0);
  });

  test("backend error surfaces as a red error bubble", async ({ page, monitor }) => {
    monitor.allow(/\/api\/query -> 5\d\d/);
    await page.route("**/api/query", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Sandbox forced failure" }) }),
    );
    await page.getByPlaceholder(INPUT).fill("force an error");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText("Sandbox forced failure")).toBeVisible();
  });
});
