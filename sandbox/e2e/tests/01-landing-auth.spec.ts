import { test, expect, login } from "./support/fixtures";
import { newAccount, updateAccount } from "./support/accounts";
import { env } from "./support/env";

test.describe("landing page", () => {
  test("renders and every link goes to the right place", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Construction Management Smart Assistant");
    for (const alt of ["New Jersey highway aerial view", "NJDOT workers installing road sign", "Rutgers University", "Kean University", "Johns Hopkins University"]) {
      await expect(page.getByAltText(alt)).toBeVisible();
    }
    await expect(page.getByRole("link", { name: "Contact Us" })).toHaveAttribute("href", /^mailto:/);

    await page.getByRole("link", { name: "Sign In" }).click();
    await expect(page).toHaveURL(/\/login$/);
    await page.getByRole("link", { name: "← Back to home" }).click();
    await expect(page).toHaveURL(`${env.frontendUrl}/`);

    await page.getByRole("link", { name: "Get Started" }).click();
    await expect(page).toHaveURL(/\/signup$/);
    await page.goto("/");
    await page.getByRole("link", { name: "Try Assistant" }).click();
    await expect(page).toHaveURL(/\/login$/);
  });

  test("static assets load (no broken images)", async ({ page, monitor }) => {
    await page.goto("/");
    await page.waitForLoadState("networkidle");
    const images = monitor.calls(/\.(png|jpe?g|webp)|_next\/image/);
    expect(images.length).toBeGreaterThan(0);
    for (const img of images) expect(img.status, img.url).toBeLessThan(400);
  });
});

test.describe("route protection (middleware.ts)", () => {
  test("signed-out /chat redirects to /login", async ({ page }) => {
    await page.goto("/chat");
    await expect(page).toHaveURL(/\/login/);
  });

  test("signed-out /update-password bounces to /forgot-password", async ({ page }) => {
    await page.goto("/update-password");
    await expect(page).toHaveURL(/\/forgot-password$/);
  });

  test("signed-in /login, /signup, /forgot-password redirect to /chat", async ({ page, main }) => {
    await login(page, main);
    for (const p of ["/login", "/signup", "/forgot-password"]) {
      await page.goto(p);
      await expect(page, p).toHaveURL(/\/chat$/);
    }
  });
});

test.describe("signup", () => {
  test("mismatched passwords are rejected client-side", async ({ page, monitor }) => {
    await page.goto("/signup");
    await page.locator("#first-name").fill("A");
    await page.locator("#last-name").fill("B");
    await page.locator("#email").fill("mismatch@njdot-sandbox.test");
    await page.locator("#password").fill("Password123!");
    await page.locator("#confirm-password").fill("Password124!");
    await page.getByRole("button", { name: "Create Account" }).click();
    await expect(page.getByText("Passwords do not match.")).toBeVisible();
    expect(monitor.calls("/auth/v1/signup")).toHaveLength(0);
  });

  test("new account signs up, lands on /chat and shows initials", async ({ page, monitor }) => {
    const a = newAccount("signup");
    a.firstName = "Jane";
    a.lastName = "Smith";
    await page.goto("/signup");
    await page.locator("#first-name").fill(a.firstName);
    await page.locator("#last-name").fill(a.lastName);
    await page.locator("#email").fill(a.email);
    await page.locator("#password").fill(a.password);
    await page.locator("#confirm-password").fill(a.password);
    await page.getByRole("button", { name: "Create Account" }).click();
    await page.waitForURL("**/chat");
    await expect(page.getByRole("button", { name: "User menu" })).toHaveText("JS");
    expect((await monitor.call("/auth/v1/signup", "POST")).status).toBe(200);
    expect((await monitor.call("/auth/v1/token", "POST")).status).toBe(200);
  });

  test("signing up an existing email shows an error", async ({ page, main, monitor }) => {
    monitor.allow(/auth\/v1\/signup -> 4\d\d/, /auth\/v1\/token.* -> 400/);
    await page.goto("/signup");
    await page.locator("#first-name").fill("Dup");
    await page.locator("#last-name").fill("User");
    await page.locator("#email").fill(main.email);
    await page.locator("#password").fill("SomethingElse123!");
    await page.locator("#confirm-password").fill("SomethingElse123!");
    await page.getByRole("button", { name: "Create Account" }).click();
    // Supabase either rejects the signup ("User already registered") or,
    // with email enumeration protection, accepts it and the auto-login with
    // the wrong password fails → SignupForm sends the user to /login.
    await expect(page.getByText(/already registered|already exists/i).or(page.locator("#password"))).toBeVisible();
    await expect(page).not.toHaveURL(/\/chat$/);
  });
});

