import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodl_grab.api import BASE, ReadAPI, RequestSession, envelope
from autodl_grab.browser import Browser, CONSOLE
from autodl_grab.errors import Pause


def request(path="/api/v1/instance", body=None, headers=None, method="POST", origin=BASE):
    return SimpleNamespace(url=origin + path, method=method,
                           post_data_json={"page_index": 1, "page_size":10, "child_scope": "fixture-child"} if body is None else body,
                           all_headers=lambda: headers or {"authorization": "fixture-active-child-token",
                                                         "x-account-scope": "fixture-child",
                                                         "cookie": "fixture-cookie", "content-length": "60"})


def test_observed_child_headers_and_body_are_used():
    session = RequestSession()
    session.observe(request())
    body, headers = session.parameters("/api/v1/instance", {"page_index": 2, "page_size": 50}, token="fixture-stale-main")
    assert headers["authorization"] == "fixture-active-child-token"
    assert headers["x-account-scope"] == "fixture-child"
    assert "cookie" not in headers and "content-length" not in headers
    assert body == {"page_index": 2, "page_size": 50, "child_scope": "fixture-child"}


def test_compact_status_request_cannot_bind_or_replace_list_profile():
    session = RequestSession()
    session.observe(request(body={"page_size":1}))
    assert not session.has('/api/v1/instance')
    session.observe(request())
    session.observe(request(body={"page_size":1}))
    assert session.profiles['/api/v1/instance']['body']['page_index'] == 1


def test_read_api_replays_child_scope_in_shared_context():
    session = RequestSession()
    session.observe(request())
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status=200, headers={}, json=lambda: {"code": "Success", "data": {"list": [], "total": 0}})
    api = ReadAPI(SimpleNamespace(request=SimpleNamespace(post=post)), session=session)
    assert api.instances() == []
    assert calls[0][1]["data"]["child_scope"] == "fixture-child"
    assert calls[0][1]["headers"]["x-account-scope"] == "fixture-child"


@pytest.mark.parametrize("item", [request(origin="https://evil.example"), request(origin="http://www.autodl.com"),
                                   request(origin="https://www.autodl.com.evil.example"), request(method="GET"),
                                   request(path="/api/v1/order/instance/create/payg"), request(path="/api/v1/instance?token=fixture"),
                                   request(body=["not-an-object"])])
def test_observer_never_accepts_unverified_requests(item):
    session = RequestSession()
    session.observe(item)
    assert session.profiles == {}


def test_never_mix_child_instance_session_with_main_market_token():
    session = RequestSession()
    session.observe(request())
    with pytest.raises(Pause, match="尚未捕获"):
        session.parameters("/api/v1/user/machine/list", {}, token="fixture-main")
    session.observe(request(path="/api/v1/user/machine/list"))
    _, headers = session.parameters("/api/v1/user/machine/list", {})
    assert headers["authorization"] == "fixture-active-child-token"


@pytest.mark.parametrize("code", ["PermissionDenied", "SubAccountPermissionDenied"])
def test_child_permission_error_is_actionable_without_raw_payload(code):
    with pytest.raises(Pause) as err:
        envelope({"code": code, "msg": "secret-token=must-not-leak", "data": {"password": "must-not-leak"}})
    assert code in str(err.value)
    assert "主账号" in str(err.value)
    assert "must-not-leak" not in str(err.value)


def test_unknown_error_code_is_diagnostic_but_message_is_not_logged():
    with pytest.raises(Pause, match="code=UnknownChildScope") as err:
        envelope({"code": "UnknownChildScope", "msg": "fixture-secret-token"})
    assert "fixture-secret-token" not in str(err.value)


def test_untrusted_code_is_not_echoed():
    with pytest.raises(Pause) as err:
        envelope({"code": "token=fixture-secret-token"})
    assert "fixture-secret-token" not in str(err.value)


@pytest.mark.parametrize("status,expected", [(401, "登录失效"), (403, "子账号")])
def test_http_auth_and_permission_diagnostics(status, expected):
    context = SimpleNamespace(request=SimpleNamespace(post=lambda *a, **kw: SimpleNamespace(status=status, headers={})))
    with pytest.raises(Pause, match=expected):
        ReadAPI(context, "fixture-token").instances()


def test_restore_session_storage_bound_to_saved_login(tmp_path):
    scripts = []
    browser = Browser(tmp_path)
    browser.context = SimpleNamespace(storage_state=lambda path: Path(path).write_text('{"cookies": []}', encoding="utf-8"),
                                      add_init_script=scripts.append)
    browser.save_login_state({"fixture-child-session": "fixture-session-value"})
    browser.restore_session_storage()
    assert len(scripts) == 1
    assert 'location.origin !== "https://www.autodl.com"' in scripts[0]
    assert "sessionStorage.getItem(k) === null" in scripts[0]
    (tmp_path / "storage_state.json").write_text('{"cookies": [1]}', encoding="utf-8")
    with pytest.raises(Pause, match="不匹配"):
        browser.restore_session_storage()


