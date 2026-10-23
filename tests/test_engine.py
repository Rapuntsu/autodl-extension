from dataclasses import replace
import json

import pytest

from autodl_grab.config import Config
from autodl_grab.engine import Engine
from autodl_grab.errors import Ambiguous, CloneExhausted, NoResource, Pause, Retryable
from autodl_grab.models import Instance, choose
from autodl_grab.store import Store
from conftest import SOURCE, TARGET


def test_start_source(env):
    env.backend.startable = True
    result = env.engine.run(env.task)
    assert result["instance_id"] == SOURCE
    assert env.backend.submits == ["start"]


def test_already_running(env):
    env.backend.source.state, env.backend.source.gpu_mode = "running", True
    assert env.engine.run(env.task)["kind"] == "start"
    assert env.backend.submits == []


@pytest.mark.parametrize("mode", [False, None])
def test_nogpu_or_unknown_mode_pauses(env, mode):
    env.backend.source.state, env.backend.source.gpu_mode = "running", mode
    with pytest.raises(Pause, match="无卡模式"):
        env.engine.run(env.task)
    assert env.store.get(env.task)["phase"] == "paused"
    assert not env.backend.submits


@pytest.mark.parametrize("state", ["starting", "stopping", "creating", "cloning"])
def test_source_transition_timeout(env, state):
    env.backend.source.state = state
    assert env.engine.round(env.task, env.config) is None
    env.clock.value += 601
    with pytest.raises(Pause, match="过渡状态超时"):
        env.engine.run(env.task)
    assert not env.backend.submits


def test_late_source_transition_gets_full_timeout(env):
    env.clock.value += 7200
    env.backend.source.state = "starting"
    assert env.engine.round(env.task, env.config) is None
    assert env.store.get(env.task)["phase"] == "waiting_source"


def test_clone_success_and_expansion(env):
    result = env.engine.run(env.task)
    assert result["kind"] == "clone"
    assert "未逐文件校验" in result["data_status"]
    assert env.backend.prepares[0].data_expand_gb == 5
    assert len(env.backend.submits) == 1


@pytest.mark.parametrize("copy_complete,source_locked", [(False, False), (True, True)])
def test_running_is_not_copy_complete(env, copy_complete, source_locked):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.copy_complete = copy_complete
        env.backend.source.clone_locked = source_locked
        return TARGET
    env.backend.on_submit = submit
    assert env.engine.round(env.task, env.config) is None
    assert env.store.get(env.task)["phase"] == "monitoring"
    env.clock.value += 3601
    with pytest.raises(Pause, match="操作超时"):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1


@pytest.mark.parametrize("copy_complete,source_locked", [(None, False), (True, None)])
def test_unknown_copy_or_lock_field_pauses_immediately(env, copy_complete, source_locked):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.copy_complete = copy_complete
        env.backend.source.clone_locked = source_locked
        return TARGET
    env.backend.on_submit = submit
    with pytest.raises(Pause, match="字段未知"):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1
    assert env.store.pending(env.task)["target"] == TARGET


def test_finish_copy_after_resume(env):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.copy_complete = False
        return TARGET
    env.backend.on_submit = submit
    env.engine.round(env.task, env.config)
    env.backend.target.copy_complete = True
    env.store.update(env.task, phase="paused")
    assert env.engine.run(env.task)["kind"] == "clone"
    assert len(env.backend.submits) == 1


def test_rent_when_no_same_region(env):
    env.backend.hosts = [h for h in env.backend.hosts if h.region == "r2"]
    result = env.engine.run(env.task)
    assert result["kind"] == "rent"
    assert result["data_status"] == "新租实例，未迁移原数据"


def test_exhausted_clones_fall_back(env):
    def prepare(choice):
        if choice.kind == "clone":
            raise CloneExhausted()
    env.backend.on_prepare = prepare
    assert env.engine.run(env.task)["kind"] == "rent"
    assert len(env.backend.submits) == 1


def test_disabled_clones_fall_back(env):
    env.config.max_clones = 0
    assert env.engine.round(env.task, env.config)["kind"] == "rent"


def test_explicit_inventory_rejection_next_candidate(env):
    def submit(choice):
        if choice.kind == "clone":
            raise NoResource()
        env.backend.create_target(choice)
        return TARGET
    env.backend.on_submit = submit
    assert env.engine.run(env.task)["kind"] == "rent"
    assert len(env.backend.submits) == 2
    assert env.store.clone_count(env.task) == 0


