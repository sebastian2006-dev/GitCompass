# ingest.py
# Clone a repo, chunk the code with tree-sitter (see chunker.py), embed the
# chunks locally with sentence-transformers (no API key, no cost), and store
# everything in Supabase/Postgres (pgvector) for later retrieval.
#
# CHANGED from the Chroma version: storage is now db.py's Postgres layer
# instead of a per-repo local Chroma collection. embed_chunks() and
# chunk_repository() are untouched -- only the storage step changed.

import os
import shutil
import tempfile

import git
import psycopg2.extras
from sentence_transformers import SentenceTransformer

from chunker import chunk_repository, CodeChunk, detect_repo_languages, SUPPORTED_LANGUAGES_DISPLAY
from db import get_connection, get_or_create_repo, update_repo_stats, delete_repo_chunks

# --- Config ------------------------------------------------------------

class UnsupportedRepoError(ValueError):
    """
    Raised when a repo has no chunkable source files. Carries structured
    detail (what languages the repo actually looks like it contains, vs.
    what's supported) so the API layer can send the frontend something more
    useful than a flat error string.
    """
    def __init__(self, message: str, detected_languages: dict = None):
        super().__init__(message)
        self.message = message
        self.detected_languages = detected_languages or {}
        self.supported_languages = SUPPORTED_LANGUAGES_DISPLAY


# Local embedding model. Runs on CPU, no API key required, ~80MB download
# on first use (cached afterward). 384-dim vectors -- MUST match the
# `vector(384)` column dimension in schema.sql. If this model ever changes,
# the schema needs a matching migration, not just this constant.
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

# Batch size for embedding calls — keeps memory bounded on large repos.
EMBED_BATCH_SIZE = 64

# Batch size for Postgres inserts, mirrors the old Chroma BATCH constant.
DB_INSERT_BATCH_SIZE = 500


_embedding_model = None  # lazy-loaded singleton, avoid reloading the model per call


def get_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _embedding_model


def clone_repo(repo_url: str, dest_dir: str) -> str:
    """
    Shallow-clone a repo (depth=1) to keep this fast and disk-light.

    Explicitly closes the Repo object after cloning. GitPython holds native
    git process handles open on the packed object files (.git/objects/pack/*)
    until the Repo object is closed or garbage collected -- on Windows this
    causes shutil.rmtree() to fail with WinError 5 (Access is denied) if it
    runs before that handle is released, since GC timing isn't guaranteed to
    happen before the cleanup step that follows.
    """
    repo = git.Repo.clone_from(repo_url, dest_dir, depth=1)
    repo.close()
    return dest_dir


def embed_chunks(chunks: list[CodeChunk]) -> list[list[float]]:
    """Embed all chunks locally in batches. Returns one vector per chunk."""
    model = get_embedding_model()
    texts = [c.to_embedding_text() for c in chunks]
    embeddings = model.encode(
        texts,
        batch_size=EMBED_BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return embeddings.tolist()


def store_chunks(repo_url: str, chunks: list[CodeChunk], embeddings: list[list[float]]) -> str:
    """
    Store chunks + embeddings + metadata in Supabase (Postgres/pgvector).
    Deletes any existing chunks for this repo first (re-ingest = fresh),
    same semantics as the old "delete collection, recreate" Chroma behavior.

    Returns the repo's UUID (was: Chroma collection_name).
    """
    repo_id = get_or_create_repo(repo_url)
    delete_repo_chunks(repo_id)

    conn = get_connection()
    with conn.cursor() as cur:
        rows = [
            (
                repo_id,
                c.file_path,
                c.start_line,
                c.end_line,
                c.symbol_name,
                c.symbol_type,
                c.language,
                c.content,
                c.to_embedding_text(),
                embeddings[i],
            )
            for i, c in enumerate(chunks)
        ]

        for i in range(0, len(rows), DB_INSERT_BATCH_SIZE):
            batch = rows[i:i + DB_INSERT_BATCH_SIZE]
            psycopg2.extras.execute_values(
                cur,
                """
                insert into chunks
                    (repo_id, file_path, start_line, end_line,
                     symbol_name, symbol_type, language, content,
                     embedding_text, embedding)
                values %s
                """,
                batch,
            )
        conn.commit()

    files_touched = len(set(c.file_path for c in chunks))
    update_repo_stats(repo_id, num_files=files_touched, num_chunks=len(chunks))

    return repo_id


def ingest_repository(repo_url: str) -> dict:
    """
    Clone a repository, chunk the code files, generate embeddings,
    and store them in Supabase.

    Returns a summary dict so the API layer can report back to the
    frontend (file/chunk counts, repo_id for querying later -- this
    replaces the old collection_name).
    """
    
    tmp_dir = tempfile.mkdtemp(prefix="codebase-rag-")
    try:
        print(f"Cloning {repo_url} ...")
        clone_repo(repo_url, tmp_dir)

        print("Chunking source files ...")
        chunks = chunk_repository(tmp_dir)

        if not chunks:
            langs = detect_repo_languages(tmp_dir)
            other = langs["other"]
            if other:
                detected_str = ", ".join(other.keys())
                message = (
                    f"This repo looks like it's mostly {detected_str}, which "
                    f"Codebase RAG doesn't support yet."
                )
            else:
                message = (
                    "No chunkable source files were found in this repo — it "
                    "may be empty, use an unrecognized language, or only "
                    "contain non-code files (docs, config, assets)."
                )
            message += " Currently supported: " + ", ".join(SUPPORTED_LANGUAGES_DISPLAY) + "."
            raise UnsupportedRepoError(message, detected_languages=other)

        print(f"Embedding {len(chunks)} chunks locally ...")
        embeddings = embed_chunks(chunks)

        print("Storing in Supabase ...")
        repo_id = store_chunks(repo_url, chunks, embeddings)

        files_touched = len(set(c.file_path for c in chunks))
        return {
            "repo_url": repo_url,
            "repo_id": repo_id,
            "num_files": files_touched,
            "num_chunks": len(chunks),
        }
    finally:
        def _onerror(func, path, _):
            import stat
            os.chmod(path, stat.S_IWRITE)
            func(path)
        shutil.rmtree(tmp_dir, onerror=_onerror, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python ingest.py <repo_url>")
        sys.exit(1)

    result = ingest_repository(sys.argv[1])
    print(f"\nDone: {result['num_chunks']} chunks from {result['num_files']} files")
    print(f"Repo ID: {result['repo_id']}")