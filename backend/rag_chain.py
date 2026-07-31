# rag_chain.py
# Retrieve relevant code chunks, build a grounded prompt, and call Groq
# (openai/gpt-oss-120b — the recommended successor to the now-decommissioned
# llama-3.3-70b-versatile) to answer the question.
#
# CHANGED for the Supabase migration:
#   - Targeted questions now use retrieve_hybrid() (vector + full-text)
#     instead of retrieve_context() (vector-only). Hybrid is a superset of
#     vector-only search (it includes the vector score plus text matching),
#     so this is a straight upgrade, not an A/B choice.
#   - collection_name param renamed to repo_id throughout, matching db.py's
#     repo registry (see retriever.py's _resolve_repo_id).
#
# CHANGED — exact-symbol routing:
#   - Count/list-all questions ("how many tickers", "list all the routes")
#     are a routing problem, not a retrieval-ranking problem: no amount of
#     tuning hybrid search guarantees it returns *every* sub-chunk of a
#     large symbol, only the highest-scoring ones. If the question names a
#     known symbol, we now bypass hybrid/broad retrieval entirely and pull
#     every chunk for that exact symbol_name via retrieve_by_symbol(), so
#     the LLM sees the complete set instead of a similarity-ranked subset.
#
# CHANGED this session — deterministic counting + safe truncation:
#   - Sending a very large exact-symbol chunk whole to Groq can exceed the
#     provider's tokens-per-minute limit (a 31KB dict literal came out to
#     ~17k tokens against an 8k TPM cap). Two different fixes for two
#     different question shapes:
#       1. Pure counting questions ("how many X") never need the LLM to see
#          the chunk text at all -- try_count_literal_entries() parses the
#          stored content directly with ast.literal_eval() and returns an
#          exact count, skipping the LLM call (and the token-limit risk)
#          entirely. This is also more *correct*, not just cheaper: LLMs are
#          unreliable at precisely counting entries in a large text blob
#          even when given the complete, untruncated content.
#       2. Non-counting exact-symbol questions ("list some tickers") do need
#          the LLM to read real entries, so they can't be short-circuited
#          the same way. These now truncate at MAX_EXACT_SYMBOL_CHARS
#          instead of never truncating, and the prompt's framing switches
#          from "this is the complete set" to "this is a partial sample"
#          when truncation actually happens, so the model doesn't
#          confidently state a false total from partial data.

import ast
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv
from groq import Groq

from db import get_repo_by_url
from retriever import (
    retrieve_hybrid,
    retrieve_file_coverage,
    retrieve_by_symbol,
    get_known_symbol_names,
    RetrievedChunk,
)

load_dotenv()

# --- Config --------------------------------------------------------------

GROQ_MODEL = "openai/gpt-oss-120b"
TOP_K = 5
MAX_CONTEXT_CHARS_PER_CHUNK = 3000  # guard against a single huge chunk dominating the prompt
MAX_ANSWER_TOKENS = 2048  # room for detailed, multi-function explanations without cutting off mid-sentence
MAX_ANSWER_TOKENS_BROAD = 4096  # broad "explain every file" answers cover far more ground per response,
                                 # so they get double the budget — otherwise ~15 files/2048 tokens works
                                 # out to one clipped sentence each

# Safe ceiling for exact-symbol chunk content sent to the LLM. Groq's
# on-demand tier caps requests at 8000 tokens/minute for our model; a large
# single chunk (e.g. a big constant dict) can exceed that on its own if sent
# whole. Pure counting questions never hit this at all -- they're answered
# by try_count_literal_entries() without any LLM call. This cap only
# matters for non-counting exact-symbol questions (e.g. "list some
# tickers"), where the chunk gets truncated to a representative sample
# instead of failing outright with a 413.
MAX_EXACT_SYMBOL_CHARS = 20000  # ~5-6k tokens, leaves headroom under 8k TPM

# Phrases that signal a "summarize/explain everything" question rather than
# a targeted lookup. Similarity top-k search structurally can't answer these
# well (it only returns the highest-scoring chunks against one query
# embedding, with no guarantee every file is represented), so these route to
# retrieve_file_coverage() instead.
#
# Two-part matching, not exact phrases: a "coverage word" (each/every/all/
# whole/entire) anywhere in the query PLUS a "target word" (file/files/repo/
# codebase/project) anywhere in the query. This is deliberately
# order/wording independent -- "each of the files", "every single file",
# "all the files in this repo" all match, whereas a fixed phrase list like
# "each file" only matches that exact adjacent wording and silently misses
# everything else.
_COVERAGE_WORDS = ("each", "every", "all", "whole", "entire")
_TARGET_WORDS = ("file", "files", "repo", "repository", "codebase", "project")

