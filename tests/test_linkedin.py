"""Offline tests for the LinkedIn auto-posting subsystem (no network, no token).

The LinkedIn API is never hit: a fake client records calls in memory. The content
engine runs with a fake retriever + FakeChat. Each test gets an isolated SQLite DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import linkedin.poster as poster
from agents.llm import FakeChat
from db.models import Base, PostRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


class FakeRetriever:
    def retrieve(self, query, k=5):
        return [
            {
                "text": "llm-guardrails-gateway blocks prompt injection + caps token spend.",
                "source": "llm-guardrails-gateway",
                "kind": "win",
                "score": 0.9,
            }
        ]


class FakeLinkedIn:
    """Records posts instead of hitting the API."""

    def __init__(self):
        self.posted: list[str] = []

    def create_post(self, text, visibility="PUBLIC"):
        self.posted.append(text)
        return {
            "id": "urn:li:share:123",
            "url": "https://www.linkedin.com/feed/update/urn:li:share:123/",
            "status": 201,
        }


def _settings(**over):
    base = dict(linkedin_auto_post=True, linkedin_access_token="tok", max_posts_per_day=1)
    base.update(over)
    return SimpleNamespace(**base)


def _run(monkeypatch, *, publish, client=None, settings=None):
    monkeypatch.setattr(poster, "get_settings", lambda: settings or _settings())
    return poster.post_to_linkedin(
        kind="post",
        topic="secure LLM serving",
        publish=publish,
        client=client,
        retriever=FakeRetriever(),
        chat=FakeChat(),
    )


def test_draft_only_does_not_publish(temp_db, monkeypatch):
    fake = FakeLinkedIn()
    res = _run(monkeypatch, publish=False, client=fake)
    assert res["status"] == "draft"
    assert res["body"]
    assert fake.posted == []  # never called
    with dbsession.get_session() as s:
        rows = s.execute(select(PostRecord)).scalars().all()
        assert len(rows) == 1 and rows[0].status == "draft"


def test_publish_calls_api_and_records(temp_db, monkeypatch):
    fake = FakeLinkedIn()
    res = _run(monkeypatch, publish=True, client=fake)
    assert res["status"] == "published"
    assert res["post_url"].startswith("https://www.linkedin.com/")
    assert len(fake.posted) == 1
    with dbsession.get_session() as s:
        row = s.execute(select(PostRecord)).scalars().one()
        assert row.status == "published" and row.published_at is not None


def test_gate_off_skips_publish(temp_db, monkeypatch):
    fake = FakeLinkedIn()
    res = _run(monkeypatch, publish=True, client=fake, settings=_settings(linkedin_auto_post=False))
    assert res["status"] == "skipped"
    assert fake.posted == []


def test_daily_cap_blocks_second_post(temp_db, monkeypatch):
    # cap = 1: first publishes, a *different* topic the same day is capped.
    first = _run(monkeypatch, publish=True, client=FakeLinkedIn())
    assert first["status"] == "published"

    monkeypatch.setattr(poster, "get_settings", lambda: _settings())
    fake2 = FakeLinkedIn()
    res = poster.post_to_linkedin(
        kind="gig",  # distinct body (via distinct FakeChat) -> not dedupe; must be a cap skip
        topic="different angle",
        publish=True,
        client=fake2,
        retriever=FakeRetriever(),
        chat=FakeChat(responses=["a completely different post body about GitOps"]),
    )
    assert res["status"] == "skipped"
    assert "cap" in res["reason"]
    assert fake2.posted == []


def test_duplicate_content_is_skipped(temp_db, monkeypatch):
    first = _run(monkeypatch, publish=False, client=FakeLinkedIn())
    assert first["status"] == "draft"
    # same kind+topic+fake chat -> identical body -> dedupe
    res = _run(monkeypatch, publish=False, client=FakeLinkedIn())
    assert res["status"] == "skipped"
    assert res["reason"] == "duplicate-content"
