import fs from "node:fs";
import type { Page } from "@playwright/test";
import { test, expect, login } from "./support/fixtures";
import { fixture } from "./support/env";

// Route 49 project files supplied for upload testing (sandbox/e2e/fixtures).
const FILES = {
  schedule: fixture("Route49-PSE-Construction_Schedule.xer"),
  narrative: fixture("Route49-PSE-Construction_Schedule_Narrative.pdf"),
  keyMap: fixture("Route49-PSE-Key_Sheet_signed.pdf"),
  estimate: fixture("Route49-PSE-DBE_Goal_Memo.pdf"),
  // Optional: drop the Route 49 Special Provision PDF here to include it.
  specialProvision: fixture("Route49-PSE-Special_Provision.pdf"),
};

const zoneInput = (page: Page, label: string) =>
  page.locator("div.flex-1.min-w-0", { has: page.locator("p", { hasText: label }) }).locator('input[type="file"]');

async function openReviewTab(page: Page) {
  await page.getByRole("button", { name: "Document Review" }).click();
  await expect(page.getByRole("button", { name: "Run Review" })).toBeVisible();
}

const checkRow = (page: Page, name: string) => page.locator("div.flex.items-start.gap-3", { hasText: name });

test.describe("Document Review — upload form", () => {
  test.beforeEach(async ({ page, main }) => {
    await login(page, main);
    await openReviewTab(page);
  });

  test("Run Review is enabled only with schedule + narrative; remove file works", async ({ page }) => {
    const run = page.getByRole("button", { name: "Run Review" });
    await expect(run).toBeDisabled();
    await zoneInput(page, "Construction Schedule (.xer)").setInputFiles(FILES.schedule);
    await expect(page.getByText("Route49-PSE-Construction_Schedule.xer")).toBeVisible();
    await expect(run).toBeDisabled();
    await zoneInput(page, "Designer Narrative PDF").setInputFiles(FILES.narrative);
    await expect(run).toBeEnabled();

    // Optional slots show their helper text
    await zoneInput(page, "Key Map Sheet PDF").setInputFiles(FILES.keyMap);
    await expect(page.getByText(/Key map is used for utility cross-checks/)).toBeVisible();
    await zoneInput(page, "DBE Goal Memo / Estimate PDF").setInputFiles(FILES.estimate);
    await expect(page.getByText(/First page only/)).toBeVisible();

    // Utility plans (multiple): add two, remove one
    const addUtility = page.locator('input[type="file"]').last();
    await addUtility.setInputFiles(FILES.keyMap);
    await addUtility.setInputFiles(FILES.estimate);
    await expect(page.getByText(/Cross-referenced by the utility-related checks/)).toBeVisible();

    const removes = page.getByRole("button", { name: "Remove file" });
    const before = await removes.count();
    await removes.last().click();
    await expect(removes).toHaveCount(before - 1);

    // Removing the narrative disables Run Review again
    await page.locator("div.flex-1.min-w-0", { has: page.locator("p", { hasText: "Designer Narrative PDF" }) })
      .getByRole("button", { name: "Remove file" }).click();
    await expect(run).toBeDisabled();
  });

  test("'What gets checked' panel lists every category", async ({ page }) => {
    for (const t of ["Administrative Dates", "Weather & Materials", "Schedule Logic", "Designer's Narrative"]) {
      await expect(page.getByText(t).first()).toBeVisible();
    }
  });
});

