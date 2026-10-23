from dataclasses import replace
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from autodl_grab.browser import Browser, instance_data_base_gb, instance_data_expansion_gb, instance_system_disk_gb, parse_host, parse_instance
from autodl_grab.config import Config
from autodl_grab.errors import Pause
from autodl_grab.models import Host

GIB = 1024 ** 3
SOURCE = "0123456789-abcdefgh"


def actual_shape(**changes):
    # Fictional values with field names from the user's value-free source schema.
    return {"uuid": SOURCE, "region_sign": "fixture-east", "machine_id": "0123456789",
            "snapshot_gpu_alias_name": "RTX 3090", "req_gpu_amount": 2, "status": "shutdown",
            "system_init_disk_size": 0, "expand_system_disk_size": 0,
            "max_writable_size_in_byte": 30 * GIB,
            "machine_info_snapshot": {"max_instance_disk_size": 10 * GIB},
            "expand_data_disk_size": 100 * GIB,
            "usage_info": {"valid": False, "root_fs_total_size": 31526391808, "data_disk_total_size": 0},
            **changes}


@pytest.mark.parametrize("expanded,total", [(0, 30), (30, 30), (60, 60), (530, 530)])
def test_actual_system_fields_are_byte_quotas_not_additive(expanded, total):
    assert instance_system_disk_gb(actual_shape(expand_system_disk_size=expanded * GIB,
                                               max_writable_size_in_byte=total * GIB)) == total


@pytest.mark.parametrize("value", [0, 1, 100, 2048])
def test_actual_data_expansion_field_converts_bytes(value):
    assert instance_data_expansion_gb(actual_shape(expand_data_disk_size=value * GIB)) == value


def test_filesystem_usage_is_never_treated_as_provisioned_quota():
    raw = actual_shape()
    host = Host("0123456789", "fixture-east", "RTX 3090", 0, 24, 6, 24, "535.1", "12.1", "1", 50, 1000)
    assert instance_system_disk_gb(raw) == 30
    assert instance_data_base_gb(raw, host) == 10
    assert instance_data_expansion_gb(raw) == 100
    assert instance_data_base_gb(raw, replace(host, data_gb=100)) == 10


def test_dont_use_disk_expand_available_as_paid_expansion():
    raw = actual_shape(disk_expand_available=1)
    raw.pop("expand_data_disk_size")
    with pytest.raises(Pause, match="缺少容量字段"):
        instance_data_expansion_gb(raw)


@pytest.mark.parametrize("raw", [{"system_init_disk_size": 30 * GIB},
                                  {"root_fs_total_size": 30 * GIB},
                                  {"system_init_disk_size": 30 * GIB, "expand_system_disk_size": 20 * GIB},
                                  {"system_init_disk_size": 30 * GIB, "expand_system_disk_size": 1},
                                  {"system_init_disk_size": 0, "expand_system_disk_size": 0}])
def test_missing_ambiguous_or_non_gib_quotas_still_pause(raw):
    with pytest.raises(Pause):
        instance_system_disk_gb(raw)


def test_conflicting_explicit_and_actual_system_quota_pause():
    with pytest.raises(Pause, match="不一致"):
        instance_system_disk_gb(actual_shape(system_disk_gb=60))


def test_legacy_capacity_fields_still_supported():
    raw = {"system_disk_gb": 60, "data_disk_gb": 50, "data_expand_gb": 5}
    assert instance_system_disk_gb(raw) == 60
    assert instance_data_expansion_gb(raw) == 5
    assert instance_data_base_gb(raw, SimpleNamespace(data_gb=100)) == 50


def test_market_aliases_identified_in_upstream_are_supported():
    raw = json.loads((Path(__file__).parent / "fixtures/host.json").read_text(encoding="utf-8"))
    raw["cpu_per_gpu"] = raw.pop("cpu_limit")
    raw["mem_per_gpu"] = raw.pop("mem_limit_in_byte")
    raw["highest_cuda_version"] = raw.pop("cuda_version")
    raw["max_instance_disk_size"] = raw.pop("data_disk_gb") * GIB
    host = parse_host(raw)
    assert host.cpu_per_gpu == 6
    assert host.memory_per_gpu_gb == 24
    assert host.cuda == "12.1"
    assert host.data_gb == 50


def test_market_omitted_system_capabilities_use_conservative_defaults():
    raw = json.loads((Path(__file__).parent / "fixtures/host.json").read_text(encoding="utf-8"))
    raw.pop("system_disk_gb")
    raw.pop("max_system_expand_gb")
    host = parse_host(raw)
    assert (host.system_gb, host.max_system_expand_gb) == (30, 0)
    raw["system_disk_size"] = 60 * GIB
    raw["system_disk_expand_available_size"] = 100 * GIB
    host = parse_host(raw)
    assert (host.system_gb, host.max_system_expand_gb) == (60, 100)


def test_missing_data_capacity_still_requires_verified_preview():
    raw = json.loads((Path(__file__).parent / "fixtures/host.json").read_text(encoding="utf-8"))
    raw.pop("max_data_expand_gb")
    with pytest.raises(Pause, match="max_data_expand_gb"):
        parse_host(raw)


