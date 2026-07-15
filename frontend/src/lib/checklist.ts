// Supabase CRUD for the configurable compliance checklist.
//
// Built-in checks are shared rows with user_id === null (seeded by
// backend/scripts/seed_compliance_checks.py). A user starts by seeing those
// built-ins; the first time they edit anything we COPY-ON-WRITE — clone all
// built-ins into their own user_id set — after which they edit freely
// without affecting the shared template or other users.
//
// Mutations are keyed by `check_key` (stable, unique per user) rather than
// row `id`, so a toggle/edit issued while the user is still viewing
// built-ins maps to the correct forked row after ensureForked() runs.

import { createClient } from '@/lib/supabase/client'
import type { CheckSpec, ComplianceCheck, SourceFile } from '@/lib/types'

const TABLE = 'compliance_checks'

/** The user's effective checklist: their own rows if forked, else built-ins. */
export async function getEffectiveChecks(
  userId: string | null | undefined,
): Promise<ComplianceCheck[]> {
  const sb = createClient()
  if (userId) {
    const { data: own } = await sb
      .from(TABLE).select('*').eq('user_id', userId).order('sort_order')
    if (own && own.length > 0) return own as ComplianceCheck[]
  }
  const { data: builtins } = await sb
    .from(TABLE).select('*').is('user_id', null).order('sort_order')
  return (builtins ?? []) as ComplianceCheck[]
}

/** True once the user has their own (forked) rows. */
async function hasOwnChecks(userId: string): Promise<boolean> {
  const sb = createClient()
  const { count } = await sb
    .from(TABLE).select('id', { count: 'exact', head: true }).eq('user_id', userId)
  return (count ?? 0) > 0
}

/** Copy-on-write: clone the shared built-ins into the user's set (once). */
export async function ensureForked(userId: string): Promise<void> {
  if (await hasOwnChecks(userId)) return
  const sb = createClient()
  const { data: builtins } = await sb
    .from(TABLE).select('*').is('user_id', null).order('sort_order')
  if (!builtins || builtins.length === 0) return
  const rows = (builtins as ComplianceCheck[]).map(b => ({
    user_id: userId,
    check_key: b.check_key,
    category: b.category,
    name: b.name,
    instruction: b.instruction,
    check_type: b.check_type,
    source_files: b.source_files,
    is_builtin: b.is_builtin,   // preserve the built-in marker for the UI
    enabled: b.enabled,
    sort_order: b.sort_order,
  }))
  await sb.from(TABLE).insert(rows)
}

/** Enable/disable a check (drives which checks run). */
export async function setEnabled(
  userId: string, checkKey: string, enabled: boolean,
): Promise<void> {
  await ensureForked(userId)
  const sb = createClient()
  await sb.from(TABLE).update({ enabled, updated_at: new Date().toISOString() })
    .eq('user_id', userId).eq('check_key', checkKey)
}

/** Edit a check's editable fields. */
export async function updateCheck(
  userId: string, checkKey: string,
  patch: Partial<Pick<ComplianceCheck, 'name' | 'category' | 'instruction' | 'source_files'>>,
): Promise<void> {
  await ensureForked(userId)
  const sb = createClient()
  await sb.from(TABLE).update({ ...patch, updated_at: new Date().toISOString() })
    .eq('user_id', userId).eq('check_key', checkKey)
}

/** Remove a check from the user's checklist. */
export async function deleteCheck(userId: string, checkKey: string): Promise<void> {
  await ensureForked(userId)
  const sb = createClient()
  await sb.from(TABLE).delete().eq('user_id', userId).eq('check_key', checkKey)
}

/** Add a brand-new custom check (natural-language rule + selected file(s)). */
export async function addCheck(
  userId: string,
  input: { category: string; name: string; instruction: string; source_files: SourceFile[] },
): Promise<string> {
  await ensureForked(userId)
  const sb = createClient()
  const { data: existing } = await sb
    .from(TABLE).select('sort_order').eq('user_id', userId)
    .order('sort_order', { ascending: false }).limit(1)
  const nextOrder = ((existing?.[0]?.sort_order as number | undefined) ?? -1) + 1
  const checkKey = `custom_${crypto.randomUUID()}`
  await sb.from(TABLE).insert({
    user_id: userId,
    check_key: checkKey,
    category: input.category.trim() || 'Custom',
    name: input.name.trim(),
    instruction: input.instruction.trim(),
    check_type: 'llm',
    source_files: input.source_files.length > 0 ? input.source_files : ['schedule'],
    is_builtin: false,
    enabled: true,
    sort_order: nextOrder,
  })
  return checkKey
}

/** Reset the user back to the shared built-in defaults (drops their own rows). */
export async function resetToDefaults(userId: string): Promise<void> {
  const sb = createClient()
  await sb.from(TABLE).delete().eq('user_id', userId)
}

/** Map effective checks -> the `checks` payload for POST /api/review (enabled only). */
export function toCheckSpecs(checks: ComplianceCheck[]): CheckSpec[] {
  return checks
    .filter(c => c.enabled)
    .map(({ check_key, category, name, instruction, source_files }) => ({
      check_key, category, name, instruction, source_files,
    }))
}
