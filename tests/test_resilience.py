import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from autodl_grab.browser import Browser
from autodl_grab.engine import Engine
from autodl_grab.api import ReadAPI
from autodl_grab.errors import (Ambiguous, CandidateRetry, NotSubmitted, Pause,
                               Retryable, TransientPage, InstanceNotVisible, browser_transient)
from conftest import SOURCE, TARGET


def test_thousand_mixed_fault_rounds_then_success(env):
    original = env.backend.instance
    calls = 0
    def instance(identifier):
        nonlocal calls
        calls += 1
        if calls <= 1000:
            if calls % 3 == 0:
                raise TimeoutError("fixture-secret")
            if calls % 3 == 1:
                raise Retryable("non JSON fixture-secret", 0)
            raise TransientPage("delayed rendering fixture-secret")
        return original(identifier)
    env.config.max_wait_seconds = 500000
    env.store.db.execute("UPDATE tasks SET config=? WHERE id=?", (json.dumps(env.config.snapshot()), env.task))
    env.store.db.commit()
    env.backend.instance = instance
    assert env.engine.run(env.task)["instance_id"] == TARGET
    assert len(env.backend.submits) == 1
    events = (env.home / "events.jsonl").read_text(encoding="utf-8")
    assert "fixture-secret" not in events
    assert events.count('"phase": "recovering"') == 1000


def test_candidate_cooldown_checks_other_candidates(env):
    def prepare(choice):
        if choice.kind == "clone":
            raise CandidateRetry("known temporary failure")
    env.backend.on_prepare = prepare
    assert env.engine.run(env.task)["kind"] == "rent"
    assert len(env.backend.submits) == 1


def test_all_candidates_cooling_still_checks_source(env):
    env.engine.cooldowns = {(kind, host.id): env.clock.value + 300
                            for kind in ("clone", "rent") for host in env.backend.hosts}
    assert env.engine.round(env.task, env.config) is None
    assert env.backend.prepares == []
    env.backend.startable = True
    assert env.engine.round(env.task, env.config)["kind"] == "start"


def test_expired_cooldown_can_prepare_again(env):
    env.engine.cooldowns[("clone", env.backend.hosts[0].id)] = env.clock.value - 1
    assert env.engine.run(env.task)["kind"] == "clone"


def test_over_budget_candidate_does_not_block_next(env):
    original = env.backend.prepare
    def prepare(config, source, choice):
        quote = original(config, source, choice)
        if choice.kind == "clone":
            quote["disk_daily"] = "100"
        return quote
    env.backend.prepare = prepare
    assert env.engine.run(env.task)["kind"] == "rent"
    assert len(env.backend.submits) == 1


def test_transient_final_guard_proves_no_click_and_reprepares(env):
    attempts = 0
    def submit(choice):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error = NotSubmitted("quote changed before final click")
            error.code = "quote_changed"
            raise error
        env.backend.create_target(choice)
        return TARGET
    env.backend.on_submit = submit
    assert env.engine.run(env.task)["instance_id"] == TARGET
    states = [r[0] for r in env.store.db.execute("SELECT state FROM actions ORDER BY id")]
    assert states == ["aborted", "done"]


def test_lost_response_waits_for_delayed_target_without_resubmit(env):
    captured = []
    env.backend.on_submit = lambda choice: captured.append(choice)
    original = env.backend.instances
    checks = 0
    def instances():
        nonlocal checks
        if captured:
            checks += 1
        if checks == 4:
            env.backend.create_target(captured[0])
        return original()
    env.backend.instances = instances
    assert env.engine.run(env.task)["instance_id"] == TARGET
    assert len(env.backend.submits) == 1
    assert checks >= 4


def test_pending_query_failures_respect_operation_deadline(env):
    env.backend.on_submit = lambda choice: None
    env.engine.round(env.task, env.config)
    original_created = env.store.pending(env.task)["created"]
    env.backend.instances = lambda: (_ for _ in ()).throw(Retryable("network"))
    with pytest.raises(Pause, match="最长等待"):
        env.engine.run(env.task)
    assert env.clock.value <= original_created + env.config.clone_timeout_seconds
    assert len(env.backend.submits) == 1
    assert env.store.pending(env.task)


def test_restart_only_reconciles_while_target_not_visible(env):
    env.backend.on_submit = lambda choice: None
    env.engine.round(env.task, env.config)
    choice = env.backend.submits[0]
    engine = Engine(env.backend, env.store, now=lambda: env.clock.value,
                    sleep=lambda seconds: (setattr(env.clock, "value", env.clock.value + seconds),
                                           env.backend.create_target(choice)), emit=lambda _: None)
    assert engine.run(env.task)["instance_id"] == TARGET
    assert len(env.backend.submits) == 1