def actual_market_shape():
    raw = json.loads((Path(__file__).parent / "fixtures/host.json").read_text(encoding="utf-8"))
    raw["cpu_per_gpu"] = raw.pop("cpu_limit")
    raw["mem_per_gpu"] = raw.pop("mem_limit_in_byte")
    raw["gpu_memory"] = raw.pop("gpu_memory_gb") * GIB
    raw["highest_cuda_version"] = raw.pop("cuda_version")
    raw["max_instance_disk_size"] = raw.pop("data_disk_gb") * GIB
    raw["max_data_disk_expand_size"] = raw.pop("max_data_expand_gb") * GIB
    raw.pop("system_disk_gb")
    raw.pop("max_system_expand_gb")
    return raw


def test_actual_market_expansion_field_is_converted_from_whole_gib_bytes():
    raw = actual_market_shape()
    host = parse_host(raw)
    assert host.max_data_expand_gb == 1000
    assert (host.system_gb, host.max_system_expand_gb) == (30, 0)
    raw["max_data_disk_expand_size"] = 1000  # Units cannot be guessed from a small integer.
    with pytest.raises(Pause, match="不是整 GiB"):
        parse_host(raw)


def test_real_market_field_names_allow_inventory_without_preview(tmp_path):
    raw_source = actual_shape()
    raw_host = actual_market_shape()
    raw_host["machine_id"] = "0123456789"
    browser = Browser(tmp_path)
    browser.names = {}
    browser.instance = lambda _: parse_instance(raw_source)
    browser.api = SimpleNamespace(machines=lambda *args: [raw_host], snapshot=lambda _: raw_source)
    browser.sessions = SimpleNamespace(has=lambda _: True)

    def no_preview(_):
        raise AssertionError("Complete API specs must not open the clone preview")

    browser.market_host = no_preview
    source, reference, hosts = browser.inventory(Config(SOURCE, "5", "2"))
    assert reference.max_data_expand_gb == 1000
    assert source.id == SOURCE and hosts == [reference]


def test_inventory_resolves_actual_disks_after_loading_source_host(tmp_path):
    raw_source = actual_shape()
    raw_host = json.loads((Path(__file__).parent / "fixtures/host.json").read_text(encoding="utf-8"))
    raw_host.update(machine_id="0123456789", region_sign="fixture-east")
    browser = Browser(tmp_path)
    browser.names = {}
    browser.instance = lambda identifier: parse_instance(raw_source)
    browser.api = SimpleNamespace(instances=lambda: [raw_source], machines=lambda *args: [raw_host], snapshot=lambda _: raw_source)
    browser.sessions = SimpleNamespace(has=lambda _: True)
    source, reference, hosts = browser.inventory(Config(SOURCE, "5", "2"))
    assert (source.system_gb, source.data_gb, source.data_expand_gb) == (30, 10, 100)
    assert source.data_gb + source.data_expand_gb == 110
    assert reference.id == source.host_id


def test_market_fallback_error_also_saves_schema(tmp_path):
    raw_source = actual_shape()
    raw_host = {"machine_id": "0123456789", "region_sign": "fixture-east", "region_name": "测试区"}
    browser = Browser(tmp_path)
    browser.names = {}
    browser.instance = lambda identifier: parse_instance(raw_source)
    browser.api = SimpleNamespace(instances=lambda: [raw_source], machines=lambda *args: [raw_host], snapshot=lambda _: raw_source)
    browser.sessions = SimpleNamespace(has=lambda _: True)
    browser.market_host = lambda _: (_ for _ in ()).throw(KeyError("gpu_name"))
    with pytest.raises(Pause, match="schema_diagnostics.json"):
        browser.inventory(Config(SOURCE, "5", "2"))
    report = json.loads((tmp_path / "schema_diagnostics.json").read_text(encoding="utf-8"))
    assert report["context"] == "market_host"
    assert report["fields"]["machine_id"] == "str"


def test_market_preview_timeout_also_saves_missing_api_schema(tmp_path):
    raw_source = actual_shape()
    raw_host = {"machine_id": "0123456789", "region_sign": "fixture-east", "region_name": "测试区"}
    browser = Browser(tmp_path)
    browser.names = {}
    browser.instance = lambda _: parse_instance(raw_source)
    browser.api = SimpleNamespace(machines=lambda *args: [raw_host], snapshot=lambda _: raw_source)
    browser.sessions = SimpleNamespace(has=lambda _: True)
    browser.market_host = lambda _: (_ for _ in ()).throw(TimeoutError("fixture-secret"))
    with pytest.raises(Pause, match="schema_diagnostics.json") as caught:
        browser.inventory(Config(SOURCE, "5", "2"))
    assert isinstance(caught.value.__cause__, TimeoutError)
    text = (tmp_path / "schema_diagnostics.json").read_text(encoding="utf-8")
    assert json.loads(text)["context"] == "market_host"
    assert "fixture-secret" not in text + str(caught.value)


def test_zero_initial_fields_do_not_mean_zero_capacity():
    snapshot = actual_shape(system_init_disk_size=0, expand_system_disk_size=0)
    assert instance_system_disk_gb(snapshot) == 30
    assert instance_data_base_gb(snapshot) + instance_data_expansion_gb(snapshot) == 110


def test_snapshot_quota_required_even_with_apparently_usable_list_telemetry():
    raw = actual_shape()
    raw.pop("max_writable_size_in_byte")
    with pytest.raises(Pause, match="缺少容量字段"):
        instance_system_disk_gb(raw)
