from types import SimpleNamespace

import pytest

from autodl_grab.api import BASE, ReadAPI, RequestSession, SNAPSHOT_PATH
from autodl_grab.errors import Pause

SOURCE = "0123456789-abcdefgh"


def source_session():
    session = RequestSession()
    session.observe(SimpleNamespace(url=BASE + "/api/v1/instance", method="POST", post_data_json={"page_index": 1, "page_size":10, "child_selector": "fixture"},
                                    all_headers=lambda: {"authorization": "fixture-child-token", "x-account-scope": "fixture-child"}))
    return session


def test_known_snapshot_get_uses_current_session_without_copying_list_filters():
    calls = []
    def get(url, **kw):
        calls.append((url, kw))
        return SimpleNamespace(status=200, headers={}, json=lambda: {"code": "Success", "data": {
            "uuid": SOURCE, "max_writable_size_in_byte": 30 * 1024 ** 3}})
    api = ReadAPI(SimpleNamespace(request=SimpleNamespace(get=get)), token="fixture-stale-main", session=source_session())
    assert api.snapshot(SOURCE)["max_writable_size_in_byte"] == 30 * 1024 ** 3
    assert calls[0][0] == BASE + SNAPSHOT_PATH
    assert calls[0][1]["params"] == {"instance_uuid": SOURCE}
    assert calls[0][1]["headers"]["authorization"] == "fixture-child-token"
    assert "child_selector" not in calls[0][1]["params"]


def test_snapshot_get_capture_requires_explicit_uuid_without_pagination():
    session = RequestSession()
    session.observe(SimpleNamespace(url=BASE + SNAPSHOT_PATH + "?instance_uuid=" + SOURCE, method="GET", all_headers=lambda: {"authorization": "fixture"}))
    assert session.route(SNAPSHOT_PATH) == (SNAPSHOT_PATH, "GET")
    assert session.has(SNAPSHOT_PATH)


def test_snapshot_post_is_never_captured_or_replayed():
    session = RequestSession()
    session.observe(SimpleNamespace(url=BASE + SNAPSHOT_PATH, method="POST", post_data_json={"instance_uuid": SOURCE}, all_headers=lambda: {}))
    assert not session.has(SNAPSHOT_PATH)
    assert session.route(SNAPSHOT_PATH) == (SNAPSHOT_PATH, "GET")


def test_snapshot_checks_full_id_before_sending_request():
    with pytest.raises(Pause, match="完整实例 ID"):
        ReadAPI(SimpleNamespace()).snapshot("0123456789")


def test_snapshot_refuses_response_for_different_instance():
    context = SimpleNamespace(request=SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(status=200, headers={},
                              json=lambda: {"code": "Success", "data": {"uuid": "9876543210-abcdefgh"}})))
    with pytest.raises(Pause, match="不同的实例 ID"):
        ReadAPI(context, session=source_session()).snapshot(SOURCE)


def bind_list(session, path, token):
    request = SimpleNamespace(url=BASE + path, method="POST", post_data_json={"page_index": 1, "page_size": 10},
                              all_headers=lambda: {"authorization": token})
    session.observe(request)
    session.observe_response(SimpleNamespace(request=request, status=200, headers={}, json=lambda: {
        "code": "Success", "data": {"list": [], "total": 0}}))


@pytest.mark.parametrize("list_path,snapshot_path", [
    ("/api/v1/instance", SNAPSHOT_PATH),
    ("/api/v1/sub_user/instance", "/api/v1/sub_user/instance/snapshot"),
])
def test_snapshot_follows_current_account_route(list_path, snapshot_path):
    session = RequestSession()
    bind_list(session, list_path, "fixture-current-token")
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status=200, json=lambda: {"code": "Success", "data": {"uuid": SOURCE}})

    api = ReadAPI(SimpleNamespace(request=SimpleNamespace(get=get)), token="fixture-stale-token", session=session)
    assert api.snapshot(SOURCE) == {"uuid": SOURCE}
    assert calls == [(BASE + snapshot_path, {"params": {"instance_uuid": SOURCE},
                                            "headers": {"authorization": "fixture-current-token"}, "timeout": 30000})]


