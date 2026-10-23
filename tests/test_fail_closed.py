from dataclasses import replace

import pytest

from autodl_grab.errors import Ambiguous, Pause, Retryable
from autodl_grab.models import choose


@pytest.mark.parametrize("reason", ["登录已失效", "余额不足", "平台返回未知状态", "页面结构变化"])
def test_explicit_submit_error_never_falls_through(env, reason):
    def submit(choice):
        # Even if a platform record appears at the same moment, an explicit
        # unexpected return requires review. A later resume reconciles it.
        env.backend.create_target(choice)
        raise Pause(reason)
    env.backend.on_submit = submit
    with pytest.raises(Pause, match=reason):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1
    assert env.store.get(env.task)["phase"] == "paused"
    assert env.store.pending(env.task)


def test_query_network_failure_after_submit_reconciles_on_retry(env):
    original = env.backend.instance
    failures = []
    def submit(choice):
        env.backend.create_target(choice)
        failures.append(True)
        return env.backend.target.id
    def instance(identifier):
        if failures and identifier != env.backend.source.id:
            failures.pop()
            raise Retryable("network", 5)
        return original(identifier)
    env.backend.on_submit, env.backend.instance = submit, instance
    assert env.engine.run(env.task)["kind"] == "clone"
    assert len(env.backend.submits) == 1


def test_target_specs_mismatch_blocks(env):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.count += 1
        return env.backend.target.id
    env.backend.on_submit = submit
    with pytest.raises(Ambiguous, match="规格"):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1


def test_unknown_backend_exception_pauses_and_retains_record(env):
    def inventory(_):
        raise RuntimeError("Unexpected UI timeout")
    env.backend.inventory = inventory
    with pytest.raises(Pause, match="未知异常"):
        env.engine.run(env.task)
    assert env.store.get(env.task)["phase"] == "paused"


def test_resume_does_not_need_source_inventory_once_target_known(env):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.copy_complete = False
        return env.backend.target.id
    env.backend.on_submit = submit
    env.engine.round(env.task, env.config)
    def unavailable(_):
        raise AssertionError("Must not query new inventory")
    env.backend.inventory = unavailable
    env.backend.target.copy_complete = True
    assert env.engine.run(env.task)["instance_id"] == env.backend.target.id
    assert len(env.backend.submits) == 1