test.describe("Document Review — checklist manager", () => {
  test("toggle, add, edit, delete, reset — each hits compliance_checks", async ({ page, main, monitor }) => {
    await login(page, main);
    await openReviewTab(page);
    await page.getByRole("button", { name: "Manage Checklist" }).click();
    await expect(page.getByText("Compliance Checklist")).toBeVisible();
    await expect(page.getByText(/\d+ of \d+ checks selected to run/)).toBeVisible();
    const total = Number((await page.getByText(/of \d+ checks selected/).textContent())!.match(/of (\d+)/)![1]);
    test.skip(total === 0, "no built-in checks — run ./sandbox/sandbox.sh seed");

    await test.step("toggle the first check off (forks built-ins into the user's rows)", async () => {
      // Controlled checkbox: it flips only after the Supabase round-trip, so
      // click and wait for the counter rather than using uncheck().
      const box = page.getByRole("checkbox").first();
      await box.click();
      await expect(page.getByText(`${total - 1} of ${total} checks selected to run`)).toBeVisible();
      await expect(box).not.toBeChecked();
      expect((await monitor.call(/\/rest\/v1\/compliance_checks/, "POST")).status).toBe(201); // fork insert
      expect((await monitor.call(/\/rest\/v1\/compliance_checks/, "PATCH", "last")).status).toBeLessThan(300);
      await box.click();
      await expect(page.getByText(`${total} of ${total} checks selected to run`)).toBeVisible();
    });

    const name = `Sandbox custom check ${Date.now()}`;
    await test.step("add a custom check", async () => {
      await page.getByRole("button", { name: "Add check" }).click();
      await page.getByPlaceholder("e.g. Schedule Logic or a new category").fill("Sandbox");
      await page.getByPlaceholder("Short title shown in results").fill(name);
      // Save needs name + rule + at least one source file.
      await expect(page.getByRole("button", { name: "Save check" })).toBeDisabled();
      await page.getByPlaceholder(/Describe exactly what to verify/).fill("Verify the sandbox check runs.");
      await expect(page.getByRole("button", { name: "Save check" })).toBeEnabled();
      await page.getByRole("button", { name: "Save check" }).click();
      await expect(checkRow(page, name)).toBeVisible();
      await expect(checkRow(page, name).getByText("Custom", { exact: true })).toBeVisible();
    });

    await test.step("edit it", async () => {
      await checkRow(page, name).getByRole("button", { name: "Edit check" }).click();
      await page.getByPlaceholder("Short title shown in results").fill(`${name} (edited)`);
      await page.getByRole("button", { name: "Save check" }).click();
      await expect(checkRow(page, `${name} (edited)`)).toBeVisible();
    });

    await test.step("delete it (No, then Delete)", async () => {
      const row = checkRow(page, `${name} (edited)`);
      await row.getByRole("button", { name: "Delete check" }).click();
      await page.getByRole("button", { name: "No", exact: true }).click();
      await row.getByRole("button", { name: "Delete check" }).click();
      await page.getByRole("button", { name: "Delete", exact: true }).click();
      await expect(checkRow(page, `${name} (edited)`)).toHaveCount(0);
    });

    await test.step("reset to defaults", async () => {
      await page.getByRole("button", { name: "Reset to defaults" }).click();
      await expect(page.getByText("Reset all to defaults?")).toBeVisible();
      await page.getByRole("button", { name: "Yes", exact: true }).click();
      await expect(page.getByText(`${total} of ${total} checks selected to run`)).toBeVisible();
    });

    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByText("Compliance Checklist")).toBeHidden();
  });
});

