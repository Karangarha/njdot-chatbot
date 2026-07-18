'use client'

import { useEffect, useMemo, useState } from 'react'
import type { ComplianceCheck, SourceFile } from '@/lib/types'
import {
  addCheck, deleteCheck, getEffectiveChecks, resetToDefaults, setEnabled, updateCheck,
} from '@/lib/checklist'

// ── Props ────────────────────────────────────────────────────────────────────

interface ChecklistManagerProps {
  userId?: string
  /** Called whenever the effective checklist changes (and on initial load). */
  onChange?: (checks: ComplianceCheck[]) => void
  onClose: () => void
}

// ── Source-file selector ─────────────────────────────────────────────────────

const SOURCE_FILE_OPTIONS: { value: SourceFile; label: string }[] = [
  { value: 'schedule', label: 'Schedule' },
  { value: 'narrative', label: 'Narrative' },
  { value: 'sp', label: 'Special Provision' },
]

function SourceFilePicker({
  selected, onChange,
}: {
  selected: SourceFile[]
  onChange: (files: SourceFile[]) => void
}) {
  const allSelected = SOURCE_FILE_OPTIONS.every(o => selected.includes(o.value))
  const toggle = (file: SourceFile) =>
    onChange(selected.includes(file) ? selected.filter(f => f !== file) : [...selected, file])
  const toggleAll = () =>
    onChange(allSelected ? [] : SOURCE_FILE_OPTIONS.map(o => o.value))

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <label className="block text-[11px] font-semibold text-gray-600">
          Evidence source — which file(s) to check against
        </label>
        <button type="button" onClick={toggleAll}
          className="text-[11px] font-semibold text-[#1B3A6B] hover:underline">
          {allSelected ? 'Clear all' : 'Select all'}
        </button>
      </div>
      <div className="flex flex-wrap gap-3">
        {SOURCE_FILE_OPTIONS.map(o => (
          <label key={o.value} className="flex items-center gap-1.5 text-xs text-gray-700">
            <input type="checkbox" checked={selected.includes(o.value)}
              onChange={() => toggle(o.value)}
              className="h-3.5 w-3.5 accent-[#1B3A6B]" />
            {o.label}
          </label>
        ))}
      </div>
    </div>
  )
}

// ── Add / edit form ──────────────────────────────────────────────────────────

function CheckForm({
  initial, categories, onCancel, onSubmit,
}: {
  initial?: Pick<ComplianceCheck, 'category' | 'name' | 'instruction' | 'source_files'>
  categories: string[]
  onCancel: () => void
  onSubmit: (v: { category: string; name: string; instruction: string; source_files: SourceFile[] }) => void
}) {
  const [category, setCategory] = useState(initial?.category ?? '')
  const [name, setName] = useState(initial?.name ?? '')
  const [instruction, setInstruction] = useState(initial?.instruction ?? '')
  const [sourceFiles, setSourceFiles] = useState<SourceFile[]>(initial?.source_files ?? ['schedule'])
  const valid = name.trim().length > 0 && instruction.trim().length > 0 && sourceFiles.length > 0

  return (
    <div className="rounded-xl border border-[#1B3A6B]/20 bg-[#F5F8FF] p-4 space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-[11px] font-semibold text-gray-600">Category</label>
          <input
            list="checklist-categories" value={category}
            onChange={e => setCategory(e.target.value)}
            placeholder="e.g. Schedule Logic or a new category"
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#1B3A6B] focus:outline-none"
          />
          <datalist id="checklist-categories">
            {categories.map(c => <option key={c} value={c} />)}
          </datalist>
        </div>
        <div>
          <label className="mb-1 block text-[11px] font-semibold text-gray-600">Check name</label>
          <input
            value={name} onChange={e => setName(e.target.value)}
            placeholder="Short title shown in results"
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#1B3A6B] focus:outline-none"
          />
        </div>
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-semibold text-gray-600">
          Rule (instruction the AI evaluates)
        </label>
        <textarea
          value={instruction} onChange={e => setInstruction(e.target.value)} rows={3}
          placeholder="Describe exactly what to verify, e.g. 'Verify no paving activities are scheduled between Dec 15 and Mar 15.'"
          className="w-full resize-y rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#1B3A6B] focus:outline-none"
        />
      </div>
      <SourceFilePicker selected={sourceFiles} onChange={setSourceFiles} />
      {sourceFiles.length === 0 && (
        <p className="text-[11px] text-[#CC2529]">Select at least one file.</p>
      )}
      <div className="flex justify-end gap-2">
        <button onClick={onCancel}
          className="rounded-lg px-3 py-2 text-xs font-semibold text-gray-500 hover:bg-gray-100">
          Cancel
        </button>
        <button
          disabled={!valid}
          onClick={() => onSubmit({
            category: category.trim(), name: name.trim(), instruction: instruction.trim(),
            source_files: sourceFiles,
          })}
          className={`rounded-lg px-4 py-2 text-xs font-bold text-white transition-colors ${
            valid ? 'bg-[#1B3A6B] hover:bg-[#12264a]' : 'cursor-not-allowed bg-gray-300'}`}>
          Save check
        </button>
      </div>
    </div>
  )
}

