export interface BDCAlertItem {
  bdc_id: string
  section_id: string
  effective_date?: string
  subject?: string
  implementation_code?: string
  change_type?: string
}

export interface CitationItem {
  document: string
  section: string
  page_printed: number
  page_pdf: number
  chunk_id: string
}

export interface QueryResponse {
  answer: string
  citations: CitationItem[]
  query_type: string
  response_time_ms: number
  bdc_alerts: BDCAlertItem[]
}

export interface Conversation {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export interface ReviewProject {
  id: string
  session_id: string | null
  project_name: string
  review_result: ReviewResult | null
  schedule_file_path?: string | null
  narrative_pdf_path?: string | null
  special_provision_pdf_path?: string | null
  key_map_pdf_path?: string | null
  estimate_pdf_path?: string | null
  // Structured extraction JSON for the key map / estimate documents --
  // Supabase-only now; Neo4j no longer persists these (see backend
  // review.py's shaped["key_map"]/shaped["estimate"], the source of this
  // data on every /api/review response).
  key_map_extraction?: Record<string, unknown> | null
  estimate_extraction?: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

// Which document(s) a check draws evidence from — any combination.
// "spec"/"csm" are static, pre-ingested reference collections (Standard
// Specifications / Construction Scheduling Manual) rather than per-review
// uploads — see backend/scripts/ingest_specs.py.
export type SourceFile = 'schedule' | 'narrative' | 'sp' | 'keymap' | 'estimate' | 'spec' | 'csm'

export interface ComplianceCheck {
  id: string
  user_id: string | null
  check_key: string
  category: string
  name: string
  instruction: string
  check_type: string
  source_files: SourceFile[]
  is_builtin: boolean
  enabled: boolean
  sort_order: number
  created_at: string
  updated_at: string
}

// Enabled-only payload sent as the `checks` form field on POST /api/review.
// check_type must round-trip ("llm" | "geo") — the backend branches on it
// for deterministic checks (project_region_i195).
export interface CheckSpec {
  check_key: string
  category: string
  name: string
  instruction: string
  check_type: string
  source_files: SourceFile[]
}

// ── Review result (shape of /api/review's result, stored in review_projects) ──

export interface ReviewCitationItem {
  kind:        'public' | 'private'
  doc_type:    string
  label:       string
  page_pdf?:   number | null
  section_id?: string | null
  verified:    boolean
}

export interface CheckItem {
  id: string
  category: string
  name: string
  reasoning?: string
  status: 'pass' | 'warning' | 'fail'
  finding: string
  evidence: string
  citations?: ReviewCitationItem[]
}

export interface ReviewResult {
  project_id: string
  project_name: string
  project_duration_days: number
  model_used: string
  summary: {
    passed: number
    warnings: number
    failed: number
    manual_review: number
  }
  checks: CheckItem[]
  manual_review_items: string[]
  schedule_file_path?: string | null
  narrative_pdf_path?: string | null
  special_provision_pdf_path?: string | null
  key_map_pdf_path?: string | null
  // Full key map extraction (utilities, coordinates, sheet index, misc) +
  // deterministic north/south-of-I-195 result — persisted for later use.
  key_map?: { extraction: Record<string, unknown>; region: Record<string, unknown> } | null
  estimate_pdf_path?: string | null
  // Engineer's Estimate extraction + the deterministic Substantial-to-Final
  // gap computation — persisted for later use.
  estimate?: { extraction: Record<string, unknown>; cost_gap: Record<string, unknown> } | null
}
