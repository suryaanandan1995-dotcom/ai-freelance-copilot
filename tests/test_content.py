"""Offline tests for the inbound content engine.

A fake retriever supplies deterministic proof points and ``FakeChat`` supplies
the generated body, so no network or API key is required.
"""
from __future__ import annotations

import pytest

from agents.llm import FakeChat
from content.engine import generate


class FakeRetriever:
    def __init__(self, docs):
        self._docs = docs

    def retrieve(self, query, k=5):
        return list(self._docs[:k])


def _proof():
    return [
        {
            "text": "Hardened EKS clusters with OPA Gatekeeper and Trivy scanning.",
            "source": "multi-cloud-k8s-terraform",
            "kind": "readme",
            "score": 0.91,
        },
        {
            "text": "GitOps CI/CD that cut deploy time in half.",
            "source": "gitops-platform",
            "kind": "readme",
            "score": 0.82,
        },
    ]


def test_post_generates_draft():
    chat = FakeChat(responses=["A LinkedIn post mentioning multi-cloud-k8s-terraform."])
    result = generate(
        "post", topic="kubernetes security", retriever=FakeRetriever(_proof()), chat=chat
    )
    assert set(result) == {"kind", "title", "body", "sources"}
    assert result["kind"] == "post"
    assert result["body"]
    assert "multi-cloud-k8s-terraform" in result["body"]
    assert result["sources"] == ["multi-cloud-k8s-terraform", "gitops-platform"]


def test_case_study_kind_normalizes():
    chat = FakeChat(responses=["# Case Study\n## Problem ...\n## Solution ...\n## Result ..."])
    result = generate("case-study", retriever=FakeRetriever(_proof()), chat=chat)
    assert result["kind"] == "case_study"
    assert result["title"] == "Case Study"
    assert result["body"]
    assert result["sources"]


def test_gig_generates_draft():
    chat = FakeChat(responses=["Gig: I will harden your Kubernetes cluster."])
    result = generate("gig", topic="devsecops", retriever=FakeRetriever(_proof()), chat=chat)
    assert result["kind"] == "gig"
    assert result["body"]
    assert len(result["sources"]) == 2


def test_post_with_no_proof():
    chat = FakeChat(responses=["A post with no retrieved proof."])
    result = generate("post", retriever=FakeRetriever([]), chat=chat)
    assert result["body"]
    assert result["sources"] == []


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        generate("tweet", retriever=FakeRetriever(_proof()), chat=FakeChat())
