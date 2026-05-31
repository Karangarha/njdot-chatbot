# NJDOT AI Assistant: Codebase Overview

This document provides a comprehensive architectural overview of the **NJDOT AI Assistant** codebase. The system is designed as a hybrid RAG (Retrieval-Augmented Generation) chatbot to query over 700 pages of New Jersey Department of Transportation (NJDOT) documents and dynamically apply monthly Baseline Document Changes (BDCs).

## 🏗️ System Architecture

The project is structured as a decoupled monorepo containing a Python/FastAPI backend and a Next.js frontend.

### 1. Backend: Python/FastAPI
Located in the `backend/` directory:
- **`app/main.py`**: Application entry point, configures CORS, and wires up API routers.
- **`app/config.py`**: Manages Supabase keys, LLM credentials, and RAG chunk sizes (default `750` tokens, overlap `100`).
- **`app/models.py`**: Pydantic schemas for queries, citations, BDC alerts, and debug items.
- **`app/api/`**: Contains the REST endpoints (`query.py`, `review.py`, `auth.py`, `conversations.py`, `pdf.py`).

### 2. Frontend: Next.js + TypeScript
Located in the `frontend/` directory:
- Main app flows include the chatbot UI (`/chat`), the compliance reviewer (`/review`), and authentication (`/login`, `/signup`).

---

## ⚡ The RAG Pipeline

### 1. Ingestion Pipeline (`app/ingestion/`)
- **PDF Text Parsing (`pdf_parser.py`)**: Uses `pdfplumber` with a fallback to `PyMuPDF`.
- **Table Extraction (`table_extractor.py`)**: Detects tables, flattens multi-headers, and extracts markdown, capturing footnotes and captions.
- **Section Detection (`section_detector.py`)**: Identifies hierarchical headings (Division, Section, Subsection).
- **Chunking (`chunker.py`)**: Splits content into token-aware chunks (750 tokens), prepending necessary heading context to every split piece.

### 2. Retrieval Pipeline (`app/retrieval/`)
- **Vector Search (`vector_search.py`)**: Embedding search via `text-embedding-3-small` and Supabase RPC `match_chunks`.
- **Keyword Search (`bm25_search.py`)**: Postgres full-text search via Supabase RPC `keyword_search_chunks` using `tsvector`.
- **Hybrid Ranker (`hybrid_ranker.py`)**: Automatically categorizes queries (keyword-heavy vs. semantic) to assign weights, then merges results using **Reciprocal Rank Fusion (RRF)**.

### 3. Baseline Document Changes (BDC) Matching (`app/retrieval/bdc_matcher.py`)
- Maps amendments to specification section IDs using the `bdc_section_map` table.
- Injects BDC amendments into the LLM context **before** baseline text so the LLM treats them as the most authoritative, up-to-date clause.

### 4. Generation Pipeline (`app/generation/`)
- **Prompt Builder (`prompt_builder.py`)**: Injects context chunks, BDCs, and specific NJDOT system instructions.
- **LLM Client (`llm_client.py`)**: Interface supporting OpenAI (`gpt-4o-mini`) and Anthropic (`claude-sonnet`).
- **Citation Serializer (`citation_serializer.py`)**: Parses the generated JSON, validating and correcting hallucinated PDF page numbers against ground-truth metadata.

---

## 🔍 Schedule Compliance Reviewer (`app/api/review.py`)
Provides an endpoint (`/api/review`) that processes uploaded CPM schedule and narrative PDFs. It uses a structured LLM prompt to run 30+ compliance checks, including:
- Administrative dates and durations.
- Milestone completions and winter shutdowns.
- Environmental and permit constraints (e.g., in-water work windows).
- Material fabrication lead times and schedule logic (no negative float).

The endpoint returns a robust, structured JSON compliance report identifying passes, warnings, and failures.

---

## 📈 Testing & Evaluation
- **`scripts/run_eval.py`**: An automated evaluation script that runs the full RAG pipeline against a 100-question test set, using an LLM judge to score system performance across various question categories (e.g., table lookups, numeric values, multi-section synthesis).
