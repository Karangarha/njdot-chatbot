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
  review_result: any
  schedule_file_path?: string | null
  narrative_pdf_path?: string | null
  special_provision_pdf_path?: string | null
  key_map_pdf_path?: string | null
  estimate_pdf_path?: string | null
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
