from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

from autodl_grab.api import ReadAPI, envelope, retry_seconds
from autodl_grab.browser import parse_host, parse_instance
from autodl_grab.errors import NoResource, Pause, Retryable
from autodl_grab.store import Store, profile_lock


class FakeAPI(ReadAPI):
    def __init__(self, pages):
        self.responses, self.calls = pages, []

    def post(self, path, body):
        self.calls.append(body)
        return self.responses[body["page_index"] - 1]


def test_complete_pagination():
    api = FakeAPI([{"list": [{"uuid": str(i)} for i in range(50)], "total": 53},
                   {"list": [{"uuid": str(i)} for i in range(50, 53)], "total": 53}])
    assert len(api.instances()) == 53
    assert [c["page_index"] for c in api.calls] == [1, 2]


def test_pagination_without_total_full_page_then_empty():
    api = FakeAPI([{"list": [{"machine_id": str(i)} for i in range(50)]}, {"list": []}])
    assert len(api.machines(["r1"], ["GPU"])) == 50
    assert len(api.calls) == 2


@pytest.mark.parametrize("pages", [[{"list": [], "total": 1}], [{"list": [{"uuid": "1"}, {"uuid": "1"}]}], [{"list": [None]}], [{"list": [] , "total": "0"}]])
def test_incomplete_or_unknown_pagination_pauses(pages):
    with pytest.raises(Pause):
        FakeAPI(pages).instances()


def test_cross_page_overlap_restarts_entire_scan_without_merging_old_rows():
    first = [{"uuid": str(i)} for i in range(50)]
    fresh = [{"uuid": "fresh-" + str(i)} for i in range(50)]
    responses = iter([
        {"list": first, "total": 51},
        {"list": [{"uuid": "49"}, {"uuid": "stale"}], "total": 51},
        {"list": fresh, "total": 51},
        {"list": [{"uuid": "fresh-50"}], "total": 51},
    ])
    calls = []
    api = ReadAPI(None)

    def post(path, body):
        calls.append(body["page_index"])
        return next(responses)

    api.post = post
    assert api.instances() == fresh + [{"uuid": "fresh-50"}]
    assert calls == [1, 2, 1, 2]


def test_persistent_overlap_is_bounded_and_writes_value_free_diagnostics(tmp_path):
    first = [{"uuid": "fixture-secret-" + str(i)} for i in range(50)]
    api = FakeAPI([{"list": first, "total": 51}, {"list": [first[-1]], "total": 51}])
    api.diagnostics_home = tmp_path
    with pytest.raises(Pause, match="跨页 ID 重叠") as caught:
        api.instances()
    assert [call["page_index"] for call in api.calls] == [1, 2, 1, 2, 1, 2]
    text = (tmp_path / "pagination_diagnostics.json").read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["reason"] == "cross_page_overlap"
    assert report["path"] == "/api/v1/instance"
    assert report["pages"][-1]["cross_page_overlap_count"] == 1
    assert "fixture-secret" not in text + str(caught.value)


@pytest.mark.parametrize("rows,reason", [
    ([{"machine_id": "fixture-secret"}, {"machine_id": "fixture-secret"}], "duplicate_in_page"),
    ([{"password": "fixture-secret"}], "invalid_id"),
    ([{"machine_id": ["fixture-secret"]}], "invalid_id"),
])
def test_market_pagination_differentiates_missing_and_in_page_duplicate_ids(tmp_path, rows, reason):
    api = FakeAPI([{"list": rows}])
    api.diagnostics_home = tmp_path
    with pytest.raises(Pause) as caught:
        api.machines(["fixture-east"], ["GPU"])
    assert len(api.calls) == 1
    assert "/api/v1/user/machine/list" in str(caught.value)
    assert "第 1 页" in str(caught.value)
    assert "machine_id" in str(caught.value)
    text = (tmp_path / "pagination_diagnostics.json").read_text(encoding="utf-8")
    assert json.loads(text)["reason"] == reason
    assert "fixture-secret" not in text + str(caught.value)


