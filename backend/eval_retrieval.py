# eval_retrieval.py
# Retrieval evaluation harness: measures whether the "right" chunk actually
# shows up in the top-k results for a hand-labeled set of (question,
# expected file/symbol) pairs, per repo.
#
# This is the check most student RAG demos skip — they eyeball the sources
# panel a few times and call it done. This script instead runs a fixed
# dataset through the real retriever and reports hit@k / MRR, so retrieval
# quality is something you can actually measure and show numbers for.
#
# --- Usage --------------------------------------------------------------
#
#   1. Ingest every repo referenced in your dataset first, e.g.:
#        python ingest.py https://github.com/pallets/click
#
#   2. Run the eval:
#        python eval_retrieval.py eval/eval_dataset.example.json
#
#   Optional flags:
#     -v / --verbose      show retrieved chunks for hits too, not just misses
#     --fail-under 0.8     exit non-zero if hit rate falls below 80% (for CI)
#
# --- Dataset format (JSON, list of entries) ------------------------------
#
#   [
#     {
#       "repo_url": "https://github.com/pallets/click",
#       "question": "What decorator adds a --flag option to a click command?",
#       "expected_file": "click/decorators.py",   # substring match on file_path
#       "expected_symbol": "option",               # optional, substring on symbol_name
#       "top_k": 5                                  # optional, defaults to DEFAULT_TOP_K
#     },
#     ...
#   ]
#
# A hit means: at least one of the top-k retrieved chunks has a file_path
# containing expected_file AND (if given) a symbol_name containing
# expected_symbol. expected_symbol is optional but recommended — matching on
# file alone is a much weaker signal for large files with many functions.

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Optional

from retriever import retrieve_context

DEFAULT_TOP_K = 5


@dataclass
class EvalEntry:
    repo_url: str
    question: str
    expected_file: str
    expected_symbol: Optional[str] = None
    top_k: int = DEFAULT_TOP_K


@dataclass
class EvalResult:
    entry: EvalEntry
    hit: bool
    rank: Optional[int]   # 1-indexed rank of the first matching chunk, None if no hit
    retrieved: list        # source labels actually retrieved, for debugging output


def _matches(chunk, entry: EvalEntry) -> bool:
    if entry.expected_file not in chunk.file_path:
        return False
    if entry.expected_symbol and entry.expected_symbol not in chunk.symbol_name:
        return False
    return True


def load_dataset(path: str) -> list[EvalEntry]:
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError("Dataset file must contain a JSON list of entries.")

    entries = []
    for i, row in enumerate(raw):
        missing = [k for k in ("repo_url", "question", "expected_file") if k not in row]
        if missing:
            raise ValueError(f"Entry {i} is missing required field(s): {missing}")
        entries.append(EvalEntry(
            repo_url=row["repo_url"],
            question=row["question"],
            expected_file=row["expected_file"],
            expected_symbol=row.get("expected_symbol"),
            top_k=row.get("top_k", DEFAULT_TOP_K),
        ))
    return entries


def evaluate_entry(entry: EvalEntry) -> EvalResult:
    try:
        chunks = retrieve_context(entry.question, repo_url=entry.repo_url, top_k=entry.top_k)
    except ValueError as e:
        # Repo hasn't been ingested yet — surface as a clear, labeled miss
        # rather than crashing the whole eval run partway through.
        return EvalResult(entry=entry, hit=False, rank=None, retrieved=[f"<error: {e}>"])

    rank = None
    for i, c in enumerate(chunks, start=1):
        if _matches(c, entry):
            rank = i
            break

    return EvalResult(
        entry=entry,
        hit=rank is not None,
        rank=rank,
        retrieved=[c.source_label() for c in chunks],
    )


def run_eval(entries: list[EvalEntry], verbose: bool = False) -> list[EvalResult]:
    results = []
    for entry in entries:
        result = evaluate_entry(entry)
        results.append(result)

        status = f"HIT  (rank {result.rank})" if result.hit else "MISS"
        expected = entry.expected_file
        if entry.expected_symbol:
            expected += f" / {entry.expected_symbol}"
        print(f"[{status}] {entry.question!r}  (expected: {expected})")

        if verbose or not result.hit:
            for j, label in enumerate(result.retrieved, start=1):
                marker = " <-- matched" if result.hit and j == result.rank else ""
                print(f"    [{j}] {label}{marker}")
    return results


def print_summary(results: list[EvalResult]) -> None:
    if not results:
        print("\nNo eval entries to report on.")
        return

    total = len(results)
    hits = sum(1 for r in results if r.hit)
    hit_rate = hits / total
    mrr = sum((1.0 / r.rank if r.hit else 0.0) for r in results) / total

    by_repo: dict = {}
    for r in results:
        by_repo.setdefault(r.entry.repo_url, []).append(r)

    print("\n" + "=" * 60)
    print("RETRIEVAL EVAL SUMMARY")
    print("=" * 60)
    print(f"Hit@k:  {hits}/{total}  ({hit_rate:.1%})")
    print(f"MRR:    {mrr:.3f}")

    print("\nBy repo:")
    for repo_url, repo_results in by_repo.items():
        repo_hits = sum(1 for r in repo_results if r.hit)
        print(f"  {repo_url}: {repo_hits}/{len(repo_results)} ({repo_hits / len(repo_results):.1%})")

    misses = [r for r in results if not r.hit]
    if misses:
        print(f"\n{len(misses)} miss(es):")
        for r in misses:
            expected = r.entry.expected_file
            if r.entry.expected_symbol:
                expected += f" / {r.entry.expected_symbol}"
            print(f"  - {r.entry.question!r}  (expected {expected})")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval quality against a hand-labeled dataset of "
                    "(question, expected file/symbol) pairs. Ingest the referenced "
                    "repo(s) first."
    )
    parser.add_argument("dataset", help="Path to a JSON eval dataset, e.g. eval/eval_dataset.example.json")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Print retrieved chunks for hits too, not just misses")
    parser.add_argument("--fail-under", type=float, default=None,
                         help="Exit non-zero if hit rate falls below this fraction "
                              "(e.g. 0.8). Useful for wiring this into CI.")
    args = parser.parse_args()

    entries = load_dataset(args.dataset)
    if not entries:
        print("Dataset is empty.")
        sys.exit(1)

    results = run_eval(entries, verbose=args.verbose)
    print_summary(results)

    if args.fail_under is not None:
        hit_rate = sum(1 for r in results if r.hit) / len(results)
        if hit_rate < args.fail_under:
            print(f"\nFAIL: hit rate {hit_rate:.1%} is below threshold {args.fail_under:.1%}")
            sys.exit(1)


if __name__ == "__main__":
    main()
