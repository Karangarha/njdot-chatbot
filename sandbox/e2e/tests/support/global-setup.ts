import fs from "node:fs";
import { env, findingsFile } from "./env";
import { newAccount, saveAccounts, signUp } from "./accounts";

async function reachable(name: string, url: string, init?: RequestInit): Promise<void> {
  try {
    const res = await fetch(url, { ...init, signal: AbortSignal.timeout(10_000) });
    if (res.status >= 500) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    throw new Error(
      `${name} not reachable at ${url} (${(err as Error).message}). ` +
        `Start it first: ./sandbox/sandbox.sh up`,
    );
  }
}

export default async function globalSetup(): Promise<void> {
  fs.rmSync(findingsFile, { force: true });
  if (!env.anonKey) throw new Error("SUPABASE_ANON_KEY is empty — run ./sandbox/sandbox.sh init");

  await reachable("Supabase auth", `${env.supabaseUrl}/auth/v1/health`, { headers: { apikey: env.anonKey } });
  await reachable("Backend", `${env.apiUrl}/health`);
  await reachable("Frontend", `${env.frontendUrl}/`);
  if (env.usingMockLlm) await reachable("Mock LLM", `${env.mockLlmUrl}/health`);

  // Fresh accounts every run so results never depend on leftovers from an
  // earlier run (and nothing existing in your local Supabase is modified).
  const main = newAccount("main");
  const pw = newAccount("pw");
  await signUp(main);
  await signUp(pw);
  saveAccounts({ main, pw });

  if (env.usingMockLlm) await fetch(`${env.mockLlmUrl}/__reset`, { method: "POST" });
}
