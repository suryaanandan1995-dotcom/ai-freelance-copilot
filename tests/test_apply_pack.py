"""Offline tests for the apply-yourself pack builder (no network, no real model).

The subject under test is the ONLY route the 262 qualified-but-uncontactable leads of
the 2026-08-10..17 window have (``auto_submit`` is permanently off and the dashboard
is not hosted), so the invariants asserted here are the ones that make it a hand-off
rather than a list of links: a cap that cuts the worst leads, one model call per pack,
a pack for every promised lead even when the model fails, and no URL that a prospect
could click into a 404.

A model is never constructed: ``chat`` is injected, and ``conftest.py`` pins
``COPILOT_ANTHROPIC_API_KEY`` to "" so the ``chat=None`` path cannot reach the network
either.
"""
from __future__ import annotations

import json

from outreach.apply_pack import (
    MAX_BULLETS,
    MAX_NOTE_WORDS,
    MIN_BULLETS,
    ApplyPack,
    build_apply_packs,
)

REPO_URL = "https://github.com/suryaanandan1995-dotcom/devsecops-cicd-pipeline"
HALLUCINATED_URL = "https://github.com/suryaanandan1995-dotcom/devsecops-pipeline-templates"


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class RecordingChat:
    """Fake chat matching the ``.invoke(messages) -> obj.content`` shape pitch.py uses.

    ``replies`` items may be dicts (JSON-encoded for the caller), strings, or Exception
    instances (raised, to simulate a failed draft).
    """

    def __init__(self, replies: list | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list = []

    def invoke(self, messages):  # noqa: ANN001 - mirrors the langchain signature
        self.calls.append(messages)
        if not self.replies:
            return _Msg("{}")
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, (dict, list)):
            return _Msg(json.dumps(reply))
        return _Msg(str(reply))


class FakeRetriever:
    def __init__(self, docs: list[dict] | None = None, boom: bool = False) -> None:
        self.docs = docs if docs is not None else [
            {
                "text": "devsecops-cicd-pipeline hardens GitHub Actions with SBOM + signing.",
                "source": "devsecops-cicd-pipeline",
                "kind": "repo",
                "score": 0.9,
            },
            {
                "text": "multi-cloud-k8s-terraform cut infra cost 40% across EKS and GKE.",
                "source": "multi-cloud-k8s-terraform",
                "kind": "win",
                "score": 0.8,
            },
        ]
        self.boom = boom
        self.queries: list[str] = []

    def retrieve(self, query, k=3):  # noqa: ANN001
        self.queries.append(query)
        if self.boom:
            raise RuntimeError("vector store unavailable")
        return self.docs[:k]


def _lead(score: int, i: int = 0, **over) -> dict:
    lead = {
        "title": f"Kubernetes + DevSecOps hardening #{i}",
        "url": f"https://jobs.example.com/listing/{i}",
        "company": f"Acme {i}",
        "source": "working_nomads",
        "fit_score": score,
    }
    lead.update(over)
    return lead


def _reply(note: str | None = None, links: list[str] | None = None, bullets=None) -> dict:
    return {
        "why_you_fit": bullets
        if bullets is not None
        else [
            "Shipped devsecops-cicd-pipeline: SBOM generation and image signing in CI.",
            "multi-cloud-k8s-terraform runs the same workload on EKS and GKE.",
        ],
        "cover_note": note
        or (
            "Hi Acme team, I saw your Kubernetes hardening listing and it is close to "
            "what I do every week: locking down cluster RBAC, wiring supply-chain checks "
            "into CI, and leaving the team with something they can run themselves. I have "
            "done exactly that on two public projects, both readable end to end, so you "
            "can judge the work before we speak. Happy to talk through scope on a short "
            "call whenever suits you."
        ),
        "proof_links": links if links is not None else [REPO_URL],
    }


# --- cap + selection -----------------------------------------------------------------


def test_the_cap_is_respected_and_keeps_only_the_highest_scoring_leads():
    leads = [_lead(70, 1), _lead(95, 2), _lead(82, 3), _lead(71, 4)]
    chat = RecordingChat([_reply()])

    packs = build_apply_packs(leads, retriever=FakeRetriever(), chat=chat, limit=2)

    assert [p.fit_score for p in packs] == [95, 82]


