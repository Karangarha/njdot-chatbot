-- ── LOCAL TEST DATABASE SETUP ────────────────────────────────────────────────
--
-- Run this once in the Supabase SQL editor of your TEST project.
-- It creates everything the backend needs: the pgvector extension,
-- the chunks table, both search functions, and the FTS index.
--
-- Steps:
--   1. Open your test Supabase project → SQL Editor
--   2. Paste this entire file and click "Run"
--   3. You should see no errors
-- ─────────────────────────────────────────────────────────────────────────────


-- 1. Enable the pgvector extension (required for vector similarity search).
--    pgvector stores and indexes the 1536-dimensional OpenAI embeddings.
create extension if not exists vector;


-- 2. Create the chunks table.
--    Each row is one chunk from the ingested PDFs.
create table if not exists chunks (
    id          uuid primary key default gen_random_uuid(),
    content     text        not null,   -- the actual text of the chunk
    metadata    jsonb       not null default '{}'::jsonb,  -- section_id, page, doc, etc.
    embedding   vector(1536),           -- OpenAI text-embedding-3-small output
    collection  text        not null,   -- "specs_2019" | "scheduling" | "material_procs"
    created_at  timestamptz default now()
);


-- 3. IVFFlat index for fast approximate nearest-neighbour vector search.
--    lists=100 is a good default for a corpus of ~5,000–20,000 chunks.
--    cosine distance (<=>)  matches how similarity is computed in match_chunks.
create index if not exists chunks_embedding_idx
    on chunks
    using ivfflat (embedding vector_cosine_ops)
    with (lists = 100);


-- 4. GIN index for fast PostgreSQL full-text search (used by keyword_search_chunks).
create index if not exists chunks_content_fts
    on chunks
    using gin(to_tsvector('english', content));


-- 5. Vector similarity search function (called by VectorSearcher).
create or replace function match_chunks(
    query_embedding   vector(1536),
    match_count       int      default 10,
    filter_collection text     default null,
    match_threshold   float8   default 0.0
)
returns table (
    id          uuid,
    content     text,
    metadata    jsonb,
    similarity  float8,
    collection  text
)
language sql stable
as $$
    select
        chunks.id,
        chunks.content,
        chunks.metadata,
        1 - (chunks.embedding <=> query_embedding) as similarity,
        chunks.collection
    from chunks
    where
        (filter_collection is null or chunks.collection = filter_collection)
        and 1 - (chunks.embedding <=> query_embedding) > match_threshold
    order by chunks.embedding <=> query_embedding
    limit match_count;
$$;


-- 6. Full-text keyword search function (called by KeywordSearcher).
--    Uses ts_rank_cd (cover-density ranking) which approximates BM25.
create or replace function keyword_search_chunks(
    search_query      text,
    match_count       int  default 10,
    filter_collection text default null
)
returns table (
    id          uuid,
    content     text,
    metadata    jsonb,
    rank        float8,
    collection  text
)
language sql stable
as $$
    select
        chunks.id,
        chunks.content,
        chunks.metadata,
        ts_rank_cd(
            to_tsvector('english', chunks.content),
            websearch_to_tsquery('english', search_query)
        )::float8 as rank,
        chunks.collection
    from chunks
    where
        (filter_collection is null or chunks.collection = filter_collection)
        and to_tsvector('english', chunks.content)
              @@ websearch_to_tsquery('english', search_query)
    order by rank desc
    limit match_count;
$$;


-- Done. Verify with:
--   select count(*) from chunks;   -- should be 0 (empty until you ingest)
--   select * from match_chunks('[0.1, 0.2, ...]'::vector(1536), 5);  -- tests the function


-- 6. Per-project uploaded documents (session_chunks) and their search
--    functions. These predated the migrations directory and lived only in the
--    database; captured here so a fresh local instance is complete from this
--    one file. See backend/migrations/010_session_chunks_hybrid.sql.
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
