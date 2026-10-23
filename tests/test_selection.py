from dataclasses import replace
from decimal import Decimal

import pytest

from autodl_grab.config import Config, money
from autodl_grab.errors import Pause
from autodl_grab.models import check_quote, choose
from conftest import SOURCE


def test_gpu_priority_before_price(env):
    env.config.alternate_gpus = ["RTX 4090", "A100"]
    base = env.backend.hosts[0]
    env.backend.hosts = [replace(base, gpu="A100", hourly_per_gpu="0.2"),
                         replace(base, id="1111111111", gpu="RTX 4090", hourly_per_gpu="0.3"), base]
    assert [c.host.gpu for c in choose(env.config, env.backend.source, env.backend.reference, env.backend.hosts, "clone")] == ["RTX 3090", "RTX 4090", "A100"]


def test_same_model_price_then_idle(env):
    base = env.backend.hosts[0]
    hosts = [replace(base, id="1111111111", idle=8, hourly_per_gpu="1.1"),
             replace(base, id="2222222222", idle=2), replace(base, id="3333333333", idle=6)]
    assert [c.host.id for c in choose(env.config, env.backend.source, env.backend.reference, hosts, "clone")] == ["3333333333", "2222222222", "1111111111"]


def test_region_priority_before_gpu_and_price(env):
    env.config.other_regions = ["r3", "r2"]
    env.config.alternate_gpus = ["RTX 4090"]
    base = env.backend.hosts[1]
    hosts = [base, replace(base, id="2222222222", region="r3", gpu="RTX 4090", hourly_per_gpu="1.2")]
    assert choose(env.config, env.backend.source, env.backend.reference, hosts, "rent")[0].host.region == "r3"


@pytest.mark.parametrize("changes", [
    {"idle": 1}, {"vram_gb": 23}, {"cpu_per_gpu": 5}, {"memory_per_gpu_gb": 23},
    {"cuda": "12.0"}, {"driver": "530.1"}, {"hourly_per_gpu": "3.00"},
    {"max_data_expand_gb": 4}, {"data_gb": 49}, {"gpu": "unlisted"}, {"system_gb": 29},
])
def test_reject_unsuitable_candidates(env, changes):
    host = replace(env.backend.hosts[0], **changes)
    assert choose(env.config, env.backend.source, env.backend.reference, [host], "clone") == []


def test_explicit_minimum_override(env):
    env.config.min_cpu, env.config.min_memory_gb = 10, 40
    host = replace(env.backend.hosts[0], cpu_per_gpu=5, memory_per_gpu_gb=20)
    assert choose(env.config, env.backend.source, env.backend.reference, [host], "clone")


def test_per_instance_hourly_cap(env):
    env.config.max_hourly = "1.99"
    assert choose(env.config, env.backend.source, env.backend.reference, env.backend.hosts, "clone") == []


def test_disk_daily_cap(env):
    env.config.max_disk_daily = "0.03"
    assert choose(env.config, env.backend.source, env.backend.reference, env.backend.hosts, "clone") == []


def test_decimal_arithmetic(env):
    host = replace(env.backend.hosts[0], hourly_per_gpu="0.1")
    choice = choose(env.config, env.backend.source, env.backend.reference, [host], "clone")[0]
    assert Decimal(choice.hourly) == Decimal("0.2")
    assert money(choice.disk_daily) == Decimal("0.0330")


@pytest.mark.parametrize("field,value", [("gpu", "wrong"), ("count", 1), ("region", "r9"),
                                         ("data_expand_gb", 0), ("system_gb", 29), ("data_gb", 49),
                                         ("kind", "rent"), ("image", "wrong")])
def test_final_quote_mismatch(env, field, value):
    choice = choose(env.config, env.backend.source, env.backend.reference, env.backend.hosts, "clone")[0]
    quote = env.backend.prepare(env.config, env.backend.source, choice)
    quote[field] = value
    with pytest.raises(Pause, match="不一致"):
        check_quote(env.config, choice, quote)


@pytest.mark.parametrize("field,value", [("hourly", "5.01"), ("disk_daily", "2.01")])
def test_final_price_cap(env, field, value):
    choice = choose(env.config, env.backend.source, env.backend.reference, env.backend.hosts, "clone")[0]
    quote = env.backend.prepare(env.config, env.backend.source, choice)
    quote[field] = value
    with pytest.raises(Pause, match="价格超过"):
        check_quote(env.config, choice, quote)


def test_final_price_changed_within_cap_still_pauses(env):
    choice = choose(env.config, env.backend.source, env.backend.reference, env.backend.hosts, "clone")[0]
    quote = env.backend.prepare(env.config, env.backend.source, choice)
    quote["hourly"] = "2.10"
    with pytest.raises(Pause, match="价格已变化"):
        check_quote(env.config, choice, quote)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "wrong"])
def test_invalid_money(value):
    with pytest.raises(Pause):
        money(value)


def test_config_requires_budgets(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text(f'source_id = "{SOURCE}"', encoding="utf-8")
    with pytest.raises(Pause):
        Config.load(path)


def test_example_config_loads():
    assert Config.load("config.example.toml").source_id == SOURCE


@pytest.mark.parametrize("changes", [{"other_regions": ["r2"]}, {"source_id": "0123"},
                                     {"poll_seconds": 0}, {"alternate_gpus": ["a", "a"]},
                                     {"min_cuda": "latest"}, {"max_clones": -1}])
def test_invalid_config(changes):
    values = {"source_id": SOURCE, "max_hourly": "2", "max_disk_daily": "1", **changes}
    with pytest.raises(Pause):
        Config(**values)