def test_the_cap_cuts_the_worst_leads_even_when_the_caller_hands_over_an_unsorted_list():
    leads = [_lead(60, 1), _lead(99, 2), _lead(61, 3)]

    packs = build_apply_packs(leads, retriever=FakeRetriever(), chat=RecordingChat([_reply()]), limit=1)

    assert [p.fit_score for p in packs] == [99]


def test_the_limit_defaults_to_max_apply_packs_per_run(monkeypatch):
    monkeypatch.setenv("COPILOT_MAX_APPLY_PACKS_PER_RUN", "3")
    leads = [_lead(90 - i, i) for i in range(8)]

    packs = build_apply_packs(leads, retriever=FakeRetriever(), chat=RecordingChat([_reply()]))

    assert len(packs) == 3


def test_an_empty_lead_list_returns_no_packs():
    chat = RecordingChat([_reply()])

    assert build_apply_packs([], retriever=FakeRetriever(), chat=chat) == []
    assert chat.calls == []


def test_a_limit_of_zero_returns_no_packs_and_spends_nothing():
    chat = RecordingChat([_reply()])

    assert build_apply_packs([_lead(99, 1)], retriever=FakeRetriever(), chat=chat, limit=0) == []
    assert chat.calls == []


# --- cost ----------------------------------------------------------------------------


def test_exactly_one_model_call_is_made_per_pack():
    leads = [_lead(90, 1), _lead(89, 2), _lead(88, 3)]
    chat = RecordingChat([_reply()])

    packs = build_apply_packs(leads, retriever=FakeRetriever(), chat=chat, limit=3)

    assert len(packs) == 3
    assert len(chat.calls) == 3


def test_leads_beyond_the_cap_cost_no_model_call_at_all():
    leads = [_lead(90 - i, i) for i in range(10)]
    chat = RecordingChat([_reply()])

    build_apply_packs(leads, retriever=FakeRetriever(), chat=chat, limit=2)

    assert len(chat.calls) == 2


# --- fallbacks: a promised lead always comes back with a pack ------------------------


def test_a_run_without_any_chat_model_still_returns_template_packs():
    leads = [_lead(88, 1), _lead(75, 2)]
    retriever = FakeRetriever()

    packs = build_apply_packs(leads, retriever=retriever, chat=None, limit=2)

    assert len(packs) == 2
    for pack in packs:
        assert pack.cover_note.strip()
        assert MIN_BULLETS <= len(pack.why_you_fit) <= MAX_BULLETS
        assert pack.proof_links


def test_a_model_failure_on_the_second_of_three_leads_still_returns_three_packs():
    leads = [_lead(90, 1), _lead(89, 2), _lead(88, 3)]
    chat = RecordingChat([_reply(), RuntimeError("overloaded_error"), _reply()])

    packs = build_apply_packs(leads, retriever=FakeRetriever(), chat=chat, limit=3)

    assert len(packs) == 3
    assert len(chat.calls) == 3  # the failure is not retried
    assert all(p.cover_note.strip() for p in packs)
    assert packs[1].lead_url == "https://jobs.example.com/listing/2"


def test_unparseable_model_output_falls_back_to_a_template_pack_without_raising():
    chat = RecordingChat(["I'm sorry, I can't help with that."])

    packs = build_apply_packs([_lead(80, 1)], retriever=FakeRetriever(), chat=chat, limit=1)

    assert len(packs) == 1
    assert "freelance DevSecOps" in packs[0].cover_note


def test_a_budget_exhausted_model_stops_further_calls_but_still_returns_every_pack():
    from costs import BudgetExhausted

    leads = [_lead(90, 1), _lead(89, 2), _lead(88, 3)]
    chat = RecordingChat([BudgetExhausted("cap reached")])

    packs = build_apply_packs(leads, retriever=FakeRetriever(), chat=chat, limit=3)

    assert len(packs) == 3
    assert len(chat.calls) == 1  # the model is abandoned after the cap is hit