// ── Main ─────────────────────────────────────────────────────────────────────

const SOURCE_FILE_LABEL: Record<SourceFile, string> = {
  schedule: 'Schedule', narrative: 'Narrative', sp: 'Special Provision',
}

/**
 * Groups checks into consecutive runs sharing the same non-empty category,
 * preserving the array's order. A blank category (`""`) starts no header —
 * each such check renders on its own with `header: null`, wherever it falls
 * in the sequence — while a run of checks sharing a real category (the doc's
 * true nested groups, e.g. Designer's Narrative) renders under one header.
 */
function groupSequential(checks: ComplianceCheck[]): { header: string | null; items: ComplianceCheck[] }[] {
  const runs: { header: string | null; items: ComplianceCheck[] }[] = []
  for (const c of checks) {
    const last = runs[runs.length - 1]
    if (c.category && last?.header === c.category) {
      last.items.push(c)
    } else if (!c.category && last?.header === null) {
      last.items.push(c)
    } else {
      runs.push({ header: c.category || null, items: [c] })
    }
  }
  return runs
}

export default function ChecklistManager({ userId, onChange, onClose }: ChecklistManagerProps) {
  const [checks, setChecks] = useState<ComplianceCheck[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState(false)
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [confirmDeleteKey, setConfirmDeleteKey] = useState<string | null>(null)
  const [confirmReset, setConfirmReset] = useState(false)

  const readOnly = !userId

  const reload = async () => {
    const data = await getEffectiveChecks(userId)
    setChecks(data)
    onChange?.(data)
  }

  useEffect(() => {
    let alive = true
    setLoading(true)
    getEffectiveChecks(userId).then(data => {
      if (!alive) return
      setChecks(data)
      onChange?.(data)
      setLoading(false)
    })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId])

  const categories = useMemo(
    () => [...new Set(checks.map(c => c.category).filter(Boolean))], [checks],
  )
  const enabledCount = checks.filter(c => c.enabled).length

  // ── Mutations (guarded by userId; each reloads to keep ids/fork state in sync) ──
  const guard = async (fn: () => Promise<void>) => {
    if (!userId) return
    setBusy(true)
    try { await fn(); await reload() } finally { setBusy(false) }
  }

  const handleToggle = (c: ComplianceCheck) =>
    guard(() => setEnabled(userId!, c.check_key, !c.enabled))
  const handleDelete = (c: ComplianceCheck) =>
    guard(() => deleteCheck(userId!, c.check_key)).then(() => setConfirmDeleteKey(null))
  const handleAdd = (v: { category: string; name: string; instruction: string; source_files: SourceFile[] }) =>
    guard(() => addCheck(userId!, v).then(() => {})).then(() => setAdding(false))
  const handleEdit = (
    checkKey: string,
    v: { category: string; name: string; instruction: string; source_files: SourceFile[] },
  ) => guard(() => updateCheck(userId!, checkKey, v)).then(() => setEditingKey(null))
  const handleReset = () =>
    guard(() => resetToDefaults(userId!)).then(() => setConfirmReset(false))

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">

        {/* Header */}
        <div className="shrink-0 border-b border-[#E8E8E8] px-5 py-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-base font-bold text-[#1B3A6B]">Compliance Checklist</h3>
              <p className="mt-0.5 text-xs text-gray-500">
                {loading ? 'Loading…' : `${enabledCount} of ${checks.length} checks selected to run`}
              </p>
            </div>
            <button onClick={onClose} aria-label="Close"
              className="rounded-full p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600">
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
          {readOnly && (
            <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
              Sign in to customize the checklist. These are the default checks that will run.
            </p>
          )}
          {!readOnly && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button onClick={() => { setAdding(true); setEditingKey(null) }} disabled={busy || adding}
                className="flex items-center gap-1.5 rounded-lg bg-[#CC2529] px-3 py-1.5 text-xs font-bold text-white hover:bg-[#a81e21] disabled:opacity-50">
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                </svg>
                Add check
              </button>
              {confirmReset ? (
                <span className="flex items-center gap-2 text-xs text-gray-600">
                  Reset all to defaults?
                  <button onClick={handleReset} disabled={busy}
                    className="font-semibold text-[#CC2529] hover:underline">Yes</button>
                  <button onClick={() => setConfirmReset(false)}
                    className="font-medium text-gray-400 hover:text-gray-600">No</button>
                </span>
              ) : (
                <button onClick={() => setConfirmReset(true)} disabled={busy}
                  className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-semibold text-gray-500 hover:border-gray-300 hover:text-gray-700 disabled:opacity-50">
                  Reset to defaults
                </button>
              )}
            </div>
          )}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {adding && !readOnly && (
            <CheckForm categories={categories} onCancel={() => setAdding(false)} onSubmit={handleAdd} />
          )}

          {loading ? (
            <p className="py-8 text-center text-sm text-gray-400">Loading checklist…</p>
          ) : (
            groupSequential(checks).map((run, runIndex) => (
              <div key={runIndex}
                className={run.header ? 'rounded-xl border border-[#1B3A6B]/15 bg-[#1B3A6B]/[0.02] p-3' : ''}>
                {run.header && (
                  <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-[#1B3A6B]">
                    {run.header}
                  </p>
                )}
                <div className="space-y-2">
                  {run.items.map(c => (
                    editingKey === c.check_key ? (
                      <CheckForm key={c.check_key} initial={c} categories={categories}
                        onCancel={() => setEditingKey(null)}
                        onSubmit={v => handleEdit(c.check_key, v)} />
                    ) : (
                      <div key={c.check_key}
                        className="flex items-start gap-3 rounded-xl border border-[#E8E8E8] bg-white px-3.5 py-3">
                        <input type="checkbox" checked={c.enabled} disabled={readOnly || busy}
                          onChange={() => handleToggle(c)}
                          className="mt-1 h-4 w-4 shrink-0 accent-[#1B3A6B] disabled:opacity-50" />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className={`text-sm font-semibold ${c.enabled ? 'text-gray-800' : 'text-gray-400 line-through'}`}>
                              {c.name}
                            </p>
                            {!c.is_builtin && (
                              <span className="rounded-full bg-[#1B3A6B]/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-[#1B3A6B]">
                                Custom
                              </span>
                            )}
                            {(c.source_files ?? []).map(f => (
                              <span key={f} className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-gray-500">
                                {SOURCE_FILE_LABEL[f] ?? f}
                              </span>
                            ))}
                          </div>
                          {c.instruction && (
                            <p className="mt-0.5 text-[11px] leading-relaxed text-gray-500">{c.instruction}</p>
                          )}
                        </div>
                        {!readOnly && (
                          <div className="flex shrink-0 items-center gap-1">
                            <button onClick={() => { setEditingKey(c.check_key); setAdding(false) }}
                              disabled={busy} aria-label="Edit check"
                              className="rounded p-1.5 text-gray-400 hover:bg-gray-100 hover:text-[#1B3A6B] disabled:opacity-50">
                              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                <path strokeLinecap="round" strokeLinejoin="round"
                                  d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931z" />
                              </svg>
                            </button>
                            {confirmDeleteKey === c.check_key ? (
                              <span className="flex items-center gap-1.5 pl-1 text-[11px]">
                                <button onClick={() => handleDelete(c)} disabled={busy}
                                  className="font-semibold text-[#CC2529] hover:underline">Delete</button>
                                <button onClick={() => setConfirmDeleteKey(null)}
                                  className="font-medium text-gray-400 hover:text-gray-600">No</button>
                              </span>
                            ) : (
                              <button onClick={() => setConfirmDeleteKey(c.check_key)}
                                disabled={busy} aria-label="Delete check"
                                className="rounded p-1.5 text-gray-400 hover:bg-gray-100 hover:!text-[#CC2529] disabled:opacity-50">
                                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                  <polyline points="3 6 5 6 21 6" />
                                  <path d="m19 6-.867 12.142A2 2 0 0116.138 20H7.862a2 2 0 01-1.995-1.858L5 6" />
                                  <path d="M10 11v6M14 11v6M9 6V4h6v2" />
                                </svg>
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  ))}
                </div>
              </div>
            ))
          )}
        </div>

        {/* Footer */}
        <div className="shrink-0 border-t border-[#E8E8E8] px-5 py-3 text-right">
          <button onClick={onClose}
            className="rounded-lg bg-[#1B3A6B] px-4 py-2 text-xs font-bold text-white hover:bg-[#12264a]">
            Done
          </button>
        </div>
      </div>
    </div>
  )
}
