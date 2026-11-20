"""Primary/backup database selection: prefer the primary, fall back on a real disconnect."""
import pytest
from sqlalchemy import create_engine


@pytest.fixture()
def db_globals(monkeypatch, tmp_path):
    """Point the module at throwaway engines and restore every global afterwards."""
    from app import db as dbmod

    saved = (dbmod.engine, dbmod.SessionLocal, dbmod._active_db,
             dbmod._primary_engine, dbmod._backup_engine)
    yield dbmod, tmp_path
    (dbmod.engine, dbmod.SessionLocal, dbmod._active_db,
     dbmod._primary_engine, dbmod._backup_engine) = saved


def _engine(tmp_path, name):
    return create_engine(f"sqlite:///{tmp_path}/{name}.db",
                         connect_args={"check_same_thread": False})


def _unreachable(tmp_path, name):
    # the parent directory does not exist, so SQLite cannot open (or create) the file
    return create_engine(f"sqlite:///{tmp_path}/missing/{name}.db",
                         connect_args={"check_same_thread": False})


def test_startup_keeps_primary_when_it_answers(db_globals, monkeypatch):
    dbmod, tmp = db_globals
    primary, backup = _engine(tmp, "primary"), _engine(tmp, "backup")
    monkeypatch.setattr(dbmod, "_primary_engine", primary)
    monkeypatch.setattr(dbmod, "_backup_engine", backup)
    monkeypatch.setattr(dbmod, "engine", primary)
    monkeypatch.setattr(dbmod, "_active_db", "primary")

    assert dbmod.choose_database() == "primary"
    assert dbmod.engine is primary


def test_startup_falls_back_when_primary_is_unreachable(db_globals, monkeypatch):
    dbmod, tmp = db_globals
    primary, backup = _unreachable(tmp, "primary"), _engine(tmp, "backup")
    monkeypatch.setattr(dbmod, "_primary_engine", primary)
    monkeypatch.setattr(dbmod, "_backup_engine", backup)
    monkeypatch.setattr(dbmod, "engine", primary)
    monkeypatch.setattr(dbmod, "_active_db", "primary")

    assert dbmod.choose_database() == "backup"
    assert dbmod.engine is backup
    assert dbmod.SessionLocal.kw["bind"] is backup


def test_disconnect_switches_to_backup(db_globals, monkeypatch):
    dbmod, tmp = db_globals
    primary, backup = _engine(tmp, "primary"), _engine(tmp, "backup")
    monkeypatch.setattr(dbmod, "_primary_engine", primary)
    monkeypatch.setattr(dbmod, "_backup_engine", backup)
    monkeypatch.setattr(dbmod, "engine", primary)
    monkeypatch.setattr(dbmod, "_active_db", "primary")

    class Disconnect:
        is_disconnect = True

    dbmod._fail_over_on_disconnect(Disconnect())
    assert dbmod.active_database() == "backup"
    assert dbmod.engine is backup


def test_other_errors_do_not_switch(db_globals, monkeypatch):
    dbmod, tmp = db_globals
    primary, backup = _engine(tmp, "primary"), _engine(tmp, "backup")
    monkeypatch.setattr(dbmod, "_primary_engine", primary)
    monkeypatch.setattr(dbmod, "_backup_engine", backup)
    monkeypatch.setattr(dbmod, "engine", primary)
    monkeypatch.setattr(dbmod, "_active_db", "primary")

    class ConstraintError:
        is_disconnect = False

    dbmod._fail_over_on_disconnect(ConstraintError())
    assert dbmod.active_database() == "primary"
    assert dbmod.engine is primary