def test_duplicate_diagnostics_describe_variant_fields_without_record_values(tmp_path):
    api = FakeAPI([{"list": [
        {"machine_id": "fixture-secret-id", "gpu_name": "fixture-secret-gpu-one", "gpu_idle_num": 1},
        {"machine_id": "fixture-secret-id", "gpu_name": "fixture-secret-gpu-two", "gpu_idle_num": 2},
    ]}])
    api.diagnostics_home = tmp_path
    with pytest.raises(Pause):
        api.machines([], [])
    text = (tmp_path / "pagination_diagnostics.json").read_text(encoding="utf-8")
    page = json.loads(text)["pages"][-1]
    assert page["repeated_id_changed_fields"] == ["gpu_idle_num", "gpu_name"]
    assert page["identical_repeat_count"] == 0
    assert "fixture-secret" not in text


def test_total_change_retries_scan_instead_of_returning_inconsistent_inventory():
    first = [{"uuid": str(i)} for i in range(50)]
    responses = iter([
        {"list": first, "total": 52},
        {"list": [{"uuid": "50"}], "total": 51},
        {"list": first, "total": 51},
        {"list": [{"uuid": "50"}], "total": 51},
    ])
    api = ReadAPI(None)
    api.post = lambda *args: next(responses)
    assert len(api.instances()) == 51


def test_disappearing_total_does_not_silently_truncate_inventory():
    api = FakeAPI([{"list": [{"uuid": str(i)} for i in range(50)], "total": 52},
                   {"list": [{"uuid": "50"}]}])
    with pytest.raises(Pause, match="分页总数均发生变化"):
        api.instances()
    assert len(api.calls) == 6


@pytest.mark.parametrize("code", ["AuthorizeFailed", "TokenExpired", "BalanceNotEnough", "Unrecognized", None])
def test_unknown_or_auth_result_pauses(code):
    with pytest.raises(Pause):
        envelope({"code": code, "msg": "sensitive"})


def test_resource_only_known_code():
    with pytest.raises(NoResource):
        envelope({"code": "GPUNotEnough"})
    with pytest.raises(Pause):
        envelope({"code": "OtherError", "msg": "GPUNotEnough"})


def test_retry_after_seconds_and_http_date():
    assert retry_seconds("120") == 120
    assert 55 < retry_seconds(format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))) <= 60
    assert retry_seconds("nonsense") is None


def test_api_429_obeys_header():
    class Response:
        status = 429
        headers = {"retry-after": "45"}
    class Request:
        def post(self, *a, **kw):
            return Response()
    class Context:
        request = Request()
    with pytest.raises(Retryable) as exc:
        ReadAPI(Context(), "not-a-token").instances()
    assert exc.value.retry_after == 45


def test_process_lock_released(tmp_path):
    with profile_lock(tmp_path):
        with pytest.raises(Pause):
            with profile_lock(tmp_path):
                pass
    with profile_lock(tmp_path):
        pass


def test_duplicate_task_start_blocked(env):
    with pytest.raises(Pause, match="未完成"):
        env.store.new(env.config)
    env.engine.run(env.task)
    assert env.store.new(env.config) != env.task


def test_cli_status_without_playwright(tmp_path):
    from autodl_grab.cli import main
    assert main(["--home", str(tmp_path), "status"]) == 0


def test_sanitized_instance_fixture():
    raw = json.loads((Path(__file__).parent / "fixtures" / "instance.json").read_text(encoding="utf-8"))
    inst = parse_instance(raw)
    assert inst.gpu_mode is True
    assert inst.copy_complete is False
    assert inst.count == 2
    raw.pop("copy_data_disk_status")
    assert parse_instance(raw).copy_complete is None


def test_sanitized_host_fixture():
    raw = json.loads((Path(__file__).parent / "fixtures" / "host.json").read_text(encoding="utf-8"))
    host = parse_host(raw)
    assert host.hourly_per_gpu == "1.25"
    assert host.vram_gb == 24
    raw.pop("driver_version")
    with pytest.raises(Pause):
        parse_host(raw)
