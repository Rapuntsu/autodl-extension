from pathlib import Path
import json

import pytest

from autodl_grab.browser import Browser, field_structure, parse_instance
from autodl_grab.errors import Pause


def fixture():
    return json.loads((Path(__file__).parent / "fixtures" / "instance.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("field", ["snapshot_gpu_alias_name", "snapshot_gpu_name", "gpu_type_name", "gpu_name"])
def test_instance_gpu_name_variants(field):
    raw = fixture()
    raw.pop("gpu_name")
    raw[field] = "RTX 3090"
    assert parse_instance(raw).gpu == "RTX 3090"


def test_displayed_snapshot_alias_takes_precedence_over_internal_name():
    raw = fixture()
    raw["snapshot_gpu_alias_name"] = "RTX 3090"
    raw["snapshot_gpu_name"] = "NVIDIA GeForce RTX 3090"
    raw["gpu_name"] = "GeForce RTX 3090"
    assert parse_instance(raw).gpu == "RTX 3090"


def test_empty_alias_falls_back_to_nonempty_gpu_name():
    raw = fixture()
    raw["snapshot_gpu_alias_name"] = "  "
    assert parse_instance(raw).gpu == "RTX 3090"


@pytest.mark.parametrize("bad", [None, "", "  "])
def test_missing_gpu_still_pauses_without_inventing_a_model(bad):
    raw = fixture()
    raw["gpu_name"] = bad
    with pytest.raises(Pause, match="缺少 GPU 型号"):
        parse_instance(raw)


def test_wrong_gpu_field_type_is_not_stringified():
    raw = fixture()
    raw["snapshot_gpu_alias_name"] = {"name": "RTX 3090"}
    with pytest.raises(Pause, match="类型变化"):
        parse_instance(raw)


def test_missing_field_generates_diagnostic_without_record_values(tmp_path):
    browser = Browser(tmp_path)
    raw = fixture()
    raw.pop("gpu_name")
    raw["ssh_password"] = "fixture-secret-password"
    raw["token"] = "fixture-secret-token"
    with pytest.raises(Pause, match="schema_diagnostics.json"):
        browser.parse_instance_record(raw)
    text = (tmp_path / "schema_diagnostics.json").read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["context"] == "instance"
    assert report["fields"]["token"] == "str"
    assert "fixture-secret" not in text
    assert raw["uuid"] not in text


def test_nested_diagnostic_preserves_only_names_and_types():
    assert field_structure({"disk_info": {"size": 100, "password": "fixture-secret"},
                            "devices": [{"gpu_alias": "RTX 3090"}]}) == {
        "disk_info": {"size": "int", "password": "str"},
        "devices": {"type": "list", "item_structure": {"gpu_alias": "str"}},
    }