test.describe("Document Review — full run with the Route 49 files", () => {
  test.setTimeout(15 * 60_000);

  test("upload → review (SSE) → results → Q&A → re-run → project list", async ({ page, main, monitor }) => {
    await login(page, main);
    await openReviewTab(page);

    await test.step("attach the Route 49 files", async () => {
      await zoneInput(page, "Construction Schedule (.xer)").setInputFiles(FILES.schedule);
      await zoneInput(page, "Designer Narrative PDF").setInputFiles(FILES.narrative);
      await zoneInput(page, "Key Map Sheet PDF").setInputFiles(FILES.keyMap);
      await zoneInput(page, "DBE Goal Memo / Estimate PDF").setInputFiles(FILES.estimate);
      if (fs.existsSync(FILES.specialProvision)) {
        await zoneInput(page, "Special Provision PDF").setInputFiles(FILES.specialProvision);
      } else {
        test.info().annotations.push({ type: "note", description: "Special Provision PDF not in fixtures — slot left empty" });
      }
    });

    let projectId = "";
    await test.step("Run Review: storage uploads, POST /api/review, SSE until ready", async () => {
      const reviewPost = page.waitForResponse((r) => /\/api\/review$/.test(r.url()) && r.request().method() === "POST", { timeout: 120_000 });
      await page.getByRole("button", { name: "Run Review" }).click();
      const res = await reviewPost;
      const body = await res.json();
      expect(res.status(), JSON.stringify(body)).toBe(200);
      expect(body.status).toBe("processing");
      projectId = body.project_id;

      // Files went to the private review-files bucket under <user>/<project>/
      const uploads = monitor.calls(/\/storage\/v1\/object\/review-files\//, "POST");
      expect(uploads.map((u) => (u.url ?? "").split("/").pop())).toEqual(
        expect.arrayContaining(["schedule.xer", "narrative.pdf", "key_map.pdf", "estimate.pdf"]),
      );
      for (const u of uploads) expect(u.status, u.url).toBe(200);

      // Results or the error the UI shows on SSE status=error — whichever
      // comes first, so a failed review fails the test right away.
      const done = page.getByText("Schedule Compliance Review");
      const failed = page.getByText(/unexpected error occurred|Lost connection while waiting|review failed/i);
      await expect(done.or(failed)).toBeVisible({ timeout: 12 * 60_000 });
      if (await failed.isVisible()) throw new Error(`review failed in the UI: "${await failed.first().textContent()}" — see logs/backend.log`);
      const sse = monitor.calls(new RegExp(`/api/review/${projectId}/status\\?token=`));
      expect(sse.length, "SSE status stream was opened").toBeGreaterThan(0);
    });

    await test.step("project saved to review_projects and session indexing started", async () => {
      expect((await monitor.call(/\/api\/session\/upload$/, "POST")).status).toBe(200);
      expect((await monitor.call(/\/rest\/v1\/review_projects/, "POST")).status).toBe(201);
      expect((await monitor.call(/\/rest\/v1\/review_projects/, "PATCH")).status).toBeLessThan(300);
    });

    await test.step("results: summary pills filter, sections collapse, cards render", async () => {
      const pills = page.locator("button[aria-pressed]");
      const n = await pills.count();
      expect(n).toBeGreaterThan(0);
      for (let i = 0; i < n; i++) {
        await pills.nth(i).click();
        await expect(pills.nth(i)).toHaveAttribute("aria-pressed", "true");
        await expect(page.getByRole("button", { name: "Clear filter" })).toBeVisible();
        await page.getByRole("button", { name: "Clear filter" }).click();
      }
      const statuses = page.getByText(/^(COMPLIANT|ISSUES FOUND|MISSING)$/);
      expect(await statuses.count()).toBeGreaterThan(0);
      const section = page.getByRole("button", { name: /\d+ checks/ }).first();
      if (await section.count()) {
        await section.click(); // collapse
        await section.click(); // expand
      }
    });

    await test.step("Download PDF produces a report file", async () => {
      const dl = page.waitForEvent("download");
      await page.getByRole("button", { name: "Download PDF" }).click();
      expect((await dl).suggestedFilename()).toBe("Schedule_Compliance_Report.pdf");
    });

    await test.step("citation pills open the right PDF (public via iframe, private via Bearer fetch)", async () => {
      const pill = page.locator("main button", { hasText: / · (p\.\d+|\S+)$/ }).first();
      if ((await pill.count()) === 0) {
        test.info().annotations.push({ type: "note", description: "no clickable (verified) citation pills in this result" });
        return;
      }
      const pdf = page.waitForResponse((r) => /\/api\/(pdf|review\/[^/]+\/pdf)\//.test(r.url()), { timeout: 30_000 });
      await pill.click();
      expect((await pdf).status()).toBe(200);
      await page.getByRole("button", { name: "Close" }).click();
    });

    await test.step("Document Q&A: SSE until ready, history, ask a question", async () => {
      await page.getByRole("button", { name: "Document Q&A" }).click();
      await expect(page.getByText(/chunks indexed/)).toBeVisible({ timeout: 8 * 60_000 });
      expect((await monitor.call(new RegExp(`/api/session/messages/${projectId}`), "GET")).status).toBe(200);

      const box = page.getByPlaceholder("Ask about the narrative, special provisions, or schedule…");
      await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
      await box.fill("Which activities are on the critical path?");
      const answered = page.waitForResponse((r) => r.url().endsWith("/api/session/query"), { timeout: 180_000 });
      await box.press("Enter");
      const res = await answered;
      expect(res.status(), await res.text()).toBe(200);
      const body = await res.json();
      expect(typeof body.answer).toBe("string");
      await expect(page.getByText("Which activities are on the critical path?")).toBeVisible();
      await page.getByRole("button", { name: "Compliance Results" }).click();
    });

    await test.step("Re-run Review with the current checklist", async () => {
      const rerun = page.waitForResponse((r) => r.url().endsWith(`/api/review/${projectId}/rerun`), { timeout: 60_000 });
      await page.getByRole("button", { name: "Re-run Review" }).click();
      expect((await rerun).status()).toBe(200);
      await expect(page.getByRole("button", { name: "Re-run Review" })).toBeEnabled({ timeout: 12 * 60_000 });
    });

    await test.step("project appears in the sidebar and reloads from the DB", async () => {
      // Two "New Review" buttons exist: the sidebar's (translated off-screen,
      // which Playwright still counts as visible) and the results header's,
      // which comes later in the DOM.
      await page.getByRole("button", { name: "New Review" }).last().click();
      await page.getByRole("button", { name: "Open sidebar" }).click();
      const item = page.locator("aside").getByRole("button", { name: /✓|Untitled Project|Route/ }).first();
      await expect(item).toBeVisible();
      await item.click();
      await expect(page.getByText("Schedule Compliance Review")).toBeVisible();
    });

    await test.step("delete the project", async () => {
      await page.getByRole("button", { name: "Open sidebar" }).click();
      const row = page.locator("aside div.group").first();
      await row.hover();
      await row.getByRole("button", { name: "Delete project" }).click();
      await row.getByRole("button", { name: "Yes" }).click();
      expect((await monitor.call(/\/rest\/v1\/review_projects\?/, "DELETE")).status).toBeLessThan(300);
      await page.reload();
      await openReviewTab(page);
      await page.getByRole("button", { name: "Open sidebar" }).click();
      await expect(page.locator("aside").getByText("No projects yet. Run a review to get started.")).toBeVisible();
    });
  });

  test("no checks selected → Run Review explains why", async ({ page, main }) => {
    await login(page, main);
    await openReviewTab(page);
    await page.getByRole("button", { name: "Manage Checklist" }).click();
    // Wait for the list to load (subtitle shows "Loading…" until then).
    await expect(page.getByText(/\d+ of \d+ checks selected to run/)).toBeVisible();
    const boxes = page.getByRole("checkbox");
    const n = await boxes.count();
    for (let i = 0; i < n; i++) {
      if (await boxes.nth(i).isChecked()) {
        await boxes.nth(i).click();
        await expect(boxes.nth(i)).not.toBeChecked();
      }
    }
    await expect(page.getByText(/^0 of \d+ checks selected to run/)).toBeVisible();
    await page.getByRole("button", { name: "Done" }).click();
    await zoneInput(page, "Construction Schedule (.xer)").setInputFiles(FILES.schedule);
    await zoneInput(page, "Designer Narrative PDF").setInputFiles(FILES.narrative);
    await page.getByRole("button", { name: "Run Review" }).click();
    await expect(page.getByText(/No checks are selected to run/)).toBeVisible();
    // restore
    await page.getByRole("button", { name: "Manage Checklist" }).click();
    await page.getByRole("button", { name: "Reset to defaults" }).click();
    await page.getByRole("button", { name: "Yes", exact: true }).click();
    await page.getByRole("button", { name: "Done" }).click();
  });
});
