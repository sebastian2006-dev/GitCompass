# db.py
# Supabase/Postgres connection layer. Replaces ingest.py's get_chroma_client()
# and _collection_name_for_repo() -- storage is now one shared Postgres
# database (via pgvector) instead of per-repo local Chroma collections.
#
# Uses a direct psycopg2 connection (not the Supabase REST client) because
# retriever.py needs to run real SQL: vector distance operators, ts_rank
# full-text scoring, and combining both in one hybrid-search query. The
# REST client doesn't expose that -- raw SQL is the simpler path here.

import os
import re

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# register pgvector's adapter so Python lists <-> vector column transparently
from pgvector.psycopg2 import register_vector


_connection = None  # lazy singleton, mirrors ingest.py's _embedding_model pattern


def _build_dsn() -> str:
    """
    Build a Postgres connection string from Supabase env vars.

    Supabase's direct-connection host is derived from the project URL:
    https://xxxxx.supabase.co  ->  db.xxxxx.supabase.co
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    db_password = os.environ.get("SUPABASE_DB_PASSWORD")

    if not supabase_url or not db_password:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_DB_PASSWORD must be set in the "
            "environment (.env file in backend/). See .env.example."
        )

    # https://abcdefgh.supabase.co -> abcdefgh
    project_ref = supabase_url.replace("https://", "").replace("http://", "").split(".")[0]
    host = f"db.{project_ref}.supabase.co"

    return (
        f"host={host} port=5432 dbname=postgres user=postgres "
        f"password={db_password} sslmode=require"
    )


def get_connection():
    """Lazy-loaded singleton connection, reused across calls within a process."""
    global _connection
    if _connection is None or _connection.closed:
        dsn = _build_dsn()
        _connection = psycopg2.connect(dsn)
        register_vector(_connection)
    return _connection


def _slug_from_repo_url(repo_url: str) -> str:
    """
    Human-readable name derived from the repo URL, e.g.
    'https://github.com/prwtham/esp32minios.git' -> 'esp32minios'.
    Purely cosmetic (shown in UI/repo lists) -- repo_url is what's actually
    unique, not this slug.
    """
    raw = repo_url.rstrip("/").split("/")[-1]
    if raw.endswith(".git"):
        raw = raw[:-4]
    return raw or "repo"


def get_or_create_repo(repo_url: str) -> str:
    """
    Return the repo's UUID, creating a row in `repos` if this URL hasn't
    been ingested before. Called at the start of ingest_repository().
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("select id from repos where repo_url = %s", (repo_url,))
        row = cur.fetchone()
        if row:
            return str(row[0])

        cur.execute(
            "insert into repos (repo_url, name) values (%s, %s) returning id",
            (repo_url, _slug_from_repo_url(repo_url)),
        )
        repo_id = cur.fetchone()[0]
        conn.commit()
        return str(repo_id)


def update_repo_stats(repo_id: str, num_files: int, num_chunks: int) -> None:
    """Update file/chunk counts after a (re-)ingest. Also bumps ingested_at."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            update repos
            set num_files = %s, num_chunks = %s, ingested_at = now()
            where id = %s
            """,
            (num_files, num_chunks, repo_id),
        )
        conn.commit()


def delete_repo_chunks(repo_id: str) -> None:
    """
    Delete all chunks for a repo before re-ingesting. Mirrors the old
    store_chunks()'s "delete collection, recreate fresh" behavior -- a
    re-ingest should fully replace old data, not append to it.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("delete from chunks where repo_id = %s", (repo_id,))
        conn.commit()


def list_repos() -> list[dict]:
    """
    All ingested repos, most recently ingested first. Powers a "pick a repo
    to query / compare" UI across chats.
    """
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select id, repo_url, name, ingested_at, num_files, num_chunks
            from repos
            order by ingested_at desc
            """
        )
        return [dict(row) for row in cur.fetchall()]


def get_repo_by_url(repo_url: str) -> dict | None:
    """Look up a repo's row by URL. Returns None if never ingested."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select id, repo_url, name, ingested_at, num_files, num_chunks
            from repos where repo_url = %s
            """,
            (repo_url,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_repo_by_id(repo_id: str) -> dict | None:
    """Look up a repo's row by id. Used to resolve repo_id -> repo_url etc."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select id, repo_url, name, ingested_at, num_files, num_chunks
            from repos where id = %s
            """,
            (repo_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None