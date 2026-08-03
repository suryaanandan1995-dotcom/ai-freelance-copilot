"""Offline tests for keyword targeting (sources/_keywords.py).

Two things are pinned here, both from live measurement on 2026-08-03:

1. **AI-infra terms must match.** The original list had no LLM/RAG/agent/MCP terms,
   so the pipeline systematically discarded the one segment that is growing
   (LLM contract vacancies +247% YoY, £550/day median) and that this portfolio is
   differentiated in.
2. **Bare "agent" must NOT match.** Sampling live listings showed it hitting
   "Customer Service Agent", "Helpdesk Support Agent" and "Insurance Agent" — the
   false-positive class that spends Opus budget scoring call-centre jobs.
"""
from __future__ import annotations

import pytest

from sources._keywords import (
    STRONG_TITLE_KEYWORDS,
    extract_tags,
    is_ai_infra,
    matches_keywords,
    title_signal,
)


# --------------------------------------------------------------------------- #
# the growth segment must match
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "LLM Infrastructure Engineer",
        "Senior LLMOps engineer, contract",
        "Build a RAG pipeline over our docs",
        "Generative AI platform engineer",
        "GenAI applications lead",
        "Agentic workflow developer",
        "AI Agent Engineer (contract)",
        "Multi-agent orchestration specialist",
        "MCP server integration work",
        "Model Context Protocol tooling",
        "MLOps contractor needed",
        "vLLM inference optimisation",
        "Vector database migration to pgvector",
        "AI security and guardrails review",
        "Large language model fine-tuning",
    ],
)
def test_ai_infra_listings_match(text):
    assert matches_keywords(text) is True


# --------------------------------------------------------------------------- #
# the measured false positives must not match
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Customer Service Agent",
        "Helpdesk Support Agent",
        "Insurance Agent — commission only",
        "Estate Agent, central London",
        "Booking agent for touring artists",
    ],
)
def test_human_agent_roles_do_not_match(text):
    """Bare "agent" is deliberately absent from KEYWORDS for exactly these."""
    assert matches_keywords(text) is False
    assert is_ai_infra(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Forward Deployed Engineer",
        "Forward-deployed software engineer, AI products",
        "FDE — customer-facing delivery",
        "Solutions Engineer (AI platform)",
        "Implementation Engineer, enterprise deployments",
        "Deployment Engineer for our LLM product",
        "Customer Engineer — integrations",
        "Integration Engineer, contract",
    ],
)
def test_fde_listings_match(text):
    """FDE is a distinct, well-paid title at AI companies and maps to contract
    delivery work; the original keyword list had none of these terms."""
    from sources._keywords import is_fde

    assert matches_keywords(text) is True
    assert is_fde(text) is True


def test_fde_and_ai_infra_are_separate_segments():
    """They need different pitches, so the two predicates must not collapse."""
    from sources._keywords import is_fde

    assert is_fde("Forward Deployed Engineer") is True
    assert is_ai_infra("Forward Deployed Engineer") is False
    assert is_ai_infra("LLM inference platform") is True
    assert is_fde("LLM inference platform") is False
    # A role can be both — that is the bullseye for this portfolio.
    both = "Forward Deployed Engineer — deploy our RAG platform at customer sites"
    assert is_fde(both) and is_ai_infra(both)


def test_agent_framework_terms_match():
    assert matches_keywords("LangGraph agent orchestration contract") is True
    assert matches_keywords("LangChain tool calling and evals") is True
    assert is_ai_infra("LangGraph agent orchestration") is True


def test_fde_predicate_handles_empty():
    from sources._keywords import is_fde

    assert is_fde("") is False
    assert is_fde(None) is False


def test_generic_words_alone_do_not_match():
    assert matches_keywords("Remote-first company with a cloud-native culture") is False
    assert matches_keywords("") is False
    assert matches_keywords(None) is False


def test_short_tokens_respect_word_boundaries():
    """"sre" must not match inside "stores", "aws" not inside "flaws"."""
    assert matches_keywords("We have many stores and few flaws") is False
    assert matches_keywords("SRE on call") is True
    assert matches_keywords("Deep AWS experience") is True


# --------------------------------------------------------------------------- #
# base segment still matches (no regression from the AI additions)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Kubernetes platform engineer",
        "DevSecOps contractor",
        "Site Reliability Engineer",
        "Terraform + ArgoCD migration",
        "CI/CD hardening on EKS",
    ],
)
def test_base_devops_listings_still_match(text):
    assert matches_keywords(text) is True


# --------------------------------------------------------------------------- #
# is_ai_infra separates the two segments
# --------------------------------------------------------------------------- #
def test_is_ai_infra_distinguishes_segments():
    assert is_ai_infra("LLM serving on Kubernetes") is True
    assert is_ai_infra("Plain Kubernetes upgrade work") is False
    # Both are relevant leads; only one is the premium segment.
    assert matches_keywords("Plain Kubernetes upgrade work") is True


def test_is_ai_infra_handles_empty():
    assert is_ai_infra("") is False
    assert is_ai_infra(None, None) is False


# --------------------------------------------------------------------------- #
# title_signal — prioritisation before spending LLM budget
# --------------------------------------------------------------------------- #
def test_title_signal_finds_strong_terms():
    assert set(title_signal("LLM Infrastructure Engineer")) == {
        "llm",
        "infrastructure engineer",
    }
    assert title_signal("AI Agent Engineer") == ["ai agent"]


def test_title_signal_ignores_body_only_boilerplate():
    """A title hit predicts fit; "we use Kubernetes" in a JD does not."""
    assert title_signal("Frontend Developer") == []
    assert matches_keywords("Frontend Developer", "Our stack uses Kubernetes") is True


def test_title_signal_empty_title():
    assert title_signal("") == []
    assert title_signal(None) == []


def test_strong_title_keywords_are_a_subset_of_keywords():
    """A typo in STRONG_TITLE_KEYWORDS would silently never match anything."""
    from sources._keywords import KEYWORDS

    assert STRONG_TITLE_KEYWORDS <= set(KEYWORDS)


def test_strong_title_keywords_exclude_ambiguous_terms():
    """Guard the fix: re-adding these would resurrect the call-centre matches."""
    assert "agent" not in STRONG_TITLE_KEYWORDS
    assert "inference" not in STRONG_TITLE_KEYWORDS


# --------------------------------------------------------------------------- #
# extract_tags
# --------------------------------------------------------------------------- #
def test_extract_tags_dedupes_and_orders_by_keyword_list():
    tags = extract_tags("LLM RAG platform", "We serve LLM traffic on Kubernetes")
    assert tags[0] == "llm"  # AI terms lead the KEYWORDS tuple
    assert tags.count("llm") == 1
    assert "kubernetes" in tags


def test_extract_tags_empty():
    assert extract_tags("") == []
    assert extract_tags(None) == []