def test_a_retriever_failure_does_not_lose_the_pack():
    packs = build_apply_packs(
        [_lead(80, 1)], retriever=FakeRetriever(boom=True), chat=None, limit=1
    )

    assert len(packs) == 1
    assert packs[0].proof_links  # settings-derived links still available


def test_a_missing_retriever_is_fetched_lazily_and_its_docs_are_used(monkeypatch):
    import rag.retriever as rr

    retriever = FakeRetriever()
    monkeypatch.setattr(rr, "get_retriever", lambda: retriever)

    packs = build_apply_packs([_lead(80, 1)], chat=None, limit=1)

    assert retriever.queries and len(packs) == 1


def test_a_lead_with_no_company_or_title_still_produces_a_pasteable_pack():
    lead = _lead(72, 9, company="", title="")

    packs = build_apply_packs([lead], retriever=FakeRetriever(), chat=None, limit=1)

    assert len(packs) == 1
    assert packs[0].cover_note.startswith("Hi,")
    assert "(untitled listing)" in packs[0].to_html()


# --- proof links: never invent one ---------------------------------------------------


def test_a_hallucinated_proof_url_is_dropped_while_the_retriever_supplied_one_survives():
    chat = RecordingChat([_reply(links=[REPO_URL, HALLUCINATED_URL])])

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=chat, limit=1)

    assert packs[0].proof_links == [REPO_URL]
    assert HALLUCINATED_URL not in packs[0].to_text()


def test_a_hallucinated_url_inside_the_cover_note_prose_is_scrubbed():
    note = (
        "Hi Acme team, your Kubernetes hardening listing lines up with work I have "
        f"already shipped and published. The pipeline templates live at {HALLUCINATED_URL} "
        "and cover SBOM generation, image signing and policy gates, so you can read the "
        "whole thing before we speak. I would rather show working code than describe it. "
        "Happy to talk scope on a short call whenever suits you."
    )
    chat = RecordingChat([_reply(note=note)])

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=chat, limit=1)

    assert HALLUCINATED_URL not in packs[0].cover_note
    assert "github.com/suryaanandan1995-dotcom/devsecops-pipeline-templates" not in packs[0].to_html()


def test_a_url_the_model_invents_in_a_bullet_is_dropped_too():
    chat = RecordingChat(
        [
            _reply(
                bullets=[
                    f"Shipped supply-chain hardening: {HALLUCINATED_URL}",
                    "multi-cloud-k8s-terraform runs the same workload on EKS and GKE.",
                ]
            )
        ]
    )

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=chat, limit=1)

    assert not any(HALLUCINATED_URL in b for b in packs[0].why_you_fit)


def test_settings_owner_links_are_allowed_proof_links():
    from config import get_settings

    settings = get_settings()
    chat = RecordingChat([_reply(links=[settings.owner_site, settings.owner_linkedin])])

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=chat, limit=1)

    assert packs[0].proof_links == [settings.owner_site, settings.owner_linkedin]


def test_a_pack_whose_model_cited_only_invented_links_still_ships_real_ones():
    chat = RecordingChat([_reply(links=[HALLUCINATED_URL, "https://example.com/nope"])])

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=chat, limit=1)

    assert packs[0].proof_links
    assert all(HALLUCINATED_URL != link for link in packs[0].proof_links)


# --- rendering -----------------------------------------------------------------------


def test_to_text_leads_with_the_listing_url_because_that_link_is_the_whole_handoff():
    packs = build_apply_packs(
        [_lead(90, 7)], retriever=FakeRetriever(), chat=RecordingChat([_reply()]), limit=1
    )
    text = packs[0].to_text()

    assert text.splitlines()[0] == "https://jobs.example.com/listing/7"
    assert "https://jobs.example.com/listing/7" in text


def test_to_text_contains_the_bullets_the_rate_and_the_pasteable_note():
    packs = build_apply_packs(
        [_lead(90, 1)], retriever=FakeRetriever(), chat=RecordingChat([_reply()]), limit=1
    )
    text = packs[0].to_text()

    assert "WHY YOU FIT" in text
    assert "RATE:" in text
    assert "PASTE INTO THE APPLY FORM" in text