@pytest.mark.parametrize("error", [Pause("auth"), ValueError("unknown schema")])
def test_unknown_and_auth_errors_do_not_retry(env, error):
    attempts = []
    def instance(_):
        attempts.append(1)
        raise error
    env.backend.instance = instance
    with pytest.raises(Pause):
        env.engine.run(env.task)
    assert len(attempts) == 1
    assert env.backend.submits == []


def test_initial_connection_failure_and_later_browser_recovery(env):
    attempts = []
    def ensure():
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("initial browser navigation failed")
    env.backend.ensure_ready = ensure
    assert env.engine.run(env.task)["kind"] == "clone"
    assert len(attempts) == 2


@pytest.mark.parametrize("code", ["disk_fee_pending", "quote_pending", "host_table_loading"])
def test_form_rebuilds_known_waits_three_times(tmp_path, monkeypatch, code):
    browser = Browser(tmp_path)
    attempts = []
    def prepare(*_):
        attempts.append(1)
        error = TransientPage("known delayed state")
        error.code = code
        raise error
    monkeypatch.setattr(browser, "prepare_form", prepare)
    with pytest.raises(CandidateRetry):
        browser.prepare(None, None, None)
    assert len(attempts) == 3


def test_browser_rebuild_closes_old_runtime_before_reopening(tmp_path, monkeypatch):
    browser = Browser(tmp_path)
    events = []
    browser.browser = SimpleNamespace(is_connected=lambda: False, close=lambda: events.append("close"))
    browser.playwright = SimpleNamespace(stop=lambda: events.append("stop"))
    monkeypatch.setattr(Browser, "__enter__", lambda self: events.append("open"))
    browser.ensure_ready()
    assert events == ["close", "stop", "open"]


def test_transport_classification_never_classifies_generic_guard_messages():
    assert not browser_transient(Pause("net::ERR_CONNECTION_RESET"))
    assert not browser_transient(ValueError("fixture timeout"))
    assert browser_transient(TimeoutError())


@pytest.mark.parametrize("status,expected", [(200,Retryable), (429,Retryable), (503,Retryable), (401,Pause), (403,Pause)])
def test_console_response_classification_and_diagnostics(tmp_path, status, expected):
    adapter = Browser(tmp_path)
    adapter.sessions = SimpleNamespace(route=lambda _: ("/api/v1/instance", "POST"))
    def invalid_json():
        raise ValueError("fixture-secret response body")
    response = SimpleNamespace(status=status, headers={"content-type":"text/html", "retry-after":"17"}, json=invalid_json)
    page = SimpleNamespace(url="https://www.autodl.com/console/instance/list",
                           expect_response=lambda *a,**kw:nullcontext(SimpleNamespace(value=response)),
                           on=lambda *args:None, remove_listener=lambda *args:None)
    with pytest.raises(expected) as caught:
        adapter.console_response(page, lambda: None)
    assert "fixture-secret" not in str(caught.value) + json.dumps(adapter.console_summary)
    if status in (429,503):
        assert caught.value.retry_after == 17


def test_readonly_api_non_json_is_retryable():
    def invalid_json():
        raise ValueError("fixture-secret")
    response = SimpleNamespace(status=200, json=invalid_json)
    session = SimpleNamespace(parameters=lambda *args: ({},{}), route=lambda _:('/api/v1/instance','POST'))
    context = SimpleNamespace(request=SimpleNamespace(post=lambda *args,**kwargs:response))
    with pytest.raises(Retryable) as caught:
        ReadAPI(context, session=session).post('/api/v1/instance', {})
    assert 'fixture-secret' not in str(caught.value)


def test_accepted_target_temporarily_missing_retries_without_new_order(env):
    original = env.backend.instance
    attempts = 0
    def instance(identifier):
        nonlocal attempts
        if identifier == TARGET:
            attempts += 1
            if attempts < 3:
                raise InstanceNotVisible("not yet visible")
        return original(identifier)
    env.backend.instance = instance
    assert env.engine.run(env.task)["instance_id"] == TARGET
    assert attempts == 3
    assert len(env.backend.submits) == 1


def test_missing_source_still_requires_review(env):
    env.backend.instance = lambda _: (_ for _ in ()).throw(InstanceNotVisible("source missing"))
    with pytest.raises(InstanceNotVisible):
        env.engine.run(env.task)
    assert env.backend.submits == []
