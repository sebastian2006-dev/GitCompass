# api.py
# FastAPI endpoints tying together ingest.py, retriever.py, and rag_chain.py.

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from rag_chain import generate_answer
from ingest import ingest_repository, UnsupportedRepoError
from db import list_repos, get_repo_by_url

app = FastAPI(title="Codebase RAG API")

# Allow the vanilla-JS frontend (served from a different origin/port, e.g.
# file:// or a separate dev server) to call this API directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class IngestRequest(BaseModel):
    repo_url: str


class QueryRequest(BaseModel):
    query: str
    repo_url: str  # required — retrieval needs to know which repo to search


class CompareRequest(BaseModel):
    query: str
    repo_urls: list[str]  # 2+ previously-ingested repos to compare


@app.post("/api/ingest")
def ingest(request: IngestRequest):
    """
    Clone + chunk + embed + store a GitHub repo. Can take a while for larger
    repos (cloning + embedding), so the frontend should show a loading state.
    """
    try:
        result = ingest_repository(request.repo_url)
    except (ValueError, UnsupportedRepoError) as e:
        # e.g. no chunkable files found in the repo
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # covers clone failures (bad URL, private repo, network issues), etc.
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")

    return {
        "status": "success",
        "repo_url": result["repo_url"],
        "num_files": result["num_files"],
        "num_chunks": result["num_chunks"],
    }


@app.post("/api/query")
def query(request: QueryRequest):
    """
    Answer a question about a previously-ingested repo. Returns the answer
    text plus the source chunks that were retrieved, so the frontend can
    show citations.
    """
    try:
        result = generate_answer(request.query, repo_url=request.repo_url)
    except ValueError as e:
        # e.g. repo hasn't been ingested yet
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query failed: {e}")

    return {
        "answer": result.answer,
        "sources": result.sources,
    }


@app.get("/api/repos")
def repos():
    """
    List every repo ingested so far, most recently ingested first. Powers a
    "pick a repo to query" or "pick repos to compare" UI — this is what
    makes ingested repos visible/reusable across chats/sessions instead of
    living only in whatever collection was created during one run.
    """
    return {"repos": list_repos()}


@app.post("/api/compare")
def compare(request: CompareRequest):
    """
    Ask the same question against 2+ previously-ingested repos and return
    one answer per repo, so the frontend can show them side by side.

    Deliberately NOT a single merged answer across repos — each repo gets
    its own generate_answer() call, scoped to just that repo's chunks via
    repo_url, same as /api/query. Keeping these independent means each
    answer is grounded only in its own repo's actual code (no risk of the
    LLM blending/confusing two repos' logic together), and a failure in one
    repo's lookup (e.g. a typo'd URL) doesn't take down the whole comparison.
    """
    if len(request.repo_urls) < 2:
        raise HTTPException(
            status_code=400,
            detail="Comparison requires at least 2 repo_urls.",
        )

    results = []
    for repo_url in request.repo_urls:
        repo = get_repo_by_url(repo_url)
        if repo is None:
            results.append({
                "repo_url": repo_url,
                "error": f"Repo not ingested yet. Call /api/ingest first.",
            })
            continue

        try:
            answer = generate_answer(request.query, repo_url=repo_url)
            results.append({
                "repo_url": repo_url,
                "repo_name": repo["name"],
                "answer": answer.answer,
                "sources": answer.sources,
            })
        except Exception as e:
            results.append({
                "repo_url": repo_url,
                "error": f"Query failed: {e}",
            })

    return {"query": request.query, "results": results}


@app.get("/api/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)