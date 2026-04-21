import type { QueryResponse } from './types'

export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

/**
 * POST /api/query — run a RAG query against the NJDOT backend.
 *
 * @param query      Natural-language question.
 * @param collection Optional collection filter: "specs_2019_v2" | "material_procs" |
 *                   "scheduling".  Omit (or pass undefined) to search all.
 * @throws Error with the backend detail message on non-2xx responses.
 */
export async function askQuestion(
  query: string,
  collection?: string,
): Promise<QueryResponse> {
  const res = await fetch(`${API_BASE}/api/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query,
      collection: collection ?? null,
    }),
  })

  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`
    try {
      const body = await res.json()
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // JSON parse failure — keep the generic message
    }
    throw new Error(detail)
  }

  return res.json() as Promise<QueryResponse>
}

async function _authPost(path: string, body: object, accessToken?: string): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed.')
  }
}

export async function requestPasswordReset(email: string): Promise<{ reset_token: string }> {
  const res = await fetch(`${API_BASE}/api/auth/request-reset`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
  if (!res.ok) throw new Error('Request failed.')
  return res.json()
}

export async function resetPassword(resetToken: string, newPassword: string): Promise<void> {
  await _authPost('/api/auth/reset-password', { reset_token: resetToken, new_password: newPassword })
}

export async function changePassword(newPassword: string, accessToken: string): Promise<void> {
  await _authPost('/api/auth/change-password', { new_password: newPassword }, accessToken)
}