def test_child_login_uses_official_entry_and_observed_auth(tmp_path, monkeypatch):
    browser = Browser(tmp_path, login=True, sub_account=True)
    browser.sessions = RequestSession()
    urls, saved, verified = [], [], []
    browser.console = SimpleNamespace(goto=lambda url, **kw: urls.append(url), evaluate=lambda _: {"fixture": "session"})
    browser.refresh_token = lambda: None
    browser.api = SimpleNamespace(instances=lambda: verified.append(True))
    browser.save_login_state = saved.append
    monkeypatch.setattr("builtins.input", lambda: "")
    browser.login()
    assert urls == [BASE + "/subAccountLogin", CONSOLE]
    assert verified == [True] and saved == [{"fixture": "session"}]


def test_refresh_session_needs_no_guessed_token_storage_key(tmp_path):
    browser = Browser(tmp_path)
    browser.sessions = RequestSession()
    browser.sessions.observe(request())
    browser.context = SimpleNamespace()
    # No evaluate() is provided: refresh must use the captured browser request.
    browser.console = SimpleNamespace(url=CONSOLE)
    browser.refresh_token()
    assert browser.api.token is None
    assert browser.api.session is browser.sessions


def test_old_profile_without_session_storage_still_loads(tmp_path):
    browser = Browser(tmp_path)
    browser.restore_session_storage()


def test_unadapted_api_is_never_called():
    api = ReadAPI(SimpleNamespace(), "fixture-token")
    with pytest.raises(Pause, match="未适配接口"):
        api.post("/api/v1/order/instance/create/payg", {})


def response_for(item, *, rows=None, code="Success", status=200):
    rows = [{"uuid": "fixture-instance", "status": "shutdown", "req_gpu_amount": 1}] if rows is None else rows
    return SimpleNamespace(request=item, status=status,
                           json=lambda: {"code": code, "msg": "fixture-secret-response",
                                         "data": {"list": rows, "total": len(rows)}})


def test_observed_child_list_alias_requires_successful_response():
    session = RequestSession()
    item = request(path="/api/v1/sub_account/instance/list", body={"page": 1, "pageSize": 10, "child_scope": "fixture"})
    session.observe(item)
    assert not session.has("/api/v1/instance")
    session.observe_response(response_for(item))
    assert session.route("/api/v1/instance") == ("/api/v1/sub_account/instance/list", "POST")
    body, _ = session.parameters("/api/v1/instance", {"page_index": 2, "page_size": 50})
    assert body == {"page": 2, "pageSize": 50, "child_scope": "fixture"}


def test_get_list_alias_is_replayed_as_get_with_original_account_scope():
    session = RequestSession()
    item = request(path="/api/v1/subaccount/instance?page_index=1&page_size=10&child_scope=fixture-child", method="GET")
    session.observe_response(response_for(item))
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status=200, headers={}, json=lambda: {"code": "Success", "data": {"list": [], "total": 0}})
    api = ReadAPI(SimpleNamespace(request=SimpleNamespace(get=get)), session=session)
    assert api.instances() == []
    assert calls[0][0] == BASE + "/api/v1/subaccount/instance"
    assert calls[0][1]["params"]["child_scope"] == "fixture-child"
    assert calls[0][1]["params"]["status"] == "[]"


@pytest.mark.parametrize("path", ["/api/v1/order/instance", "/api/v1/instance/create", "/api/v1/instance/start", "/api/v1/clone/instance/list"])
def test_even_list_shaped_mutation_response_is_not_bound(path):
    session = RequestSession()
    item = request(path=path, body={"page_index": 1, "page_size": 10})
    session.observe_response(response_for(item))
    assert not session.has("/api/v1/instance")


@pytest.mark.parametrize("rows,code", [([], "PermissionDenied"), ([{"uuid": "only-id"}], "Success"), ([], {"invalid": "code"})])
def test_unconfirmed_alias_is_not_bound(rows, code):
    session = RequestSession()
    item = request(path="/api/v1/sub_account/instance/list", body={"page_index": 1, "page_size": 10})
    session.observe_response(response_for(item, rows=rows, code=code))
    assert not session.has("/api/v1/instance")


def test_timeout_writes_only_sanitized_route_and_schema_metadata(tmp_path):
    browser = Browser(tmp_path)
    browser.sessions = RequestSession()
    item = request(path="/api/v1/unrecognized/list?token=fixture-secret-query")
    browser.sessions.observe(item)
    browser.sessions.observe_response(response_for(item))
    page = SimpleNamespace(url=CONSOLE + "?account=fixture-secret-account", wait_for_timeout=lambda _: None)
    with pytest.raises(Pause, match="脱敏接口诊断"):
        browser.wait_session(page, "/api/v1/instance")
    text = (tmp_path / "login_diagnostics.json").read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["page_path"] == "/console/instance/list"
    assert report["requests"][-1]["row_keys"] == ["req_gpu_amount", "status", "uuid"]
    assert "fixture-secret" not in text
    assert "fixture-active-child-token" not in text
    assert "fixture-child" not in text


def test_timeout_records_no_requests_instead_of_claiming_permission_failure(tmp_path):
    browser = Browser(tmp_path)
    browser.sessions = RequestSession()
    page = SimpleNamespace(url=CONSOLE, wait_for_timeout=lambda _: None)
    with pytest.raises(Pause, match="脱敏接口诊断"):
        browser.wait_session(page, "/api/v1/instance")
    assert json.loads((tmp_path / "login_diagnostics.json").read_text(encoding="utf-8"))["requests"] == []
