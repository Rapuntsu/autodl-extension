import json
from types import SimpleNamespace

import pytest

from autodl_grab import cli
from autodl_grab.browser import Browser
from autodl_grab.diagnostics import diagnose_error, error_report
from autodl_grab.errors import Pause


def test_report_retains_cause_and_network_code_without_message_values():
    try:
        try:
            raise RuntimeError("Page.goto: net::ERR_NETWORK_ACCESS_DENIED https://example.test/?token=fixture-secret")
        except RuntimeError as error:
            raise Pause("fixture-secret") from error
    except Pause as error:
        report = error_report(error, "console_navigation")
    assert report["network_codes"] == ["ERR_NETWORK_ACCESS_DENIED"]
    assert [item["type"] for item in report["exceptions"]] == ["Pause", "RuntimeError"]
    assert "fixture-secret" not in json.dumps(report)
    assert "example.test" not in json.dumps(report)


@pytest.mark.parametrize("error,expected", [
    (RuntimeError("net::ERR_CONNECTION_RESET fixture-secret"), "页面网络访问失败"),
    (TimeoutError("fixture-secret"), "浏览器等待超时"),
    (RuntimeError("Executable doesn't exist at fixture-secret"), "缺少 Chromium"),
    (ValueError("fixture-secret"), "ValueError"),
])
def test_diagnostic_is_actionable_and_value_free(tmp_path, error, expected):
    message = diagnose_error(tmp_path, error, "console_navigation")
    assert expected in message
    assert "error_diagnostics.json" in message
    text = (tmp_path / "error_diagnostics.json").read_text(encoding="utf-8")
    assert "fixture-secret" not in text + message
    assert json.loads(text)["stage"] == "console_navigation"


def test_unknown_network_codes_are_not_echoed(tmp_path):
    message = diagnose_error(tmp_path, RuntimeError("net::ERR_FIXTURE_SECRET"), "plan")
    assert "ERR_FIXTURE_SECRET" not in message + (tmp_path / "error_diagnostics.json").read_text()


def test_report_write_failure_preserves_original_diagnosis(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("fixture")
    message = diagnose_error(blocked, TimeoutError("fixture-secret"), "plan")
    assert "等待超时" in message and "写入失败" in message
    assert "fixture-secret" not in message


def test_cli_browser_start_failure_writes_sanitized_trace(tmp_path, monkeypatch, capsys):
    class FailingBrowser:
        stage = "console_navigation"

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("net::ERR_NETWORK_ACCESS_DENIED token=fixture-secret")

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli, "Browser", FailingBrowser)
    assert cli.main(["--home", str(tmp_path), "login"]) == 2
    captured = capsys.readouterr()
    report = json.loads((tmp_path / "error_diagnostics.json").read_text(encoding="utf-8"))
    assert report["stage"] == "console_navigation"
    assert report["exceptions"][0]["frames"][-1]["file"] == "cli.py"
    assert "ERR_NETWORK_ACCESS_DENIED" in captured.err
    assert "fixture-secret" not in captured.err + json.dumps(report)


def test_cli_plan_failure_keeps_store_empty(tmp_path, monkeypatch, capsys):
    class WorkingBrowser:
        def __init__(self, *args, **kwargs):
            self.stage = "browser_start"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def inventory(self, config):
            self.stage = "source_snapshot"
            raise ValueError("fixture-secret")

    monkeypatch.setattr(cli, "Browser", WorkingBrowser)
    monkeypatch.setattr(cli.Config, "load", lambda _: SimpleNamespace())
    assert cli.main(["--home", str(tmp_path), "plan", "-c", "unused.toml"]) == 2
    assert "source_snapshot" in capsys.readouterr().err
    with_store = cli.Store(tmp_path)
    try:
        assert with_store.all() == []
    finally:
        with_store.close()


@pytest.mark.parametrize("position", ["before", "after", "both"])
def test_cli_accepts_headed_and_home_on_either_side_of_command(tmp_path, monkeypatch, capsys, position):
    received = []

    class WorkingBrowser:
        def __init__(self, home, *, headed=False):
            received.append((home, headed))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli, "Browser", WorkingBrowser)
    monkeypatch.setattr(cli.Config, "load", lambda _: SimpleNamespace())
    monkeypatch.setattr(cli.Engine, "plan", lambda *args: {"fixture": True})
    flags = ["--headed", "--home", str(tmp_path)]
    command = ["plan", "-c", "unused.toml"]
    arguments = (flags if position in ("before", "both") else []) + command
    arguments += flags if position in ("after", "both") else []
    assert cli.main(arguments) == 0
    assert received == [(tmp_path.resolve(), True)]
    assert json.loads(capsys.readouterr().out) == {"fixture": True}


def test_failed_navigation_preserves_original_error_when_cleanup_also_fails(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    (tmp_path / "storage_state.json").write_text('{"cookies": [], "origins": []}')
    cleanup = []
    original = RuntimeError("net::ERR_CONNECTION_RESET fixture-secret")

    def goto(*args, **kwargs):
        raise original

    def close():
        cleanup.append("browser")
        raise RuntimeError("cleanup-secret")

    def stop():
        cleanup.append("runtime")
        raise RuntimeError("cleanup-secret")

    context = SimpleNamespace(on=lambda *args: None, add_init_script=lambda *args: None,
                              new_page=lambda: SimpleNamespace(goto=goto))
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=close)
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser), stop=stop)
    monkeypatch.setattr(playwright, "sync_playwright", lambda: SimpleNamespace(start=lambda: runtime))
    adapter = Browser(tmp_path)
    with pytest.raises(RuntimeError) as caught:
        adapter.__enter__()
    assert caught.value is original
    assert cleanup == ["browser", "runtime"]
    assert adapter.stage == "console_navigation"


def test_cli_wrapped_engine_error_keeps_original_diagnostics(tmp_path, monkeypatch, capsys):
    class FailingBrowser:
        stage = "session_verification"

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            try:
                raise TimeoutError("fixture-secret")
            except TimeoutError as error:
                raise Pause("任务已暂停") from error

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli, "Browser", FailingBrowser)
    assert cli.main(["--home", str(tmp_path), "login"]) == 2
    stderr = capsys.readouterr().err
    assert "任务已暂停" in stderr and "浏览器等待超时" in stderr
    assert "fixture-secret" not in stderr