# Standalone phrases that imply "the whole thing" without needing a
# coverage+target word pair (e.g. "what does this do" style summaries).
_STANDALONE_BROAD_PHRASES = (
    "what does this project do",
    "what does this repo do",
    "what does this codebase do",
    "overview of the project",
    "overview of this project",
    "overview of the codebase",
    "summarize this repo",
    "summarize this project",
    "summarize the codebase",
)

# Phrases that signal a counting or enumerate-everything question about a
# *specific named thing* (a constant, list, function, etc.) rather than the
# whole repo. Distinct from the broad-question patterns above: "list all the
# files" is a repo-coverage question, but "list all the tickers" or "how
# many routes are there" is a symbol-scoped counting question -- if the
# named symbol is known, this takes priority over both hybrid and broad
# routing, since only exact retrieval can answer it correctly.
_COUNT_LIST_PATTERNS = (
    r"\bhow many\b",
    r"\bcount\b",
    r"\bnumber of\b",
    r"\blist all\b",
    r"\blist every\b",
    r"\bshow all\b",
    r"\benumerate\b",
    r"\bgive me all\b",
)

# Narrower than _COUNT_LIST_PATTERNS -- matches only questions asking for a
# count/total, not "list all"/"enumerate" style questions. Used to decide
# whether try_count_literal_entries() applies: a pure counting question can
# be answered with a single integer and never needs to see the chunk's full
# text, but a "list some" or "list all" question genuinely needs the LLM to
# read real entries, so it must go through the normal (now-truncated) LLM
# path instead.
_PURE_COUNTING_RE = re.compile(r"\bhow many\b|\bcount\b|\bnumber of\b")


def is_broad_question(query: str) -> bool:
    """
    True if the query looks like a whole-repo summarization request rather
    than a targeted lookup. Word-level matching (not exact-phrase substring
    matching) so word order and filler words in between don't matter --
    "each of the files", "every single file", and "all files" all match the
    same way.
    """
    q = query.lower()

    if any(phrase in q for phrase in _STANDALONE_BROAD_PHRASES):
        return True

    words = set(re.findall(r"[a-z']+", q))
    has_coverage_word = any(w in words for w in _COVERAGE_WORDS)
    has_target_word = any(w in words for w in _TARGET_WORDS)
    return has_coverage_word and has_target_word


def is_count_or_list_question(query: str) -> bool:
    """
    True if the query is asking to count or fully enumerate something
    ("how many tickers", "list all the routes"). This is a signal to look
    for a named symbol and, if found, retrieve it exactly -- see
    find_named_symbol() and generate_answer()'s routing.
    """
    q = query.lower()
    return any(re.search(p, q) for p in _COUNT_LIST_PATTERNS)


def is_pure_counting_question(query: str) -> bool:
    """
    True only for questions asking for a count/total ("how many", "count",
    "number of"), not for "list all"/"enumerate" style questions. See
    _PURE_COUNTING_RE's docstring-equivalent comment above for why this
    distinction matters.
    """
    return bool(_PURE_COUNTING_RE.search(query.lower()))


def find_named_symbol(query: str, repo_id: str) -> str | None:
    """
    Check whether the query names a known symbol (constant, function,
    class, etc.) in this repo. Case-insensitive substring match against
    the query text -- deliberately simple, since this only needs to catch
    "how many tickers are in COMMON_TICKERS" style questions naming an
    exact identifier, not fuzzy/semantic matching (hybrid search already
    handles the fuzzy case).

    Returns None if no known symbol is mentioned, in which case the caller
    falls back to normal hybrid/broad retrieval.
    """
    known_symbols = get_known_symbol_names(repo_id)
    q_lower = query.lower()
    # Prefer the longest matching symbol name, in case one symbol name is a
    # substring of another (e.g. "TICKERS" vs "COMMON_TICKERS") -- avoids
    # matching the shorter, less specific one first.
    matches = [s for s in known_symbols if s and s.lower() in q_lower]
    if not matches:
        return None
    return max(matches, key=len)


