"""CLI: build the portfolio knowledge base and persist it to JSON.

Runs `ingest_portfolio`, embeds each chunk with the offline FakeEmbedder, saves
the vector store to `settings.rag_store_path`, and prints a summary. No external
services and no API key required.

Usage:
    python -m scripts.build_kb [--repos PATH] [--out PATH] [--real]
"""
from __future__ import annotations

import argparse
from collections import Counter

from config import get_settings
from rag.embedder import get_embedder
from rag.retriever import build_store


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build the portfolio RAG knowledge base.")
    parser.add_argument("--repos", default=settings.portfolio_repos_path, help="Portfolio repos dir")
    parser.add_argument("--out", default=settings.rag_store_path, help="Output JSON store path")
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use the real SentenceTransformer embedder (requires the [prod] extra).",
    )
    args = parser.parse_args()

    embedder = get_embedder(use_real=args.real)
    store = build_store(args.repos, embedder)
    store.save(args.out)

    kinds = Counter(d.get("metadata", {}).get("kind", "?") for d in store.docs)
    sources = sorted({d.get("metadata", {}).get("source", "?") for d in store.docs})

    print("Portfolio knowledge base built.")
    print(f"  embedder : {type(embedder).__name__} (dim={embedder.dim})")
    print(f"  repos    : {args.repos}")
    print(f"  output   : {args.out}")
    print(f"  chunks   : {len(store)}")
    print(f"  by kind  : {dict(kinds)}")
    print(f"  sources  : {len(sources)} -> {', '.join(sources)}")


if __name__ == "__main__":
    main()