def test_all_empty_repeated_poll_then_stock(env):
    hosts = env.backend.hosts[:]
    def inventory():
        env.backend.hosts = [] if env.backend.inventory_calls < 3 else hosts
    env.backend.on_inventory = inventory
    assert env.engine.run(env.task)["kind"] == "clone"
    assert env.backend.inventory_calls == 3
    assert env.clock.value > env.store.get(env.task)["created"]


def test_original_recovers_before_submit(env):
    env.backend.on_prepare = lambda choice: setattr(env.backend, "startable", True)
    assert env.engine.run(env.task)["instance_id"] == SOURCE
    assert env.backend.submits == ["start"]


def test_lost_response_reconciles_unique_target(env):
    def submit(choice):
        env.backend.create_target(choice)
        raise TimeoutError()
    env.backend.on_submit = submit
    assert env.engine.run(env.task)["instance_id"] == TARGET
    assert len(env.backend.submits) == 1


def test_lost_response_no_target_never_resubmits(env):
    env.backend.on_submit = lambda choice: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(Ambiguous):
        env.engine.run(env.task)
    with pytest.raises(Ambiguous):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1
    assert env.store.pending(env.task)["state"] == "pending"


def test_multiple_new_instances_pause(env):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.extra = [replace(env.backend.target, id="7654321098-abcdefgh")]
        raise TimeoutError()
    env.backend.on_submit = submit
    with pytest.raises(Ambiguous):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1


def test_restart_after_persisted_intent(env):
    choice = choose(env.config, env.backend.source, env.backend.reference, env.backend.hosts, "clone")[0]
    quote = env.backend.prepare(env.config, env.backend.source, choice)
    env.store.intent(env.task, "clone", choice, {SOURCE}, quote)
    env.backend.create_target(choice)
    # A distinct SQLite connection represents a fresh process.
    reopened = Store(env.home)
    try:
        engine = Engine(env.backend, reopened, now=lambda: env.clock.value)
        assert engine.run(env.task)["instance_id"] == TARGET
    finally:
        reopened.close()
    assert env.backend.submits == []


def test_unknown_page_after_target_creation_never_falls_back(env):
    def submit(choice):
        env.backend.create_target(choice)
        env.backend.target.state = "stopped"
        return TARGET
    env.backend.on_submit = submit
    with pytest.raises(Pause, match="目标未进入"):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1


def test_returned_old_id_pauses(env):
    env.backend.on_submit = lambda choice: SOURCE
    with pytest.raises(Ambiguous, match="已有实例 ID"):
        env.engine.run(env.task)
    assert len(env.backend.submits) == 1


def test_submit_intent_is_durable_before_effect(env):
    def submit(choice):
        assert env.store.pending(env.task)["state"] == "pending"
        assert json.loads(env.store.pending(env.task)["before_ids"]) == [SOURCE]
        env.backend.create_target(choice)
        return TARGET
    env.backend.on_submit = submit
    env.engine.run(env.task)


def test_ctrl_c_keeps_pending_action(env):
    env.backend.on_submit = lambda choice: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        env.engine.run(env.task)
    assert env.store.get(env.task)["phase"] == "paused"
    assert env.store.pending(env.task)


def test_network_backoff_and_retry_after(env):
    calls, sleeps = [], []
    original = env.backend.instance
    def instance(identifier):
        calls.append(identifier)
        if len(calls) == 1:
            raise Retryable("429", 90)
        return original(identifier)
    env.backend.instance = instance
    def sleep(seconds):
        sleeps.append(seconds)
        env.clock.value += seconds
    env.engine.sleep = sleep
    env.engine.run(env.task)
    assert sum(sleeps) >= 90
    assert max(sleeps) <= 30


def test_unknown_source_state_pause(env):
    env.backend.source.state = "surprise"
    with pytest.raises(Pause, match="未知源实例"):
        env.engine.run(env.task)


def test_readonly_plan_has_no_effects(env):
    result = env.engine.plan(env.config)
    assert result["clone"] and result["rent"]
    assert not env.backend.submits and not env.backend.prepares
    assert env.store.pending(env.task) is None


def test_task_wait_deadline(env):
    env.clock.value += 86401
    with pytest.raises(Pause, match="最长等待"):
        env.engine.run(env.task)
    assert not env.backend.submits


def test_completed_resume_is_readonly(env):
    result = env.engine.run(env.task)
    assert env.engine.run(env.task) == result
    assert len(env.backend.submits) == 1