def test_to_html_escapes_a_scripted_title_and_a_quoted_company():
    lead = _lead(
        90,
        1,
        title="<script>alert('xss')</script> K8s engineer",
        company='Ac"me" <b>Ltd</b>',
    )

    packs = build_apply_packs([lead], retriever=FakeRetriever(), chat=RecordingChat([_reply()]), limit=1)
    out = packs[0].to_html()

    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<b>Ltd</b>" not in out
    assert "&quot;me&quot;" in out


def test_to_html_escapes_remote_text_that_reaches_the_bullets_and_the_note():
    chat = RecordingChat(
        [
            _reply(
                note=(
                    "Hi <img src=x onerror=alert(1)> team, your listing matches work I "
                    "have shipped: cluster hardening, supply-chain checks in CI and a "
                    "handover the team can run without me. Both projects are public and "
                    "readable end to end, so you can judge the code before we speak. "
                    "Happy to walk through scope on a short call."
                ),
                bullets=["<b>devsecops-cicd-pipeline</b> signs every image in CI."],
            )
        ]
    )

    out = build_apply_packs(
        [_lead(90, 1)], retriever=FakeRetriever(), chat=chat, limit=1
    )[0].to_html()

    assert "<img src=x" not in out
    assert "&lt;img src=x" in out
    assert "<b>devsecops" not in out


def test_to_html_links_the_listing_url_with_quotes_escaped():
    lead = _lead(90, 1, url='https://jobs.example.com/x?a="1"')

    out = build_apply_packs(
        [lead], retriever=FakeRetriever(), chat=RecordingChat([_reply()]), limit=1
    )[0].to_html()

    assert 'href="https://jobs.example.com/x?a=&quot;1&quot;"' in out


# --- shape of the drafted content ----------------------------------------------------


def test_the_cover_note_stays_within_the_word_bound_even_when_the_model_rambles():
    long_note = " ".join(f"word{i}" for i in range(600))
    chat = RecordingChat([_reply(note=long_note)])

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=chat, limit=1)

    assert len(packs[0].cover_note.split()) <= MAX_NOTE_WORDS


def test_the_template_cover_note_is_also_within_the_word_bound():
    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=None, limit=1)

    assert len(packs[0].cover_note.split()) <= MAX_NOTE_WORDS


def test_bullets_are_capped_at_four_and_topped_up_to_at_least_two():
    many = RecordingChat([_reply(bullets=[f"Real artifact bullet number {i}." for i in range(9)])])
    one = RecordingChat([_reply(bullets=["Only one bullet came back."])])

    capped = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=many, limit=1)[0]
    topped = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=one, limit=1)[0]

    assert len(capped.why_you_fit) == MAX_BULLETS
    assert len(topped.why_you_fit) >= MIN_BULLETS


def test_no_pack_ever_quotes_a_price_when_standard_rate_is_unset(monkeypatch):
    monkeypatch.setenv("COPILOT_STANDARD_RATE", "")

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=None, limit=1)

    assert "15-minute call" in packs[0].suggested_rate
    assert "$" not in packs[0].suggested_rate


def test_a_configured_standard_rate_is_offered_as_a_ballpark_only(monkeypatch):
    monkeypatch.setenv("COPILOT_STANDARD_RATE", "£550/day")

    packs = build_apply_packs([_lead(90, 1)], retriever=FakeRetriever(), chat=None, limit=1)

    assert "£550/day" in packs[0].suggested_rate
    assert "ballpark" in packs[0].suggested_rate


def test_an_apply_pack_is_frozen_so_a_verified_proof_link_cannot_be_swapped_later():
    import dataclasses

    import pytest

    pack = ApplyPack(
        lead_url="https://jobs.example.com/1",
        title="t",
        company="c",
        fit_score=80,
        why_you_fit=["a", "b"],
        cover_note="note",
        suggested_rate="rate",
        proof_links=[REPO_URL],
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        pack.lead_url = HALLUCINATED_URL
