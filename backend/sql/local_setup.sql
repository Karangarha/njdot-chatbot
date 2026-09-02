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
