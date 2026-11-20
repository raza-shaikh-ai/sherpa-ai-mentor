"""Housekeeping: wipe indexed repo data every AUTO_CLEAR_DAYS days (default 10).

Small free-tier Postgres plans cap storage and compute, and the repo tables are what grow:
files, chunks, cached agent outputs, finished upload jobs and their embedding vectors. This
sweeper deletes them on a fixed cycle so the deployment keeps running without manual
cleanup. Accounts, provider keys, custom endpoints, study results and usage counters stay.

The cycle is tracked in DATA_DIR/auto-clear.json rather than in the database, so neither a
restart nor the wipe itself resets it. The first run starts the clock instead of wiping
immediately — deploy day is day 0. Run `python -m app.autoclear` to wipe now.
"""
import json
import logging
import os
import threading
import time

from sqlalchemy import delete, func, select, text

from . import db as dbmod
from . import private
from .config import settings
from .dbmodels import IngestJob, Repo

log = logging.getLogger(__name__)

MARKER = "auto-clear.json"
_SECONDS_PER_DAY = 86_400


def marker_path() -> str:
    return os.path.join(settings.data_dir, MARKER)


def last_cleared() -> float | None:
    """When the last wipe ran, or None if this deployment has never started the clock."""
    try:
        with open(marker_path(), encoding="utf-8") as f:
            return float(json.load(f)["last_cleared"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _stamp(at: float) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = f"{marker_path()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_cleared": at}, f)
    os.replace(tmp, marker_path())  # atomic: a crash mid-write can't corrupt the cycle


def clear_repo_data() -> dict:
    """Delete every indexed repo and derived artifact. Keeps users and their settings.

    Deleting a `repos` row cascades to repo_files, repo_chunks, repo_artifacts and
    quiz_attempts (their foreign keys are ON DELETE CASCADE). Embedding vectors are
    content-addressed and shared, so only those no leftover chunk points at are removed.
    """
    with dbmod.SessionLocal() as db:
        jobs = db.execute(delete(IngestJob).where(
            IngestJob.status.in_(("ready", "failed")))).rowcount or 0
        # jobs still queued/processing are left to finish; the next wipe collects them
        active_jobs = db.execute(select(func.count()).select_from(IngestJob)).scalar_one()
        repo_ids = list(db.execute(select(Repo.id)).scalars())
        repos = db.execute(delete(Repo)).rowcount or 0
        embeddings = 0
        if dbmod.vectors_ready:
            embeddings = db.execute(text(
                "DELETE FROM chunk_embeddings e WHERE NOT EXISTS ("
                " SELECT 1 FROM repo_chunks c WHERE c.text_hash = e.text_hash)"
            )).rowcount or 0
        db.commit()

    for repo_id in repo_ids:
        private.drop(repo_id)  # private snapshots live in memory, not in the DB
    return {"repos": repos, "repo_ids": repo_ids, "jobs": jobs,
            "active_jobs_kept": active_jobs, "embeddings": embeddings}


def maybe_clear(now: float | None = None, on_cleared=None) -> dict | None:
    """Wipe if the cycle is due. Returns the stats when a wipe happened, else None."""
    if not settings.auto_clear_enabled or settings.auto_clear_days <= 0:
        return None
    now = time.time() if now is None else now
    last = last_cleared()
    if last is None:
        _stamp(now)
        log.info("autoclear: clock started; next wipe in %g day(s)", settings.auto_clear_days)
        return None
    if now < last + settings.auto_clear_days * _SECONDS_PER_DAY:
        return None

    stats = clear_repo_data()
    repo_ids = stats.pop("repo_ids")
    _stamp(now)
    log.info("autoclear: wiped %s", stats)
    if on_cleared:
        try:
            on_cleared(repo_ids)
        except Exception:  # noqa: BLE001 — housekeeping must not undo a successful wipe
            log.exception("autoclear: cleanup callback failed")
    return stats


def start_scheduler(interval: float = 3600, on_cleared=None) -> threading.Event:
    """Background thread that checks hourly; set the returned event to stop it."""
    stop = threading.Event()

    def loop():
        while True:
            try:
                maybe_clear(on_cleared=on_cleared)
            except Exception:  # noqa: BLE001 — a DB blip must not kill the sweeper
                log.exception("autoclear: sweep failed")
            if stop.wait(interval):
                return

    threading.Thread(target=loop, name="auto-clear", daemon=True).start()
    return stop


if __name__ == "__main__":  # python -m app.autoclear — wipe now and restart the cycle
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = clear_repo_data()
    result.pop("repo_ids", None)
    _stamp(time.time())
    log.info("autoclear: manual wipe done: %s", result)