def try_count_literal_entries(chunks: list[RetrievedChunk]) -> int | None:
    """
    For exact-symbol count questions, attempt to parse the chunk content as
    a Python literal (dict/list/set/tuple) and return the exact entry count
    directly, bypassing the LLM entirely.

    Two independent reasons this matters, not just one: (1) LLMs are
    unreliable at precisely counting entries in a large text blob even when
    given the complete, untruncated content -- asking it to count is asking
    it to guess with extra steps; (2) large literals can exceed provider
    token-per-minute limits on their own (COMMON_TICKERS is ~17k tokens,
    Groq's on-demand tier caps at 8000/minute), so sending it whole for a
    question that only needs a single integer back is also just wasteful
    and fragile.

    Returns None if parsing fails (not a simple literal, spans multiple
    chunks, or doesn't parse standalone), in which case the caller falls
    back to the normal LLM-based path.
    """
    if len(chunks) != 1:
        return None  # only handle the common single-chunk case for now
    content = chunks[0].content.strip()
    if "=" not in content:
        return None
    rhs = content.split("=", 1)[1].strip()
    try:
        value = ast.literal_eval(rhs)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, (dict, list, set, tuple)):
        return len(value)
    return None


def _resolve_repo_id(repo_url: str = None, repo_id: str = None) -> str:
    """Shared resolution for the exact-symbol path, which needs a repo_id
    up front (get_known_symbol_names/retrieve_by_symbol take repo_id only,
    not repo_url) -- unlike retrieve_hybrid/retrieve_file_coverage, which
    accept either and resolve internally."""
    if repo_id:
        return repo_id
    if not repo_url:
        raise ValueError("Either repo_url or repo_id must be provided.")
    repo = get_repo_by_url(repo_url)
    if not repo:
        raise ValueError(f"Repo not ingested yet: {repo_url}")
    return str(repo["id"])


_groq_client = None  # lazy singleton


def get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY not set. Add it to a .env file in backend/ "
                "(see .env.example) or export it in your shell."
            )
        _groq_client = Groq(api_key=api_key)
    return _groq_client


SYSTEM_PROMPT = """You are a codebase assistant. You answer questions about a specific \
codebase using ONLY the code context provided below. Follow these rules:

- Base your answer strictly on the given context. Do not invent functions, files, \
or behavior that isn't shown.
- If the context doesn't contain enough information to answer confidently, say so \
plainly instead of guessing.
- When you reference code, cite the file path and line numbers exactly as given in \
the context (e.g. "in `auth.py:45-62`").
- Be concise and technical. Assume the reader is a developer already familiar with \
the language, so skip basic explanations unless asked.
"""

# Appended to the system prompt only for broad/whole-repo questions. The
# base prompt's "be concise" instruction is exactly right for targeted
# lookups but works against what these questions actually want: a real,
# per-function walkthrough of every file rather than a one-line gloss.
SYSTEM_PROMPT_BROAD_ADDENDUM = """
The user is asking for a detailed explanation covering the whole repo, not a quick \
summary. For each file in the context:
- Explain what each function/struct actually does step by step, referencing the \
real variable names, conditions, and calls shown in the code — not just a \
one-sentence paraphrase of its name.
- Note how it connects to other files where the context makes that clear (e.g. a \
struct defined in one file being populated in another).
- If a file's chunk is just a signature or declaration with no body shown, say so \
explicitly rather than skipping it silently.
Favor thoroughness over brevity for this answer.
"""

# Appended to the system prompt only for exact-symbol count/list questions
# where the chunk was sent complete (not truncated). The base prompt's "be
# concise" instruction can otherwise push the model toward summarizing or
# estimating -- exactly wrong for a question whose whole point is a precise
# count or a complete enumeration.
SYSTEM_PROMPT_EXACT_ADDENDUM = """
The user asked a counting or list-all question about a specific named symbol \
(a constant, list, function, etc.), and the context below is the complete, \
unabridged set of chunks for that symbol, in order, with nothing left out by \
similarity ranking. Do not estimate, round, or hedge:
- If asked "how many", count the actual entries shown and give the exact number.
- If asked to list something, enumerate every entry shown, not just a sample.
- If the entries span multiple chunks, treat them as one continuous list — don't \
report per-chunk sub-counts unless asked.
"""

