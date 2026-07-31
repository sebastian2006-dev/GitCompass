# retriever.py
# Embed an incoming query with the same local model used at ingest time,
# run search against Supabase (Postgres/pgvector), and return the top-k
# matching code chunks along with their file/line metadata.
#
# CHANGED from the Chroma version:
#   - retrieve_context() now runs a pgvector cosine-distance query instead
#     of collection.query(). Same signature/behavior, new backend.
#   - retrieve_file_coverage() now runs a Postgres query grouped by
#     file_path instead of collection.get(). Same signature/behavior.
#   - NEW: retrieve_hybrid() combines vector similarity with Postgres
#     full-text search (ts_rank against content_tsv), so exact identifier
#     matches (e.g. "getUserById") aren't lost to a purely semantic search
#     that only "gets the gist" of the query.
#   - repo_url is now resolved via db.py's repo registry instead of a
#     collection-name slug/hash.

from dataclasses import dataclass

import psycopg2.extras

from db import get_connection, get_repo_by_url, get_repo_by_id
from ingest import get_embedding_model


@dataclass
class RetrievedChunk:
    """A single retrieval result — the chunk text plus where it came from."""
    content: str
    file_path: str
    start_line: int
    end_line: int
    symbol_name: str
    symbol_type: str
    language: str
    distance: float  # lower = more similar (cosine distance, 0=identical, 2=opposite)

    def source_label(self) -> str:
        """Human-readable citation, e.g. 'auth.py:45-62 (function login)'."""
        return f"{self.file_path}:{self.start_line}-{self.end_line} ({self.symbol_type} {self.symbol_name})"


def _resolve_repo_id(repo_url: str = None, repo_id: str = None) -> str:
    """
    Resolve either a repo_url or repo_id into a concrete repo_id.
    Mirrors the old _resolve_collection_name()'s job, but against the real
    `repos` table instead of deriving a slug/hash string.
    """
    if repo_id:
        return repo_id
    if repo_url:
        repo = get_repo_by_url(repo_url)
        if repo is None:
            raise ValueError(
                f"No ingested repo found for '{repo_url}'. "
                f"Did you call ingest_repository() on it first?"
            )
        return str(repo["id"])
    raise ValueError("retrieve_context requires either repo_url or repo_id")