test.describe("login / logout", () => {
  test("wrong password shows the Supabase error", async ({ page, main, monitor }) => {
    monitor.allow(/auth\/v1\/token\?grant_type=password -> 400/);
    await page.goto("/login");
    await page.getByLabel("Email address").fill(main.email);
    await page.getByLabel("Password", { exact: true }).fill("definitely-wrong-password");
    await page.getByRole("button", { name: "Sign In" }).click();
    await expect(page.getByText(/Invalid login credentials/i)).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });

  test("login → user menu → sign out → /chat is protected again", async ({ page, main, monitor }) => {
    await login(page, main);
    await page.getByRole("button", { name: "User menu" }).click();
    await expect(page.getByText(main.email)).toBeVisible();
    // Clicking outside closes the menu
    await page.mouse.click(10, 800);
    await expect(page.getByRole("button", { name: "Sign Out" })).toBeHidden();

    await page.getByRole("button", { name: "User menu" }).click();
    await page.getByRole("button", { name: "Sign Out" }).click();
    await page.waitForURL(`${env.frontendUrl}/`);
    expect((await monitor.call("/auth/v1/logout", "POST")).status).toBeLessThan(300);
    await page.goto("/chat");
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("password flows", () => {
  test("change password from the user menu", async ({ page, accounts, monitor }) => {
    const a = accounts.pw;
    await login(page, a);
    await page.getByRole("button", { name: "User menu" }).click();
    await page.getByRole("button", { name: "Change Password" }).click();
    await page.waitForURL("**/update-password");

    const next = `${a.password}-changed`;
    await page.locator("#password").fill(next);
    await page.locator("#confirm").fill(next + "x");
    await page.getByRole("button", { name: "Update Password" }).click();
    await expect(page.getByText("Passwords do not match.")).toBeVisible();

    await page.locator("#confirm").fill(next);
    await page.getByRole("button", { name: "Update Password" }).click();
    await expect(page.getByText("Password updated!")).toBeVisible();
    const call = await monitor.call("/api/auth/change-password", "POST");
    expect(call.status).toBe(200);
    updateAccount("pw", { password: next });
    // The page promises "Redirecting you back…" to /chat. The backend changes
    // the password through the Supabase admin API, which revokes the user's
    // sessions, so middleware.ts sees no session and sends them to /login.
    monitor.allow(/auth\/v1\/user -> 403/);
    await page.waitForURL(/\/(chat|login)/, { timeout: 10_000 });
    expect(
      new URL(page.url()).pathname,
      "after a successful password change the user is logged out (admin password update revokes sessions) instead of returning to /chat",
    ).toBe("/chat");
  });

  test("forgot password: email → new password → login with it", async ({ page, accounts, monitor }) => {
    test.info().annotations.push({
      type: "security",
      description:
        "Reset needs only the email: /api/auth/request-reset returns a usable reset_token to whoever asks " +
        "(backend/app/api/auth.py). Passing this test confirms anyone who knows an email can take over the account.",
    });
    const a = accounts.pw;
    await page.goto("/login");
    await page.getByRole("link", { name: "Forgot password?" }).click();
    await page.waitForURL("**/forgot-password");

    await page.locator("#email").fill(a.email);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText("Set your new password")).toBeVisible();
    expect((await monitor.call("/api/auth/request-reset", "POST")).status).toBe(200);

    // "Start over" returns to the email step
    await page.getByRole("button", { name: "Start over" }).click();
    await expect(page.getByText("Reset your password")).toBeVisible();
    await page.locator("#email").fill(a.email);
    await page.getByRole("button", { name: "Continue" }).click();

    const next = `${a.password}-reset`;
    await page.locator("#password").fill(next);
    await page.locator("#confirm").fill(next);
    await page.getByRole("button", { name: "Update Password" }).click();
    await page.waitForURL("**/login?reset=success");
    await expect(page.getByText("Password updated successfully.", { exact: false })).toBeVisible();
    expect((await monitor.call("/api/auth/reset-password", "POST")).status).toBe(200);
    updateAccount("pw", { password: next });

    await page.getByLabel("Email address").fill(a.email);
    await page.getByLabel("Password", { exact: true }).fill(next);
    await page.getByRole("button", { name: "Sign In" }).click();
    await page.waitForURL("**/chat");
  });

  test("forgot password for an unknown email does not reveal that it is unknown", async ({ page, monitor }) => {
    await page.goto("/forgot-password");
    await page.locator("#email").fill(`nobody-${Date.now()}@njdot-sandbox.test`);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText("Set your new password")).toBeVisible();
    expect((await monitor.call("/api/auth/request-reset", "POST")).status).toBe(200);
  });
});
