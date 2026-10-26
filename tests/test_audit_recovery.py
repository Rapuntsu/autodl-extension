import json
import sqlite3
from pathlib import Path

import pytest

from autodl_grab.api import RequestSession, retry_seconds
from autodl_grab.config import Config
from autodl_grab.browser import Browser
from autodl_grab.errors import Ambiguous, NotSubmitted, Pause, Retryable, CandidateRetry
from autodl_grab.store import Store


@pytest.mark.parametrize("kind", ["clone", "start"])
def test_proven_preclick_refusal_allows_safe_resume(env, kind):
    if kind == "start":
        env.backend.startable = True
        original = env.backend.submit_start
        env.backend.submit_start = lambda _: (_ for _ in ()).throw(NotSubmitted("guard refused"))
    else:
        original = env.backend.submit
        env.backend.submit = lambda *args: (_ for _ in ()).throw(NotSubmitted("guard refused"))
    with pytest.raises(NotSubmitted):
        env.engine.run(env.task)
    assert env.store.pending(env.task) is None
    assert env.store.clone_count(env.task) == 0
    assert env.store.db.execute("SELECT state FROM actions").fetchone()[0] == "aborted"
    if kind == "start":
        env.backend.submit_start = original
    else:
        env.backend.submit = original
    assert env.engine.run(env.task)["kind"] == kind
    assert len(env.backend.submits) == 1


def test_source_transition_deadline_survives_pause_and_resume(env):
    env.backend.source.state = "starting"
    env.engine.round(env.task, env.config)
    since = env.store.get(env.task)["wait_since"]
    env.clock.value += 601
    for _ in range(2):
        with pytest.raises(Pause, match="过渡状态超时"):
            env.engine.run(env.task)
        assert env.store.get(env.task)["wait_since"] == since
    env.backend.source.state = "running"
    env.backend.source.gpu_mode = True
    assert env.engine.run(env.task)["kind"] == "start"


def test_clone_can_confirm_source_unlock_with_positive_ui_evidence(env):
    env.backend.source.clone_locked = None
    env.backend.verify_clone_unlocked = lambda _: True
    assert env.engine.run(env.task)["kind"] == "clone"


def test_copy_in_progress_does_not_require_unlock_evidence_yet(env):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.copy_complete = False
        return env.backend.target.id
    env.backend.on_submit = submit
    env.backend.source.clone_locked = None
    env.backend.verify_clone_unlocked = lambda _: True
    assert env.engine.round(env.task, env.config) is None
    env.backend.target.copy_complete = True
    assert env.engine.run(env.task)["kind"] == "clone"
    assert len(env.backend.submits) == 1


def test_returned_clone_id_still_requires_matching_source(env):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.source_id = "7654321098-abcdefgh"
        return env.backend.target.id
    env.backend.on_submit = submit
    with pytest.raises(Ambiguous, match="来源"):
        env.engine.run(env.task)
    assert env.store.pending(env.task)
    assert len(env.backend.submits) == 1


def test_old_database_migration_preserves_wait_and_pending(tmp_path):
    db = sqlite3.connect(tmp_path / "tasks.sqlite3")
    db.executescript("""
        CREATE TABLE tasks(id TEXT PRIMARY KEY, config TEXT NOT NULL, phase TEXT NOT NULL,
                           created REAL NOT NULL, updated REAL NOT NULL, target TEXT,
                           detail TEXT NOT NULL DEFAULT '', result TEXT, candidate TEXT);
        CREATE TABLE actions(id INTEGER PRIMARY KEY, task TEXT NOT NULL, kind TEXT NOT NULL,
                             choice TEXT, before_ids TEXT NOT NULL, target TEXT, state TEXT NOT NULL,
                             created REAL NOT NULL, quote TEXT NOT NULL);
    """)
    db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)",
               ("old", "{}", "waiting_source", 1, 2, None, json.dumps({"state":"starting", "since":123}), None, None))
    db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?)",
               (1, "old", "start", None, "[]", None, "pending", 1, "{}"))
    db.commit()
    db.close()
    for _ in range(2):
        store = Store(tmp_path)
        assert store.get("old")["wait_since"] == 123
        assert store.pending("old")["state"] == "pending"
        store.close()


def test_event_log_failure_keeps_durable_task_update(env, monkeypatch):
    original = Path.open
    def open_path(path, *args, **kwargs):
        if path.name == "events.jsonl":
            raise OSError("unwritable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_path)
    with pytest.warns(RuntimeWarning, match="SQLite"):
        env.store.update(env.task, phase="paused", detail="original reason")
    assert env.store.get(env.task)["detail"] == "original reason"


@pytest.mark.parametrize("value", ["Infinity", "NaN", "-Infinity", "invalid", None])
def test_invalid_retry_after_never_causes_unbounded_sleep(value):
    assert retry_seconds(value) is None


def test_network_backoff_respects_remaining_task_deadline(env):
    env.clock.value += env.config.max_wait_seconds - 1
    env.backend.instance = lambda _: (_ for _ in ()).throw(Retryable("429", 9000))
    before = env.clock.value
    with pytest.raises(Pause, match="最长等待"):
        env.engine.run(env.task)
    assert env.clock.value - before == 1


def test_inventory_clears_ui_search_but_preserves_account_scope():
    session = RequestSession()
    session.bind("/api/v1/instance", {"path":"/api/v1/sub_user/instance", "method":"POST", "headers":{},
                 "body":{"page_index":1,"page_size":10,"uuid":"old-search","name":"old-name",
                         "child_scope":"account", "sub_name":"selected-child"}})
    body, _ = session.parameters("/api/v1/instance", {"page_index":1,"page_size":50})
    assert "uuid" not in body and "name" not in body
    assert body["child_scope"] == "account" and body["sub_name"] == "selected-child"


@pytest.mark.parametrize("key,value", [("source_id",123), ("image",123), ("max_clones",None),
                                      ("rent_data_expand_gb",None), ("min_cpu",True)])
def test_invalid_config_types_raise_actionable_pause(key, value):
    with pytest.raises(Pause):
        Config(**{"source_id":"0123456789-abcdefgh", "max_hourly":"5", "max_disk_daily":"2", key:value})


def test_schema_diagnostic_failure_keeps_original_reason(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("fixture", encoding="utf-8")
    error = Browser(blocked).schema_pause("instance", {}, Pause("original schema problem"))
    assert "original schema problem" in str(error) and "写入失败" in str(error)


@pytest.mark.parametrize("persistent", [False, True])
def test_overlapping_ui_rows_restart_entire_prepare_but_are_bounded(tmp_path, monkeypatch, persistent):
    browser = Browser(tmp_path)
    attempts = []
    def prepare(*args):
        attempts.append(True)
        if persistent or len(attempts) == 1:
            error = Pause("duplicate rows")
            error.code = "duplicate_host_rows"
            raise error
        return {"quote":"fresh"}
    monkeypatch.setattr(browser, "prepare_form", prepare)
    if persistent:
        with pytest.raises(CandidateRetry, match="三次"):
            browser.prepare(None,None,None)
        assert len(attempts) == 3
    else:
        assert browser.prepare(None,None,None) == {"quote":"fresh"}
        assert len(attempts) == 2


def test_structural_refusal_never_restarts_prepare(tmp_path, monkeypatch):
    browser = Browser(tmp_path)
    attempts = []
    def prepare(*args):
        attempts.append(True)
        raise Pause("source changed")
    monkeypatch.setattr(browser, "prepare_form", prepare)
    with pytest.raises(Pause, match="source changed"):
        browser.prepare(None,None,None)
    assert len(attempts) == 1
