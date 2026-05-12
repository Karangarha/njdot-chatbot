# NJDOT AI Assistant

A RAG (Retrieval-Augmented Generation) chatbot for querying New Jersey Department of Transportation documents — standard specifications, material procedures, and construction scheduling manuals.

---

## Prerequisites

- Python 3.11+
- Node.js 18+
- A Supabase project with the `pgvector` extension enabled
- An OpenAI API key (for embeddings)
- An Anthropic API key (for Claude LLM)

---

## Backend Setup & Running

```bash
cd backend

# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate       

# 2. Install dependencies
pip install -r requirements.txt
# 3. Copy the env template and fill in your values
cp .env.example .env

# 4. Start the API server
uvicorn app.main:app --reload --port 8000
# Runs at  http://localhost:8000
# API docs at http://localhost:8000/docs
```

---

## Frontend Setup & Running

```bash
cd frontend

# 1. Install dependencies
npm install

# 2. Create the local env file
cp .env.local.example .env.local

# 3. Start the dev server
npm run dev
# Runs at http://localhost:3000
```

---

## Running Ingestion (if starting fresh)

Run these from inside the activated virtual environment (`cd backend && source venv/bin/activate`).

```bash
# Ingest all collections (specs, material procedures, scheduling)
python scripts/ingest_specs.py

# Ingest a single collection
python scripts/ingest_specs.py --collection specs_2019_v2
python scripts/ingest_specs.py --collection material_procs
python scripts/ingest_specs.py --collection scheduling

# Dry run — parse and chunk PDFs without writing to the database
python scripts/ingest_specs.py --dry-run

# Deploy (or re-deploy) the SQL functions to Supabase
python scripts/deploy_sql.py

# Smoke-test retrieval against the live database
python scripts/test_retrieval.py

```

## Testing
```bash
cd backend
python scripts/run_eval.py
```