# Appended instead of SYSTEM_PROMPT_EXACT_ADDENDUM when the exact-symbol
# chunk had to be truncated to fit within provider token limits. Critically
# different framing: the addendum above tells the model "this is complete,"
# which would make a truncated context actively misleading. This version
# tells the model the opposite -- explicitly not to claim completeness --
# so it describes only what's shown instead of confidently overstating it.
SYSTEM_PROMPT_EXACT_TRUNCATED_ADDENDUM = """
The user asked about a specific named symbol (a constant, list, function, etc.). \
The symbol's full content was too large to include in full, so the context below \
is a PARTIAL, TRUNCATED sample of real entries -- not the complete set:
- Do NOT state or imply a total count. If asked "how many", say the full content \
wasn't available to count exactly, and offer what you can see instead.
- If asked to list examples, list only real entries actually shown below -- never \
invent additional entries to fill out an answer.
- Be explicit that this is a partial sample, not the whole thing.
"""


def build_prompt(
    query: str,
    chunks: list[RetrievedChunk],
    is_broad: bool = False,
    is_exact_symbol: bool = False,
) -> tuple[str, bool]:
    """
    Assemble the retrieved chunks + question into a single user message.

    Returns (prompt_text, was_truncated) -- was_truncated is only ever True
    for the is_exact_symbol case, and tells the caller (generate_answer())
    which system-prompt addendum to use: SYSTEM_PROMPT_EXACT_ADDENDUM if the
    full content made it in, or SYSTEM_PROMPT_EXACT_TRUNCATED_ADDENDUM if it
    had to be cut down (see MAX_EXACT_SYMBOL_CHARS).
    """
    exact_symbol_was_truncated = False

    context_blocks = []
    for i, c in enumerate(chunks, start=1):
        content = c.content.strip()
        if is_exact_symbol:
            # Exact-symbol chunks skip the default per-chunk cap (they need
            # to stay as complete as possible for counting/enumeration), but
            # still need *some* ceiling -- Groq's on-demand tier caps
            # requests at 8000 tokens/minute, and a single large chunk (e.g.
            # a big constant dict) can exceed that on its own. Pure counting
            # questions never reach this function at all (short-circuited in
            # generate_answer() via try_count_literal_entries()), so any
            # exact-symbol chunk that does get here and needs truncation is
            # for a "list some"/"list all" style question, where a
            # representative sample is an honest answer as long as we say
            # so explicitly (see the truncated addendum above).
            if len(content) > MAX_EXACT_SYMBOL_CHARS:
                content = content[:MAX_EXACT_SYMBOL_CHARS] + "\n... (truncated)"
                exact_symbol_was_truncated = True
        elif len(content) > MAX_CONTEXT_CHARS_PER_CHUNK:
            content = content[:MAX_CONTEXT_CHARS_PER_CHUNK] + "\n... (truncated)"
        context_blocks.append(
            f"[{i}] {c.source_label()}\n```{c.language}\n{content}\n```"
        )

    context_text = "\n\n".join(context_blocks) if context_blocks else "(no relevant code found)"

    if is_exact_symbol and exact_symbol_was_truncated:
        note = (
            "Note: this symbol's full content is too large to include in full. "
            "What's shown below is a partial, truncated sample of real entries -- "
            "NOT the complete set. Do not state or imply a total count from this "
            "sample; only describe or list the entries actually shown.\n\n"
        )
    elif is_exact_symbol:
        note = (
            "Note: this context is the complete, unabridged set of chunks for the "
            "specific symbol named in the question -- nothing has been omitted by "
            "similarity ranking or top-k truncation. Count or enumerate directly "
            "from what's shown, in order.\n\n"
        )
    elif is_broad:
        note = (
            "Note: this context includes one representative chunk from every "
            "file in the repo (not a similarity-ranked selection), since the "
            "question asks about the whole project. Some files may only be "
            "represented by a short excerpt or signature — say so rather than "
            "inventing detail for parts you can't see.\n\n"
        )
    else:
        note = ""

    prompt = f"""{note}Context from the codebase:

{context_text}

Question: {query}
"""
    return prompt, exact_symbol_was_truncated


@dataclass
class RagAnswer:
    answer: str
    sources: list[dict]  # serializable source citations for the API/frontend


def _sources_from_chunks(chunks: list[RetrievedChunk]) -> list[dict]:
    """Shared chunk -> source-dict mapping, used by both the deterministic
    count path and the normal LLM path so source citations stay consistent
    regardless of which one answers the question."""
    return [
        {
            "file_path": c.file_path,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "symbol_name": c.symbol_name,
            "symbol_type": c.symbol_type,
            "label": c.source_label(),
        }
        for c in chunks
    ]


