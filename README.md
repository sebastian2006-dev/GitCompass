# GitCompass

Ask any GitHub codebase a question and get an answer grounded in the actual code — with file/line citations, not guesses.

Paste a repo URL, GitCompass clones it, chunks the code along function/class boundaries (via tree-sitter, not naive text splitting), embeds it locally, and lets you chat with it. Answers cite the exact file and line range they came from.

## How it works

1. **Ingest** — `POST /api/ingest` clones the repo, walks its files, and hands them to `chunker.py`
2. **Chunk** — tree-sitter parses each file into an AST and slices along function/class/method boundaries, so a chunk is never half a function
3. **Embed** — each chunk is embedded locally with `sentence-transformers` (`all-MiniLM-L6-v2`, 384-dim, no API key, no cost)
4. **Store** — chunks + embeddings are stored in Postgres via Supabase (pgvector), one shared database across all ingested repos
5. **Query** — `POST /api/query` retrieves relevant chunks (vector + keyword hybrid search) and passes them to Groq for answer generation, returning both the answer and its source chunks
6. **Compare** — `POST /api/compare` runs the same question against 2+ ingested repos independently, so answers never blend logic across repos

## Features

- **AST-aware chunking** — splits code by function/class/method, not fixed-size windows, so chunks stay semantically whole
- **Multi-language support** — Python, JavaScript, TypeScript, C, C++, Arduino
- **Exact-symbol routing** — count/list-style questions ("how many functions are in this file?") are answered by directly counting matched symbols instead of relying on the LLM to count from retrieved snippets
- **Hybrid retrieval** — combines vector similarity with keyword/full-text search over the same chunk store
- **Source citations** — every answer returns the file path and line range each claim came from
- **Multi-repo comparison** — ask one question across several ingested repos and see answers side by side
- **Retrieval evaluation harness** (`eval_retrieval.py`) — measures hit@k and MRR against a hand-labeled question/answer dataset, so retrieval quality is measurable instead of eyeballed
- **Graceful fallback** — repos with no chunkable files get a clear error listing detected vs. supported languages, instead of a silent failure
- **Animated frontend** — vanilla JS/HTML/CSS UI with an anime.js-driven compass rose background, Supabase-backed auth and chat history sync, and neomorphic styling

## Tech stack

**Backend** — Python, FastAPI, tree-sitter, sentence-transformers, Groq (LLM inference), Postgres + pgvector via Supabase

**Frontend** — Vanilla JS, HTML, CSS, anime.js, Supabase JS client

## Project structure

```
backend/
  api.py              FastAPI app — /api/ingest, /api/query, /api/compare, /api/repos, /api/health
  chunker.py           tree-sitter based AST chunking, one config per language
  ingest.py            clone -> chunk -> embed -> store pipeline
  retriever.py          vector + hybrid search, symbol lookup, file coverage
  rag_chain.py          prompt construction, Groq calls, count/list-question routing
  db.py                 Postgres/Supabase connection layer (pgvector)
  eval_retrieval.py     hit@k / MRR evaluation harness for retrieval quality
  requirements.txt
  .env                  API keys and Supabase credentials (not committed)

frontend/
  index.html             app shell — landing, auth, chat UI
  app.js                  state, API calls, chat/indexing UI logic
  supabase.js              Supabase client + auth/persistence helpers
  style.css                neomorphic styling
  supabase-schema.sql       Supabase table definitions
```

## Setup

### Backend

```bash
cd backend
pip install -r requirements.txt
```

Create a `.env` file in `backend/` with:

```
GROQ_API_KEY=your_groq_key          # https://console.groq.com/keys
SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SUPABASE_DB_PASSWORD=your_db_password
```

Run the schema in `frontend/supabase-schema.sql` against your Supabase project, then start the API:

```bash
python api.py
```

The API runs at `http://localhost:8000`.

### Frontend

Open `frontend/index.html` in a browser, or serve the folder with any static file server. Set your Supabase project URL and anon key in `frontend/supabase.js` (`SUPABASE_URL`, `SUPABASE_ANON_KEY`).

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/ingest` | POST | Clone, chunk, embed, and store a repo (`{ "repo_url": "..." }`) |
| `/api/query` | POST | Ask a question about an ingested repo (`{ "query": "...", "repo_url": "..." }`) |
| `/api/compare` | POST | Ask the same question across multiple repos (`{ "query": "...", "repo_urls": [...] }`) |
| `/api/repos` | GET | List every repo ingested so far |
| `/api/health` | GET | Health check |

## Evaluating retrieval quality

```bash
python ingest.py https://github.com/pallets/click   # ingest repos referenced in your dataset first
python eval_retrieval.py eval/eval_dataset.example.json
```

Reports hit@k and MRR against a labeled `(question, expected_file, expected_symbol)` dataset. Use `--fail-under 0.8` to fail the run (e.g. in CI) if the hit rate drops below a threshold.

## Supported languages

Python (`.py`), JavaScript (`.js`, `.jsx`), TypeScript (`.ts`, `.tsx`), C (`.c`, `.h`), C++ (`.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh`, `.hxx`), Arduino (`.ino`)