@pytest.mark.parametrize("path", [SNAPSHOT_PATH, "/api/v1/sub_user/instance/snapshot"])
def test_observed_snapshot_replaces_captured_id_without_copying_extra_parameters(path):
    session = RequestSession()
    session.observe(SimpleNamespace(url=BASE + path + "?instance_uuid=" + SOURCE + "&extra=fixture-secret",
                                   method="GET", all_headers=lambda: {"authorization": "fixture-current"}))
    assert session.has(SNAPSHOT_PATH)
    assert session.route(SNAPSHOT_PATH) == (path, "GET")
    body, headers = session.parameters(SNAPSHOT_PATH, {"instance_uuid": "9876543210-abcdefgh"})
    assert body == {"instance_uuid": "9876543210-abcdefgh"}
    assert headers == {"authorization": "fixture-current"}


@pytest.mark.parametrize("path", [SNAPSHOT_PATH, "/api/v1/sub_user/instance/snapshot"])
def test_both_snapshot_routes_reject_post_and_incomplete_id(path):
    for method, identifier in [("POST", SOURCE), ("GET", "0123456789")]:
        session = RequestSession()
        session.observe(SimpleNamespace(url=BASE + path + "?instance_uuid=" + identifier,
                                       method=method, all_headers=lambda: {}))
        assert not session.has(SNAPSHOT_PATH)


@pytest.mark.parametrize("first,second", [
    ("/api/v1/instance", "/api/v1/sub_user/instance"),
    ("/api/v1/sub_user/instance", "/api/v1/instance"),
    ("/api/v1/sub_user/instance", "/api/v1/sub_user/instance"),
])
def test_account_switch_drops_stale_snapshot_and_market(first, second):
    session = RequestSession()
    bind_list(session, first, "fixture-old-token")
    old_route = session.route(SNAPSHOT_PATH)[0]
    session.observe(SimpleNamespace(url=BASE + old_route + "?instance_uuid=" + SOURCE,
                                   method="GET", all_headers=lambda: {"authorization": "fixture-old-token"}))
    session.profiles["/api/v1/user/machine/list"] = {"fixture": "old-market"}
    bind_list(session, second, "fixture-new-token")
    assert not session.has(SNAPSHOT_PATH)
    assert not session.has("/api/v1/user/machine/list")
    assert session.route(SNAPSHOT_PATH) == (second + "/snapshot", "GET")
    assert session.parameters(SNAPSHOT_PATH, {"instance_uuid": SOURCE})[1] == {"authorization": "fixture-new-token"}
    # A late snapshot request from the previous route must not rebind the session.
    session.observe(SimpleNamespace(url=BASE + old_route + "?instance_uuid=" + SOURCE,
                                   method="GET", all_headers=lambda: {"authorization": "fixture-old-token"}))
    assert not session.has(SNAPSHOT_PATH)


def test_unverified_alias_never_falls_back_to_main_snapshot():
    session = RequestSession()
    bind_list(session, "/api/v1/subaccount/instance", "fixture-child-token")
    with pytest.raises(Pause, match="详情路径尚未核实"):
        ReadAPI(SimpleNamespace(), session=session).snapshot(SOURCE)


def test_child_bad_request_reports_actual_route_and_does_not_retry_main():
    session = RequestSession()
    bind_list(session, "/api/v1/sub_user/instance", "fixture-child-token")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status=200, json=lambda: {"code": "BadRequest", "msg": "fixture-secret"})

    with pytest.raises(Pause, match="/api/v1/sub_user/instance/snapshot") as error:
        ReadAPI(SimpleNamespace(request=SimpleNamespace(get=get)), session=session).snapshot(SOURCE)
    assert calls == [BASE + "/api/v1/sub_user/instance/snapshot"]
    assert "fixture-secret" not in str(error.value)