def _row_to_chunk(row: dict, distance: float = 0.0) -> RetrievedChunk:
    """Shared row -> RetrievedChunk mapping, used by all three retrieval paths."""
    return RetrievedChunk(
        content=row["content"],
        file_path=row["file_path"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        symbol_name=row["symbol_name"],
        symbol_type=row["symbol_type"],
        language=row["language"],
        distance=distance,
    )


def retrieve_context(
    query: str,
    repo_url: str = None,
    repo_id: str = None,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """
    Perform pgvector cosine-similarity search to find code chunks relevant
    to the given query, scoped to one repo.

    Note: pure similarity top-k is a good fit for targeted questions ("where
    is X handled") but a poor fit for broad "explain the whole repo" style
    questions, since it will only ever surface the top_k highest-scoring
    chunks and has no notion of "make sure every file is represented." See
    retrieve_file_coverage() for that case, or retrieve_hybrid() for
    questions that hinge on an exact identifier/keyword match.
    """
    resolved_repo_id = _resolve_repo_id(repo_url, repo_id)

    model = get_embedding_model()
    query_embedding = model.encode([query], convert_to_numpy=True)[0].tolist()

    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select file_path, start_line, end_line, symbol_name,
                   symbol_type, language, content,
                   embedding <=> %s::vector as distance
            from chunks
            where repo_id = %s
            order by embedding <=> %s::vector
            limit %s
            """,
            (query_embedding, resolved_repo_id, query_embedding, top_k),
        )
        rows = cur.fetchall()

    return [_row_to_chunk(row, distance=row["distance"]) for row in rows]


# Cap on how many chunks we'll pull per file for file-coverage retrieval.
# Most files fit comfortably in 1-2 chunks (module fallback, or a couple of
# functions); this just guards against one huge file with many functions
# eating a disproportionate share of the prompt budget.
MAX_CHUNKS_PER_FILE_FOR_COVERAGE = 2

# Hard ceiling on total chunks returned by file-coverage retrieval, so a
# very large repo can't blow the LLM's context window. If a repo has more
# distinct files than this, coverage is best-effort (first N encountered),
# not guaranteed complete — there's no good way to fully summarize an
# arbitrarily large repo in one prompt regardless of retrieval strategy.
MAX_TOTAL_CHUNKS_FOR_COVERAGE = 40


def retrieve_file_coverage(
    repo_url: str = None,
    repo_id: str = None,
    max_chunks_per_file: int = MAX_CHUNKS_PER_FILE_FOR_COVERAGE,
    max_total_chunks: int = MAX_TOTAL_CHUNKS_FOR_COVERAGE,
) -> list[RetrievedChunk]:
    """
    Retrieve a representative sample of chunks covering every distinct file
    in the repo, instead of the top-k-by-similarity chunks for a specific
    query. Used for broad "explain the whole repo" / "what does each file
    do" style questions, where the point is breadth of coverage rather than
    relevance to a narrow query embedding.

    Uses a window function (row_number() per file_path, ordered by
    start_line) to take up to max_chunks_per_file per file directly in SQL,
    equivalent to the old in-Python grouping over Chroma's full collection
    dump.
    """
    resolved_repo_id = _resolve_repo_id(repo_url, repo_id)

    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            with ranked as (
                select file_path, start_line, end_line, symbol_name,
                       symbol_type, language, content,
                       row_number() over (
                           partition by file_path order by start_line
                       ) as rn
                from chunks
                where repo_id = %s
            )
            select file_path, start_line, end_line, symbol_name,
                   symbol_type, language, content
            from ranked
            where rn <= %s
            order by file_path, start_line
            limit %s
            """,
            (resolved_repo_id, max_chunks_per_file, max_total_chunks),
        )
        rows = cur.fetchall()

    return [_row_to_chunk(row) for row in rows]  # distance not meaningful here


# Weighting for hybrid search's combined score. Vector similarity captures
# semantic/conceptual matches ("the login logic"); full-text captures exact
# identifier matches ("getUserById"). Weighted toward vector by default since
# most questions are conceptual, but full-text still meaningfully breaks
# ties/surfaces exact-name hits that pure similarity can bury.
HYBRID_VECTOR_WEIGHT = 0.6
HYBRID_TEXT_WEIGHT = 0.4


def retrieve_hybrid(
    query: str,
    repo_url: str = None,
    repo_id: str = None,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """
    Combined vector + full-text (BM25-equivalent) search in a single SQL
    query. Where retrieve_context() can miss exact symbol matches (e.g. a
    query for "getUserById" might rank a semantically-similar-but-differently-
    named function higher), this blends in Postgres's ts_rank score against
    content_tsv so exact/near-exact keyword hits aren't lost to pure
    semantic similarity.

    Score = HYBRID_VECTOR_WEIGHT * (1 - cosine_distance) + HYBRID_TEXT_WEIGHT * ts_rank,
    normalized so both components are roughly comparable in scale before
    combining (ts_rank's raw scale is unbounded and not directly comparable
    to a 0-1 cosine similarity, so this uses ts_rank_cd with normalization
    option 32 -- divides by itself + 1 -- to keep it in a similar 0-1ish range).
    """
    resolved_repo_id = _resolve_repo_id(repo_url, repo_id)

    model = get_embedding_model()
    query_embedding = model.encode([query], convert_to_numpy=True)[0].tolist()

    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select file_path, start_line, end_line, symbol_name,
                   symbol_type, language, content,
                   1 - (embedding <=> %(qvec)s::vector) as vector_score,
                   coalesce(
                       ts_rank_cd(content_tsv, plainto_tsquery('english', %(query)s), 32),
                       0
                   ) as text_score
            from chunks
            where repo_id = %(repo_id)s
            order by
                (%(vw)s * (1 - (embedding <=> %(qvec)s::vector)))
                + (%(tw)s * coalesce(
                    ts_rank_cd(content_tsv, plainto_tsquery('english', %(query)s), 32),
                    0
                  ))
                desc
            limit %(top_k)s
            """,
            {
                "qvec": query_embedding,
                "query": query,
                "repo_id": resolved_repo_id,
                "vw": HYBRID_VECTOR_WEIGHT,
                "tw": HYBRID_TEXT_WEIGHT,
                "top_k": top_k,
            },
        )
        rows = cur.fetchall()

    # distance = 1 - vector_score, kept consistent with retrieve_context()'s
    # "lower is more similar" convention
    return [_row_to_chunk(row, distance=1 - row["vector_score"]) for row in rows]
def get_known_symbol_names(repo_id: str) -> list[str]:
    """
    Distinct symbol names present in this repo, used by rag_chain.py to
    check whether a count/list-style question names a known symbol
    (e.g. "how many tickers are in COMMON_TICKERS") before deciding to
    bypass hybrid/broad retrieval in favor of exact retrieval.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct symbol_name
            from chunks
            where repo_id = %s and symbol_name is not null
            """,
            (repo_id,),
        )
        return [row[0] for row in cur.fetchall()]


def retrieve_by_symbol(symbol_name: str, repo_id: str) -> list[RetrievedChunk]:
    """
    Return every chunk for an exact symbol_name match, ordered by start_line.
    Used for count/enumerate-style questions where similarity top-k can't
    guarantee it surfaces *every* sub-chunk of a large symbol -- this pulls
    the complete set instead of a similarity-ranked subset.
    """
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select file_path, start_line, end_line, symbol_name,
                   symbol_type, language, content
            from chunks
            where repo_id = %s and symbol_name = %s
            order by start_line
            """,
            (repo_id, symbol_name),
        )
        rows = cur.fetchall()

    return [_row_to_chunk(row) for row in rows]  # distance not meaningful here    


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python retriever.py <repo_url> <query>")
        sys.exit(1)

    repo_url, query = sys.argv[1], sys.argv[2]
    results = retrieve_hybrid(query, repo_url=repo_url, top_k=5)

    print(f"Top {len(results)} hybrid results for: {query!r}\n")
    for r in results:
        print(f"[{r.distance:.4f}] {r.source_label()}")
        print(r.content[:200].strip())
        print("---")