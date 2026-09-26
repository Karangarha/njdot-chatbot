-- 010: hybrid retrieval support for session_chunks.
--
-- session_chunks and match_session_chunks predate this migrations directory
-- and were applied straight to the database; sp_retriever.py's docstring says
-- so. The first half of this file captures them verbatim from the running
-- database so the schema is reproducible, then the second half adds what
-- keyword retrieval needs.
--
-- Captured from the local Supabase container on 2026-09-14.
-- Idempotent: safe to re-run.

-- ── Captured: existing table ────────────────────────────────────────────────
-- NOTE session_id is TEXT, not uuid. Review project ids travel as strings
-- through the pipeline, and the new function below must match this type or
-- every call fails to resolve its arguments.
create table if not exists session_chunks (
    id         uuid primary key default gen_random_uuid(),
    session_id text not null,
    doc_type   text not null,
    content    text not null,
    embedding  vector(1536),
    metadata   jsonb,
    expires_at timestamptz not null default (now() + '10 years'::interval)
);

-- ── Captured: existing vector search ───────────────────────────────────────
-- Note what this does NOT do: it takes no doc_type argument, so it ranks
-- across every document type in the session and the caller filters afterwards.
-- That is the recall bug worked around in
-- backend/app/retrieval_langchain/sp_retriever.py by over-fetching. Recorded
-- here as-is, because this file documents what exists rather than what we wish
-- existed.
create or replace function public.match_session_chunks(
    query_embedding vector,
    p_session_id    text,
    match_count     integer          default 8,
    match_threshold double precision default 0.2
)
returns table(id uuid, content text, doc_type text, metadata jsonb, similarity double precision)
language sql
stable
as $function$
    SELECT
        id,
        content,
        doc_type,
        metadata,
        1 - (embedding <=> query_embedding) AS similarity
    FROM  session_chunks
    WHERE session_id = p_session_id
      AND expires_at > now()
      AND 1 - (embedding <=> query_embedding) >= match_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$function$;

-- ── New: full-text index for BM25-style keyword search ─────────────────────
create index if not exists session_chunks_content_fts
  on session_chunks
  using gin(to_tsvector('english', content));

-- ── New: metadata index for the exact anchor lookup ────────────────────────
-- Without this, pinning a named section is a sequential scan of the project's
-- chunks on every check.
create index if not exists session_chunks_metadata_gin
  on session_chunks
  using gin(metadata jsonb_path_ops);

-- ── New: keyword search, mirroring keyword_search_chunks ───────────────────
-- ts_rank_cd is cover-density ranking, which approximates BM25.
--
-- doc_type is filtered INSIDE this function, unlike match_session_chunks.
-- That is deliberate: a caller must not fetch N rows of any type and then
-- discard the ones it did not want, which is exactly how a check asking for 8
-- Special Provision passages ended up with 3 on a project that also holds a
-- narrative and a key map.
create or replace function public.keyword_search_session_chunks(
    search_query text,
    p_session_id text,
    p_doc_type   text default null,
    match_count  int  default 8
)
returns table(id uuid, content text, metadata jsonb, doc_type text, rank double precision)
language sql
stable
as $function$
    SELECT
        session_chunks.id,
        session_chunks.content,
        session_chunks.metadata,
        session_chunks.doc_type,
        ts_rank_cd(
            to_tsvector('english', session_chunks.content),
            websearch_to_tsquery('english', search_query)
        )::float8 AS rank
    FROM session_chunks
    WHERE session_chunks.session_id = p_session_id
      AND session_chunks.expires_at > now()
      AND (p_doc_type IS NULL OR session_chunks.doc_type = p_doc_type)
      AND to_tsvector('english', session_chunks.content)
            @@ websearch_to_tsquery('english', search_query)
    ORDER BY rank DESC
    LIMIT match_count;
$function$;
