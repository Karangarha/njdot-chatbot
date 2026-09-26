import fs from "node:fs";
import path from "node:path";

// Reads sandbox/.env (the same file docker compose uses) so the suite and the
// containers can never disagree about URLs or keys. Real env vars win.
function loadSandboxEnv(): Record<string, string> {
  const file = path.resolve(__dirname, "../../../.env");
  if (!fs.existsSync(file)) return {};
  const out: Record<string, string> = {};
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
  return out;
}

const file = loadSandboxEnv();
const get = (key: string, fallback = "") => process.env[key] ?? file[key] ?? fallback;

export const env = {
  frontendUrl: get("FRONTEND_URL", "http://localhost:3000").replace(/\/+$/, ""),
  apiUrl: get("API_URL", "http://localhost:8000").replace(/\/+$/, ""),
  supabaseUrl: get("SUPABASE_URL", "http://127.0.0.1:54321").replace(/\/+$/, ""),
  anonKey: get("SUPABASE_ANON_KEY"),
  mockLlmUrl: get("MOCK_LLM_URL", "http://localhost:4010").replace(/\/+$/, ""),
  usingMockLlm: !/api\.openai\.com/.test(get("OPENAI_BASE_URL", "mock")),
  compose: get("SANDBOX_COMPOSE", ""),
};

export const fixturesDir = path.resolve(__dirname, "../../fixtures");
export const fixture = (name: string) => path.join(fixturesDir, name);
export const stateFile = path.resolve(__dirname, "../../.state/accounts.json");
export const findingsFile = path.resolve(__dirname, "../../.state/findings.jsonl");
