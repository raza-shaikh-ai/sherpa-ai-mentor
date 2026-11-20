"""The housekeeping sweep frees repo data on a 10-day cycle, keeping accounts."""
import time

import pytest
from sqlalchemy import func, select

DAY = 86_400


@pytest.fixture()
def seeded(client):
    """One signed-in user with one indexed repo, one chunk and one finished upload job."""
    r = client.post("/api/auth/register", json={"email": "wipe@example.com",
                                                "password": "password123"})
    assert r.status_code == 201, r.text

    from app import db as dbmod
    from app.dbmodels import IngestJob, Repo, RepoChunk, User

    with dbmod.SessionLocal() as db:
        user = db.execute(select(User).where(User.email == "wipe@example.com")).scalar_one()
        db.add(Repo(id="abc123", owner_id=user.id, name="demo", files_count=1, chunks_count=1))
        db.add(RepoChunk(repo_id="abc123", ord=0, file="a.py", start_line=1, end_line=2,
                         kind="code", text="print(1)", text_hash="h" * 40))
        db.add(IngestJob(id="job1", user_id=user.id, repo_id="abc123", name="demo",
                         status="ready", created_at=time.time(), updated_at=time.time()))
        db.commit()
        return {"user_id": user.id}


def _counts():
    from app import db as dbmod
    from app.dbmodels import IngestJob, Repo, RepoChunk, User

    with dbmod.SessionLocal() as db:
        return {name: db.execute(select(func.count()).select_from(model)).scalar_one()
                for name, model in (("users", User), ("repos", Repo),
                                    ("chunks", RepoChunk), ("jobs", IngestJob))}


def test_wipe_clears_repo_data_but_keeps_accounts(client, seeded, tmp_path, monkeypatch):
    from app import autoclear

    monkeypatch.setattr(autoclear.settings, "data_dir", str(tmp_path))
    stats = autoclear.clear_repo_data()

    assert stats["repos"] == 1
    assert stats["jobs"] == 1
    assert stats["repo_ids"] == ["abc123"]
    assert _counts() == {"users": 1, "repos": 0, "chunks": 0, "jobs": 0}


def test_cycle_starts_on_first_run_then_wipes_after_ten_days(client, seeded, tmp_path, monkeypatch):
    from app import autoclear

    monkeypatch.setattr(autoclear.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(autoclear.settings, "auto_clear_enabled", True)
    monkeypatch.setattr(autoclear.settings, "auto_clear_days", 10)
    now = 1_700_000_000.0

    assert autoclear.maybe_clear(now=now) is None  # deploy day: start the clock, don't wipe
    assert autoclear.last_cleared() == now
    assert _counts()["repos"] == 1

    assert autoclear.maybe_clear(now=now + 9 * DAY) is None
    assert _counts()["repos"] == 1

    stats = autoclear.maybe_clear(now=now + 10 * DAY)
    assert stats is not None and stats["repos"] == 1
    assert _counts()["repos"] == 0
    assert _counts()["users"] == 1
    assert autoclear.last_cleared() == now + 10 * DAY


def test_disabled_never_wipes(client, seeded, tmp_path, monkeypatch):
    from app import autoclear

    monkeypatch.setattr(autoclear.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(autoclear.settings, "auto_clear_enabled", False)

    assert autoclear.maybe_clear(now=1_700_000_000.0 + 365 * DAY) is None
    assert _counts()["repos"] == 1