def generate_answer(
    query: str,
    repo_url: str = None,
    repo_id: str = None,
    top_k: int = TOP_K,
) -> RagAnswer:
    """
    Retrieve relevant context, build a prompt, and call the LLM (Groq)
    to answer the codebase query. Returns both the answer text and the
    source chunks used, so the caller can show citations.
    """
    broad = is_broad_question(query)
    exact_symbol = None

    if is_count_or_list_question(query):
        # Only resolve repo_id / hit the DB for known symbols if the
        # question actually looks like a counting/enumeration question --
        # no cost added to the common case.
        resolved_repo_id = _resolve_repo_id(repo_url, repo_id)
        exact_symbol = find_named_symbol(query, resolved_repo_id)

    if exact_symbol:
        # Exact-match routing: pull every chunk for this symbol instead of
        # relying on similarity ranking, which has no way to guarantee it
        # surfaces all N sub-chunks of a large constant/list. This makes
        # counting/enumeration questions correct by construction rather
        # than by hoping retrieval happens to return everything relevant.
        chunks = retrieve_by_symbol(exact_symbol, resolved_repo_id)
        broad = False  # exact-symbol framing takes over below, not broad framing

        # Pure counting questions ("how many X") get a deterministic answer
        # straight from parsing the source -- no LLM call, no truncation,
        # no token-limit risk, and no risk of the model miscounting a large
        # blob of text. Only short-circuits for genuine count questions;
        # "list some"/"list all" questions fall through to the normal LLM
        # path below since they need real entries read out, not just a
        # number.
        if is_pure_counting_question(query):
            literal_count = try_count_literal_entries(chunks)
            if literal_count is not None:
                return RagAnswer(
                    answer=f"There are exactly **{literal_count}** entries in `{exact_symbol}`.",
                    sources=_sources_from_chunks(chunks),
                )
    elif broad:
        # "Explain each file" / "overview of the project" style questions:
        # even hybrid search would only return the highest-scoring chunks
        # for an inherently vague query, silently omitting most files. Pull
        # one representative chunk per file instead, so every file gets at
        # least minimal coverage regardless of relevance scoring.
        chunks = retrieve_file_coverage(
            repo_url=repo_url,
            repo_id=repo_id,
        )
    else:
        # Hybrid (vector + full-text) instead of vector-only: catches exact
        # identifier matches (e.g. "getUserById") that pure semantic
        # similarity can bury under conceptually-similar-but-differently-
        # named chunks.
        chunks = retrieve_hybrid(
            query,
            repo_url=repo_url,
            repo_id=repo_id,
            top_k=top_k,
        )

    prompt, was_truncated = build_prompt(
        query, chunks, is_broad=broad, is_exact_symbol=bool(exact_symbol)
    )

    if exact_symbol and was_truncated:
        system_prompt = SYSTEM_PROMPT + SYSTEM_PROMPT_EXACT_TRUNCATED_ADDENDUM
        max_tokens = MAX_ANSWER_TOKENS_BROAD
    elif exact_symbol:
        system_prompt = SYSTEM_PROMPT + SYSTEM_PROMPT_EXACT_ADDENDUM
        max_tokens = MAX_ANSWER_TOKENS_BROAD  # enumerations can be long; give them the same room as broad answers
    elif broad:
        system_prompt = SYSTEM_PROMPT + SYSTEM_PROMPT_BROAD_ADDENDUM
        max_tokens = MAX_ANSWER_TOKENS_BROAD
    else:
        system_prompt = SYSTEM_PROMPT
        max_tokens = MAX_ANSWER_TOKENS

    client = get_groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,  # keep it grounded/factual, not creative
        max_tokens=max_tokens,
    )

    answer_text = response.choices[0].message.content
    finish_reason = response.choices[0].finish_reason

    if finish_reason == "length":
        answer_text += (
            "\n\n*(Note: this answer was cut off because it hit the response length "
            "limit — try asking about a narrower part of the code, or ask a follow-up "
            "to continue.)*"
        )

    return RagAnswer(answer=answer_text, sources=_sources_from_chunks(chunks))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python rag_chain.py <repo_url> <query>")
        sys.exit(1)

    repo_url, query = sys.argv[1], sys.argv[2]
    result = generate_answer(query, repo_url=repo_url)

    print("ANSWER:\n")
    print(result.answer)
    print("\nSOURCES:")
    for s in result.sources:
        print(" -", s["label"])