"""Clone source guards on a fictional page with every request intercepted."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodl_grab.browser import Browser
from autodl_grab.config import Config
from autodl_grab.errors import Pause
from autodl_grab.models import Choice, Host, Instance

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "abcd123456-1234abcd"
OTHER = "wxyz000001-1234abcd"


@pytest.fixture(scope="module")
def chromium():
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def clone_page(chromium):
    context = chromium.new_context(viewport={"width": 1600, "height": 2600})

    def serve(route):
        if route.request.is_navigation_request() and route.request.url == "https://example.com/":
            route.fulfill(status=200, content_type="text/html", body="<!doctype html><body></body>")
        else:
            route.abort()

    context.route("**/*", serve)
    page = context.new_page()
    page.goto("https://example.com/")
    page.add_script_tag(content=(ROOT / "tests/fixtures/clone_fixtures.js").read_text(encoding="utf-8"))
    page.evaluate("source => { window.cloneFactory = (0,eval)('(' + source + ')'); }",
                  (ROOT / "src/autodl_grab/assets/create.js").read_text(encoding="utf-8"))
    yield page
    context.close()


def mount(page, **spec):
    page.evaluate("""spec => {
      window.__cfx.mount({expandGb:0, need:false, ...spec});
      window.cloneGuard = window.cloneFactory('offline-test');
    }""", spec)


@pytest.mark.parametrize("remark", [None, "Example", "HiMoE&OpenVLA", "Example / second part"])
def test_source_with_optional_remark_can_prepare_and_confirm(clone_page, remark):
    spec = {} if remark is None else {"sourceRemark": remark}
    mount(clone_page, **spec)
    result = clone_page.evaluate("""id => {
      const a=window.cloneGuard;
      const page=a.page(id), model=a.tickModel(id,'vGPU-00GB'), host=a.pickHost(id,'efgh000001');
      const quote=a.prepareCreate(id,{host:'efgh000001',model:'vGPU-00GB',gpus:1,expandGb:0});
      const confirm=a.confirmCreate(id,quote.copy);
      return {page,model,host,quote,confirm,counts:window.__cfx.counts()};
    }""", SOURCE)
    for step in ("page", "model", "host", "quote", "confirm"):
        assert result[step]["ok"], result
    assert result["counts"].get("button:创建并开机") == 1


@pytest.mark.parametrize("spec", [
    {"source": OTHER},
    {"source": OTHER, "sourceRemark": "Example"},
    {"source": SOURCE + "x", "sourceRemark": "Example"},
    {"source": OTHER, "sourceRemark": SOURCE},
    {"sourceRemark": ""},
    {"sourceRemark": "   "},
    {"source": SOURCE + " trailing text"},
])
def test_invalid_source_is_rejected(clone_page, spec):
    mount(clone_page, **spec)
    result = clone_page.evaluate("id => window.cloneGuard.page(id)", SOURCE)
    assert not result["ok"]
    assert "源实例" in result["refused"]
    assert clone_page.evaluate("window.__cfx.counts()") == {}


@pytest.mark.parametrize("duplicate", ["card", "value"])
def test_duplicate_source_nodes_are_rejected(clone_page, duplicate):
    mount(clone_page, sourceRemark="Example")
    clone_page.evaluate("""kind => {
      const card=document.querySelector('.instanceID.label').parentElement;
      const node=kind==='card' ? card : card.querySelector('.value');
      node.after(node.cloneNode(true));
    }""", duplicate)
    result = clone_page.evaluate("id => window.cloneGuard.page(id)", SOURCE)
    assert not result["ok"]
    assert "源实例" in result["refused"]


def test_source_change_after_quote_blocks_final_click(clone_page):
    mount(clone_page, sourceRemark="Example")
    result = clone_page.evaluate("""([id,other]) => {
      const a=window.cloneGuard;
      a.tickModel(id,'vGPU-00GB'); a.pickHost(id,'efgh000001');
      const quote=a.prepareCreate(id,{host:'efgh000001',model:'vGPU-00GB',gpus:1,expandGb:0});
      document.querySelector('.instanceID.label').nextElementSibling.querySelector('span').textContent=other+' / Example';
      return {quote,confirm:a.confirmCreate(id,quote.copy),counts:window.__cfx.counts()};
    }""", [SOURCE, OTHER])
    assert result["quote"]["ok"], result
    assert not result["confirm"]["ok"]
    assert "源实例" in result["confirm"]["refused"]
    assert result["counts"].get("button:创建并开机", 0) == 0


@pytest.mark.parametrize("initial", ["empty", "other"])
def test_wait_for_delayed_exact_model_without_clicking(clone_page, tmp_path, initial):
    mount(clone_page)
    clone_page.evaluate("""initial => {
      const group=document.querySelector('.el-checkbox-group');
      const labels=[...group.children];
      for (const label of labels) {
        if (initial==='empty' || label.querySelector('input').value==='vGPU-00GB') label.remove();
      }
      setTimeout(() => labels.forEach(label => group.appendChild(label)), 400);
    }""", initial)
    adapter = Browser(tmp_path)
    state = adapter.wait_creation_models(clone_page, SOURCE, "vGPU-00GB", timeout_ms=3000)
    assert any(m["name"] == "vGPU-00GB" for m in state["models"])
    assert clone_page.evaluate("window.__cfx.counts()") == {}
    adapter.call(clone_page, "__autodlClone", "tickModel", SOURCE, "vGPU-00GB")
    assert clone_page.evaluate("window.__cfx.counts()") == {"model:vGPU-00GB": 1}
    assert not (tmp_path / "create_diagnostics.json").exists()


@pytest.mark.parametrize("target", ["missing-model", "vgpu-00gb", "vGPU-00GB x2"])
def test_missing_model_times_out_with_safe_diagnostic(clone_page, tmp_path, target):
    mount(clone_page, ticked=["RTX 0000"], sourceRemark="private-remark")
    adapter = Browser(tmp_path)
    adapter.stage = "clone_prepare_models"
    with pytest.raises(Pause, match="目标型号.*未出现.*create_diagnostics.json"):
        adapter.wait_creation_models(clone_page, SOURCE, target, timeout_ms=100)
    text = (tmp_path / "create_diagnostics.json").read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["target_model"] == target
    assert report["stage"] == "clone_prepare_models"
    assert report["wait_ms"] >= 100
    assert any(m["name"] == "RTX 0000" and m["ticked"] for m in report["models"])
    assert SOURCE not in text and "private-remark" not in text
    assert "expand_size" not in text
    assert clone_page.evaluate("window.__cfx.counts()") == {}


@pytest.mark.parametrize("invalid", ["source", "duplicate", "billing"])
def test_model_wait_never_retries_guard_refusals(clone_page, tmp_path, monkeypatch, invalid):
    mount(clone_page)
    if invalid == "source":
        clone_page.evaluate("document.querySelector('.instanceID.label').nextElementSibling.querySelector('span').textContent='wxyz000001-1234abcd'")
    elif invalid == "billing":
        clone_page.evaluate("window.__cfx.set({billing:'monthly'})")
    else:
        clone_page.evaluate("""() => {
          const group=document.querySelector('.el-checkbox-group');
          group.appendChild(group.firstElementChild.cloneNode(true));
        }""")
    monkeypatch.setattr(clone_page, "wait_for_timeout", lambda _: pytest.fail("Must not retry a guard refusal"))
    with pytest.raises(Pause, match="页面结构或确认内容变化"):
        Browser(tmp_path).wait_creation_models(clone_page, SOURCE, "vGPU-00GB")
    assert clone_page.evaluate("window.__cfx.counts()") == {}
    assert not (tmp_path / "create_diagnostics.json").exists()


def test_model_timeout_keeps_original_reason_if_diagnostic_write_fails(clone_page, tmp_path):
    mount(clone_page)
    blocked = tmp_path / "blocked"
    blocked.write_text("fixture", encoding="utf-8")
    with pytest.raises(Pause, match="目标型号.*未出现.*诊断写入失败"):
        Browser(blocked).wait_creation_models(clone_page, SOURCE, "missing-model", timeout_ms=0)
    assert clone_page.evaluate("window.__cfx.counts()") == {}


@pytest.mark.parametrize("default", ["model", "all"])
def test_clone_prepare_waits_before_clearing_default_selection(clone_page, tmp_path, monkeypatch, default):
    mount(clone_page, ticked=["RTX 0000"] if default == "model" else [], all=default == "all")
    clone_page.evaluate("""() => {
      const group=document.querySelector('.el-checkbox-group');
      const label=group.firstElementChild; label.remove();
      setTimeout(() => group.prepend(label), 400);
    }""")
    adapter = Browser(tmp_path)
    adapter.form = clone_page
    adapter.names = {"fixture-east": "测试乙区"}
    host = Host("efgh000001", "fixture-east", "vGPU-00GB", 2, 32, 12, 62,
                "580.105.08", "13.0", "1.58", 50, 2266)
    adapter.reference = host
    source = Instance(SOURCE, "abcd123456", host.region, host.gpu, 1, "stopped", None)
    choice = Choice("clone", host, 1, 0, 30, "1.58", "0")
    monkeypatch.setattr(adapter, "open_clone", lambda _: None)
    monkeypatch.setattr(adapter, "inject", lambda *args: None)
    quote = adapter.prepare(Config(SOURCE, "5", "2"), source, choice)
    assert quote["gpu"] == "vGPU-00GB"
    counts = clone_page.evaluate("window.__cfx.counts()")
    assert counts["model:RTX 0000" if default == "model" else "model:全部"] == 1
    assert counts["model:vGPU-00GB"] == 1
    assert counts.get("button:创建并开机", 0) == 0


def test_rent_prepare_waits_after_configuring_rent(tmp_path, monkeypatch):
    adapter = Browser(tmp_path)
    events = []
    node = SimpleNamespace(wait_for=lambda **kw: None, count=lambda: 1,
                           click=lambda: events.append("click"),
                           filter=lambda **kw: node, get_by_role=lambda *a, **kw: node)
    adapter.form = SimpleNamespace(goto=lambda *a, **kw: None, locator=lambda *a: node,
                                   get_by_text=lambda *a, **kw: node)
    adapter.names = {"fixture-west": "测试丙区"}
    host = Host("efgh000001", "fixture-west", "vGPU-00GB", 2, 32, 12, 62,
                "580.105.08", "13.0", "1.58", 50, 2266)
    choice = Choice("rent", host, 1, 0, 30, "1.58", "0", "fixture-image")
    source = SimpleNamespace(id=SOURCE)
    monkeypatch.setattr(adapter, "select_filter", lambda *a: None)
    monkeypatch.setattr(adapter, "inject", lambda *a: None)
    monkeypatch.setattr(adapter, "load_host_rows", lambda *a: [{"host": host.id, "free": 2}])
    monkeypatch.setattr(adapter, "call", lambda *a: events.append(a[2]))

    def wait(page, identifier, model):
        assert events[-1] == "configureRent"
        assert (page, identifier, model) == (adapter.form, SOURCE, host.gpu)
        assert adapter.stage == "rent_prepare_models"
        raise Pause("wait-sentinel")

    monkeypatch.setattr(adapter, "wait_creation_models", wait)
    with pytest.raises(Pause, match="wait-sentinel"):
        adapter.prepare(None, source, choice)
    assert "tickModel" not in events
