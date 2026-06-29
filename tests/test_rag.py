"""Offline tests for the portfolio RAG knowledge base.

Everything here runs with NO external services and NO API key: the FakeEmbedder
is deterministic and the InMemoryVectorStore persists to plain JSON.
"""
from __future__ import annotations

import math

import pytest

from rag.embedder import FakeEmbedder
from rag.ingest import ingest_portfolio
from rag.retriever import Retriever, build_store
from rag.store import InMemoryVectorStore

SAMPLE_README = """# Sample Kubernetes Security Project

[![CI](https://img.shields.io/badge/CI-passing-green)](https://example.com)

## Overview

This project hardens Kubernetes clusters with OPA Gatekeeper policy-as-code,
Trivy image scanning, and Istio mTLS between every service. It shifts security
left so insecure builds never reach a cluster.

## The Problem It Solves

Teams bolt security on after shipping, so vulnerabilities and leaked secrets are
found in production. This blocks CRITICAL CVEs at the gate and enforces
least-privilege admission control.

## Tech Stack

Some other section that should be ignored by the ingest.
"""

UNRELATED_README = """# Static Marketing Website

## Overview

A small static brochure site built with plain HTML and CSS. It serves landing
pages and a contact form. No backend, no containers, no clusters here.
"""


def _make_repo(root, name: str, readme: str | None) -> None:
    repo = root / name
    repo.mkdir()
    if readme is not None:
        (repo / "README.md").write_text(readme, encoding="utf-8")


# --- ingest / chunker -------------------------------------------------------


def test_ingest_extracts_sections_and_is_robust(tmp_path):
    _make_repo(tmp_path, "k8s-security", SAMPLE_README)
    _make_repo(tmp_path, "marketing-site", UNRELATED_README)
    _make_repo(tmp_path, "no-readme-repo", None)  # robustness: missing README

    docs = ingest_portfolio(str(tmp_path))

    sources = {d["source"] for d in docs}
    assert "k8s-security" in sources
    assert "marketing-site" in sources
    assert "no-readme-repo" not in sources  # gracefully skipped

    # achievements.md is always ingested.
    assert any(d["kind"] == "achievement" and d["source"] == "achievements" for d in docs)

    k8s_text = "\n".join(d["text"] for d in docs if d["source"] == "k8s-security")
    assert "Sample Kubernetes Security Project" in k8s_text
    assert "OPA Gatekeeper" in k8s_text
    assert "least-privilege admission control" in k8s_text
    # The "Tech Stack" section must NOT be included.
    assert "should be ignored" not in k8s_text


def test_ingest_missing_repos_dir_returns_only_achievements(tmp_path):
    docs = ingest_portfolio(str(tmp_path / "does-not-exist"))
    assert docs  # achievements still present
    assert all(d["kind"] == "achievement" for d in docs)


def test_chunks_have_required_shape(tmp_path):
    _make_repo(tmp_path, "k8s-security", SAMPLE_README)
    docs = ingest_portfolio(str(tmp_path))
    for d in docs:
        assert set(d.keys()) == {"text", "source", "kind"}
        assert isinstance(d["text"], str) and d["text"].strip()


# --- FakeEmbedder -----------------------------------------------------------


def test_fake_embedder_is_deterministic_and_normalized():
    emb = FakeEmbedder(dim=128)
    a = emb.embed("kubernetes security hardening")
    b = emb.embed("kubernetes security hardening")
    assert a == b  # deterministic
    assert len(a) == 128
    norm = math.sqrt(sum(x * x for x in a))
    assert norm == pytest.approx(1.0, abs=1e-9)

    # Different text -> different vector.
    c = emb.embed("static marketing website")
    assert a != c

    # Empty text -> zero vector (stays length dim, norm 0).
    z = emb.embed("")
    assert len(z) == 128
    assert all(v == 0.0 for v in z)


# --- InMemoryVectorStore ----------------------------------------------------


def test_store_ranks_relevant_doc_first():
    emb = FakeEmbedder(dim=256)
    docs = [
        {
            "text": "Kubernetes security with OPA Gatekeeper, Trivy scanning and Istio mTLS.",
            "metadata": {"source": "k8s-security", "kind": "portfolio"},
            "vector": emb.embed(
                "Kubernetes security with OPA Gatekeeper, Trivy scanning and Istio mTLS."
            ),
        },
        {
            "text": "A static marketing website built with plain HTML and CSS, no backend.",
            "metadata": {"source": "marketing-site", "kind": "portfolio"},
            "vector": emb.embed(
                "A static marketing website built with plain HTML and CSS, no backend."
            ),
        },
    ]
    store = InMemoryVectorStore(dim=emb.dim)
    store.add(docs)

    results = store.search(emb.embed("kubernetes security scanning mTLS"), k=2)
    assert len(results) == 2
    top_doc, top_score = results[0]
    assert top_doc["metadata"]["source"] == "k8s-security"
    assert top_score >= results[1][1]


def test_store_save_load_roundtrip(tmp_path):
    emb = FakeEmbedder(dim=64)
    docs = [
        {
            "text": "GitOps with ArgoCD and Terraform on AKS.",
            "metadata": {"source": "gitops", "kind": "portfolio"},
            "vector": emb.embed("GitOps with ArgoCD and Terraform on AKS."),
        }
    ]
    store = InMemoryVectorStore(dim=emb.dim)
    store.add(docs)

    out = tmp_path / "kb.json"
    store.save(str(out))
    assert out.is_file()

    loaded = InMemoryVectorStore.from_file(str(out))
    assert len(loaded) == 1
    assert loaded.dim == 64
    q = emb.embed("ArgoCD GitOps")
    orig = store.search(q, k=1)[0][1]
    rt = loaded.search(q, k=1)[0][1]
    assert orig == pytest.approx(rt, abs=1e-12)


# --- Retriever (end to end) -------------------------------------------------


def test_retriever_returns_expected_source_for_kubernetes_security(tmp_path):
    _make_repo(tmp_path, "k8s-security", SAMPLE_README)
    _make_repo(tmp_path, "marketing-site", UNRELATED_README)

    emb = FakeEmbedder()
    store = build_store(str(tmp_path), emb)
    retriever = Retriever(store, emb)

    results = retriever.retrieve("kubernetes security OPA Trivy mTLS", k=3)
    assert results
    assert set(results[0].keys()) >= {"text", "source", "score"}
    assert results[0]["source"] == "k8s-security"
    # scores are sorted descending
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
