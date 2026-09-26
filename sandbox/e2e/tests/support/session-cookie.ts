import type { BrowserContext, Request } from "@playwright/test";

// @supabase/ssr keeps the browser session in sb-<ref>-auth-token as
// "base64-" + base64url(JSON), split into .0/.1… chunks when it's long.
const AUTH_COOKIE = /^sb-.+-auth-token(\.\d+)?$/;

async function readCookie(ctx: BrowserContext) {
  const all = (await ctx.cookies()).filter((c) => AUTH_COOKIE.test(c.name));
  if (!all.length) throw new Error("no Supabase auth cookie in the browser context");
  const chunks = all
    .filter((c) => /\.\d+$/.test(c.name))
    .sort((a, b) => Number(a.name.split(".").pop()) - Number(b.name.split(".").pop()));
  const parts = chunks.length ? chunks : all;
  return { parts, value: parts.map((c) => c.value).join("") };
}

function decode(value: string) {
  const b64 = decodeURIComponent(value).replace(/^base64-/, "");
  return JSON.parse(Buffer.from(b64, "base64url").toString("utf8"));
}

/** The access token the app's Supabase client currently holds. */
export async function accessToken(ctx: BrowserContext): Promise<string> {
  return decode((await readCookie(ctx)).value).access_token;
}

/** Mark the stored session expired, so the app's client refreshes it on its
 * next getSession() or auto-refresh tick — what an hour-old page looks like. */
export async function expireSession(ctx: BrowserContext): Promise<void> {
  const { parts, value } = await readCookie(ctx);
  const session = decode(value);
  session.expires_at = Math.floor(Date.now() / 1000) - 60;
  const next = "base64-" + Buffer.from(JSON.stringify(session), "utf8").toString("base64url");
  const size = Math.ceil(next.length / parts.length);
  await ctx.addCookies(parts.map((c, i) => ({ ...c, value: next.slice(i * size, (i + 1) * size) })));
}

export const bearer = (req: Request) => (req.headers()["authorization"] ?? "").replace(/^Bearer /, "");
