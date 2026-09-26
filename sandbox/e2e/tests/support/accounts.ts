import fs from "node:fs";
import path from "node:path";
import { env, stateFile } from "./env";

export type Account = { email: string; password: string; firstName: string; lastName: string };

export type Accounts = {
  /** Main account: chat, review, checklist. Password never changes. */
  main: Account;
  /** Used by the change-password / forgot-password specs (password rotates). */
  pw: Account;
};

const stamp = () => `${Date.now()}${Math.floor(Math.random() * 1000)}`;

export function newAccount(tag: string): Account {
  return {
    email: `sandbox-${tag}-${stamp()}@njdot-sandbox.test`,
    password: `Sandbox!${stamp()}`,
    firstName: "Sandbox",
    lastName: tag.charAt(0).toUpperCase() + tag.slice(1),
  };
}

/** Create a confirmed user through GoTrue's public signup endpoint — the
 *  same call the SignupForm makes. Local Supabase auto-confirms emails
 *  ([auth.email] enable_confirmations = false); if yours doesn't, this fails
 *  loudly instead of letting every later login fail mysteriously. */
export async function signUp(a: Account): Promise<void> {
  const res = await fetch(`${env.supabaseUrl}/auth/v1/signup`, {
    method: "POST",
    headers: { apikey: env.anonKey, "Content-Type": "application/json" },
    body: JSON.stringify({
      email: a.email,
      password: a.password,
      data: { first_name: a.firstName, last_name: a.lastName },
    }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`signup ${a.email} failed: ${res.status} ${JSON.stringify(body)}`);
  if (!body.access_token) {
    throw new Error(
      `signup ${a.email} returned no session — email confirmation is ON in your local Supabase. ` +
        `Set [auth.email] enable_confirmations = false in supabase/config.toml and restart it.`,
    );
  }
}

/** Password-grant login outside the browser (for direct API checks). */
export async function accessToken(a: Account): Promise<string> {
  const res = await fetch(`${env.supabaseUrl}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { apikey: env.anonKey, "Content-Type": "application/json" },
    body: JSON.stringify({ email: a.email, password: a.password }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(`login ${a.email} failed: ${res.status} ${JSON.stringify(body)}`);
  return body.access_token as string;
}

export function saveAccounts(a: Accounts): void {
  fs.mkdirSync(path.dirname(stateFile), { recursive: true });
  fs.writeFileSync(stateFile, JSON.stringify(a, null, 2));
}

export function loadAccounts(): Accounts {
  return JSON.parse(fs.readFileSync(stateFile, "utf8"));
}

export function updateAccount(key: keyof Accounts, patch: Partial<Account>): void {
  const all = loadAccounts();
  all[key] = { ...all[key], ...patch };
  saveAccounts(all);
}
