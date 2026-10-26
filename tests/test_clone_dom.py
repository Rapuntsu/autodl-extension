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
    monkeypatch.setattr(adapter, "open_rent", lambda *a: None)
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


def test_delayed_table_and_fee_can_prepare_and_submit_once(clone_page, tmp_path, monkeypatch):
    mount(clone_page, expandGb=100, need=True, daily="0.00", tableDelay="later")
    clone_page.evaluate("setInterval(() => window.__cfx.tick(), 400)")
    adapter = Browser(tmp_path)
    adapter.form = clone_page
    adapter.names = {"fixture-east": "测试乙区"}
    host = Host("efgh000001", "fixture-east", "vGPU-00GB", 2, 32, 12, 62,
                "580.105.08", "13.0", "1.58", 50, 2266)
    adapter.reference = host
    source = Instance(SOURCE, "abcd123456", host.region, host.gpu, 1, "stopped", None,
                      data_expand_gb=100)
    choice = Choice("clone", host, 1, 100, 30, "1.58", "0.66")
    monkeypatch.setattr(adapter, "open_clone", lambda _: None)
    monkeypatch.setattr(adapter, "inject", lambda *args: None)
    # Queue a delayed, coherent fee update after the page has selected a host.
    clone_page.evaluate("setTimeout(() => { __cfx.type('100'); __cfx.enter(); }, 600)")
    quote = adapter.prepare(Config(SOURCE, "5", "2"), source, choice)
    assert quote["disk_daily"] == "0.66"
    assert clone_page.evaluate("window.__cfx.counts()") .get("button:创建并开机", 0) == 0
    adapter.call(clone_page, "__autodlClone", "confirmCreate", SOURCE, adapter.prepared_copy)
    from autodl_grab.errors import NotSubmitted
    # A second confirm refuses but must not claim the earlier click never happened.
    with pytest.raises(Pause) as error:
        adapter.call(clone_page, "__autodlClone", "confirmCreate", SOURCE, adapter.prepared_copy)
    assert not isinstance(error.value, NotSubmitted)
    assert clone_page.evaluate("window.__cfx.counts()") ["button:创建并开机"] == 1


def test_host_rows_arriving_after_old_stability_threshold(clone_page, tmp_path):
    mount(clone_page)
    clone_page.evaluate("""id => {
      cloneGuard.tickModel(id,'vGPU-00GB'); __cfx.set({loaded:0});
      setTimeout(() => __cfx.set({loaded:10}), 1900);
    }""", SOURCE)
    rows = Browser(tmp_path).load_host_rows(clone_page, "efgh000001", timeout_ms=3500)
    assert rows[0]["host"] == "efgh000001"


def test_scroll_loads_target_beyond_first_ten_rows(clone_page, tmp_path):
    mount(clone_page)
    clone_page.evaluate("""id => {
      cloneGuard.tickModel(id,'RTX 0000');
      setInterval(() => __cfx.frame(), 40);
    }""", SOURCE)
    rows = Browser(tmp_path).load_host_rows(clone_page, "qrst000011", timeout_ms=3000)
    assert any(r["host"] == "qrst000011" for r in rows)


@pytest.mark.parametrize("loading", [False, True])
def test_host_wait_timeout_is_bounded_and_diagnostic_is_value_free(clone_page, tmp_path, loading):
    mount(clone_page, tableDelay="later" if loading else "now", sourceRemark="private-remark")
    clone_page.evaluate("id => cloneGuard.tickModel(id,'vGPU-00GB')", SOURCE)
    with pytest.raises(Pause, match="超时"):
        Browser(tmp_path).load_host_rows(clone_page, "missing-host", timeout_ms=100)
    text = (tmp_path / "page_diagnostics.json").read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["code"] == ("host_table_loading" if loading else "host_not_loaded")
    assert report["method"] == "rawHosts"
    assert report["wait_ms"] >= 100
    assert SOURCE not in text and "private-remark" not in text and "missing-host" not in text
    assert clone_page.evaluate("window.__cfx.counts()") .get("button:创建并开机", 0) == 0


def test_final_guard_proves_no_click_and_preserves_refusal(clone_page, tmp_path):
    from autodl_grab.errors import NotSubmitted
    mount(clone_page)
    copy = clone_page.evaluate("""id => {
      cloneGuard.tickModel(id,'vGPU-00GB'); cloneGuard.pickHost(id,'efgh000001');
      return cloneGuard.prepareCreate(id,{host:'efgh000001',model:'vGPU-00GB',gpus:1,expandGb:0}).copy;
    }""", SOURCE)
    clone_page.evaluate("window.__cfx.setHost('efgh000001',{price:'1.60'})")
    with pytest.raises(NotSubmitted, match="prepared copy"):
        Browser(tmp_path).call(clone_page, "__autodlClone", "confirmCreate", SOURCE, copy)
    assert clone_page.evaluate("window.__cfx.counts()") .get("button:创建并开机", 0) == 0


def mount_rent(page):
    mount(page)
    page.evaluate("""id => {
      cloneGuard.tickModel(id,'vGPU-00GB');
      for (const card of document.querySelectorAll('.instance-info > .card')) {
        if (['源实例:','镜像:'].includes(card.querySelector('.label')?.textContent.trim())) card.remove();
      }
      const count=[...document.querySelectorAll('.filter-item')].find(e=>e.querySelector('.filter-label')?.textContent.trim()==='GPU数量:');
      count.classList.add('gpu-num');
      document.querySelector('.select-server').prepend(count);
      document.querySelector('.list-filter').remove();
      const image=document.createElement('form');image.className='card image';
      image.innerHTML='<input type="text" readonly value="Fixture / Python / CUDA">';
      document.querySelector('.create-wrap').appendChild(image);
      history.replaceState(null,'','/create/fixture-west/efgh000001/1/payg');
    }""", SOURCE)


def rent_config():
    return {"id":SOURCE, "host":"efgh000001", "model":"vGPU-00GB", "regionSign":"fixture-west",
            "region":"测试丙区", "image":"Fixture / Python / CUDA", "expandGb":0}


def test_current_rent_form_needs_no_model_filter_and_can_confirm_once(clone_page, tmp_path):
    mount_rent(clone_page)
    adapter = Browser(tmp_path)
    adapter.call(clone_page, "__autodlClone", "configureRent", rent_config())
    adapter.call(clone_page, "__autodlClone", "pickCount", SOURCE, 1)
    clone_page.evaluate("document.querySelector('.data-disk input.el-input__inner').parentElement.parentElement.remove()")
    assert adapter.call(clone_page, "__autodlClone", "expansion", SOURCE)["gb"] == 0
    quote = adapter.call(clone_page, "__autodlClone", "prepareCreate", SOURCE,
                         {"host":"efgh000001", "model":"vGPU-00GB", "gpus":1, "expandGb":0})
    assert quote["copy"]["region"] == "测试丙区"
    adapter.call(clone_page, "__autodlClone", "confirmCreate", SOURCE, quote["copy"])
    assert clone_page.evaluate("window.__cfx.counts()") ["button:创建并开机"] == 1


@pytest.mark.parametrize("changed", ["route", "image", "coupon", "billing"])
def test_rent_guard_rejects_changed_contract_without_clicking(clone_page, tmp_path, changed):
    mount_rent(clone_page)
    adapter = Browser(tmp_path)
    adapter.call(clone_page, "__autodlClone", "configureRent", rent_config())
    if changed == "route":
        clone_page.evaluate("history.replaceState(null,'','/create/fixture-east/efgh000001/1/payg')")
    elif changed == "image":
        clone_page.evaluate("document.querySelector('form.card.image input').value='other image'")
    elif changed == "coupon":
        clone_page.evaluate("document.querySelector('.coupon-box input').value='selected'")
    else:
        clone_page.evaluate("""() => {
          for (const label of document.querySelectorAll('.card.billing label')) {
            const input=label.querySelector('input'); const on=input.value==='monthly';
            input.checked=on; label.classList.toggle('is-active',on); label.setAttribute('aria-checked',String(on));
          }
        }""")
    with pytest.raises(Pause):
        adapter.call(clone_page, "__autodlClone", "page", SOURCE)
    assert clone_page.evaluate("window.__cfx.counts()") .get("button:创建并开机", 0) == 0


def test_market_uses_exact_host_card_to_open_configuration(clone_page, tmp_path, monkeypatch):
    market = '''<meta charset="utf-8"><div class="gpu-market"><div class="list-content">
      <div class="list-item"><span class="machine-id">wxyz000001</span><button onclick="throw Error('wrong host')">2卡可租</button></div>
      <div class="list-item"><span class="machine-id">efgh000001</span><button onclick="location.href='/create/fixture-west/efgh000001/1/payg'">2卡可租</button></div>
    </div></div>'''
    clone_page.route("https://www.autodl.com/**", lambda route: route.fulfill(
        content_type="text/html", body=market if route.request.url.endswith("/market/list") else '<div id="app"><div class="create-wrap">Configuration</div></div>'))
    adapter = Browser(tmp_path)
    adapter.form, adapter.names = clone_page, {"fixture-west":"测试丙区"}
    monkeypatch.setattr(adapter, "select_filter", lambda *a: None)
    images = []
    monkeypatch.setattr(adapter, "select_rent_image", images.append)
    host = Host("efgh000001", "fixture-west", "vGPU-00GB", 2, 32, 12, 62,
                "580.105.08", "13.0", "1.58", 50, 2266)
    adapter.open_rent(Choice("rent", host, 1, 0, 30, "1.58", "0", "Fixture / Python / CUDA"))
    assert clone_page.url.endswith("/create/fixture-west/efgh000001/1/payg")
    assert images == ["Fixture / Python / CUDA"]


def test_stale_native_checked_on_disabled_row_is_never_a_selection(clone_page, tmp_path):
    mount(clone_page)
    clone_page.evaluate("""id => {
      cloneGuard.tickModel(id,'RTX 0000');
      document.querySelector('label.el-radio.is-disabled input').checked=true;
    }""", SOURCE)
    rows = Browser(tmp_path).call(clone_page, "__autodlClone", "rawHosts")["rows"]
    assert all(not r["selected"] for r in rows if r["disabled"])
    assert all(not r["disabled"] for r in rows if r["selected"])


def test_selectable_radio_disagreement_must_settle_before_reading(clone_page, tmp_path):
    mount(clone_page)
    clone_page.evaluate("""id => {
      cloneGuard.tickModel(id,'vGPU-00GB');
      const input=document.querySelector('label.el-radio input'); input.checked=false;
      setTimeout(()=>input.checked=true,400);
    }""", SOURCE)
    rows = Browser(tmp_path).load_host_rows(clone_page, "efgh000001", timeout_ms=2000)
    assert rows[0]["selected"] and not rows[0]["disabled"]


def preview_want(expand=90):
    return {"host":"efgh000001", "model":"vGPU-00GB", "gpus":1, "expandGb":expand,
            "regionSign":"fixture-east", "systemExpandGb":0}


def preview_payload(expand=90, daily=600, hourly=1580):
    return {"code":"Success", "data":{"machine_id":"efgh000001", "num":1, "charge_type":"payg",
            "expand_data_disk":expand * 1024**3, "expand_system_disk":0, "region_sign":"fixture-east",
            "payg_price":hourly, "data_disk_info":{"data_disk_deal_daily_price":daily}}}


def install_preview(page, expand=90, daily="0.60", **spec):
    mount(page, expandGb=expand, need=expand > 0, daily=daily, **spec)
    page.evaluate("id => cloneGuard.tickModel(id,'vGPU-00GB')", SOURCE)
    page.add_script_tag(content=(ROOT / "src/autodl_grab/assets/quote.js").read_text(encoding="utf-8"))


def send_preview(page, payload=None, expand=90, status=200, transport="fetch"):
    payload = payload if payload is not None else preview_payload(expand)
    page.route("https://www.autodl.com/api/v1/sub_user/order/price/preview", lambda route: route.fulfill(
        status=status, content_type="application/json", headers={"access-control-allow-origin":"*"}, body=json.dumps(payload)))
    body = {"machine_id":"efgh000001", "num":1, "charge_type":"payg", "expand_data_disk":expand * 1024**3}
    page.evaluate("""async ({body,transport}) => {
      const url='https://www.autodl.com/api/v1/sub_user/order/price/preview';
      if (transport === 'xhr') {
        await new Promise(resolve => { const x=new XMLHttpRequest();x.open('POST',url);x.onloadend=resolve;x.send(JSON.stringify(body)); });
      } else { await fetch(url,{method:'POST',body:JSON.stringify(body)}); }
    }""", {"body":body, "transport":transport})
    page.wait_for_function("!window.__autodlQuote.read().pending")


@pytest.mark.parametrize("transport", ["fetch", "xhr"])
def test_platform_disk_quote_replaces_fixed_tariff(clone_page, transport):
    install_preview(clone_page)
    send_preview(clone_page, transport=transport)
    quote = clone_page.evaluate("([id,want]) => cloneGuard.prepareCreate(id,want)", [SOURCE, preview_want()])
    assert quote["ok"], quote
    assert quote["copy"]["quotedDaily"] == "0.600"
    assert clone_page.evaluate("([id,cp]) => cloneGuard.confirmCreate(id,cp)", [SOURCE, quote["copy"]])["ok"]
    assert clone_page.evaluate("window.__cfx.counts()")["button:创建并开机"] == 1


@pytest.mark.parametrize("daily", ["0", "0.0", "0.00"])
def test_equivalent_amount_formats_are_accepted(clone_page, daily):
    install_preview(clone_page, expand=0, daily=daily)
    send_preview(clone_page, preview_payload(expand=0, daily=0), expand=0)
    assert clone_page.evaluate("([id,w]) => cloneGuard.prepareCreate(id,w)", [SOURCE, preview_want(0)])["ok"]


def test_unsettled_fee_keeps_safe_diagnostic_values(clone_page):
    install_preview(clone_page, daily="0.00")
    send_preview(clone_page)
    quote = clone_page.evaluate("([id,w]) => cloneGuard.prepareCreate(id,w)", [SOURCE, preview_want()])
    assert quote["code"] == "disk_fee_pending"
    assert quote["summary"] == {"expand_gb":90, "displayed_daily":"0.00", "preview_daily":"0.600"}
    assert clone_page.evaluate("window.__cfx.counts()").get("button:创建并开机", 0) == 0


@pytest.mark.parametrize("changed", ["host", "count", "disk", "region", "system", "schema", "auth", "http", "transport"])
def test_preview_mismatch_or_failure_cannot_submit(clone_page, changed):
    install_preview(clone_page)
    payload = preview_payload()
    status = 200
    if changed == "host": payload["data"]["machine_id"] = "wxyz000001"
    if changed == "count": payload["data"]["num"] = 2
    if changed == "disk": payload["data"]["expand_data_disk"] = 100 * 1024**3
    if changed == "region": payload["data"]["region_sign"] = "other"
    if changed == "system": payload["data"]["expand_system_disk"] = 1024**3
    if changed == "schema": payload["data"]["payg_price"] = "1580"
    if changed == "auth": status = 401
    if changed == "http": status = 418
    if changed == "transport": status = 503
    send_preview(clone_page, payload, status=status)
    quote = clone_page.evaluate("([id,w]) => cloneGuard.prepareCreate(id,w)", [SOURCE, preview_want()])
    assert not quote["ok"]
    assert clone_page.evaluate("window.__cfx.counts()").get("button:创建并开机", 0) == 0


def test_new_preview_invalidates_prepared_quote_even_with_same_amount(clone_page):
    install_preview(clone_page)
    send_preview(clone_page)
    quote = clone_page.evaluate("([id,w]) => cloneGuard.prepareCreate(id,w)", [SOURCE, preview_want()])
    send_preview(clone_page)
    refused = clone_page.evaluate("([id,cp]) => cloneGuard.confirmCreate(id,cp)", [SOURCE, quote["copy"]])
    assert refused["notSubmitted"] is True and refused["code"] == "quote_changed"
    assert clone_page.evaluate("window.__cfx.counts()").get("button:创建并开机", 0) == 0


def test_current_platform_100gb_tariff_and_empty_region(clone_page):
    install_preview(clone_page, expand=100, daily="0.67", configLie="1.08")
    payload = preview_payload(100, daily=670, hourly=1080)
    payload["data"]["region_sign"] = ""
    send_preview(clone_page, payload, expand=100)
    quote = clone_page.evaluate("([id,w]) => cloneGuard.prepareCreate(id,w)", [SOURCE, preview_want(100)])
    assert quote["ok"], quote
    assert quote["copy"]["quotedDaily"] == "0.670"
    assert quote["copy"]["quotedHourly"] == "1.080"


@pytest.mark.parametrize("displayed,accepted", [("0.01",True), ("0.02",True), ("0.00",False), ("0.03",False)])
def test_half_cent_display_rounding_keeps_exact_budget_amount(clone_page, displayed, accepted):
    install_preview(clone_page, expand=3, daily=displayed)
    send_preview(clone_page, preview_payload(3, daily=15), expand=3)
    quote = clone_page.evaluate("([id,w]) => cloneGuard.prepareCreate(id,w)", [SOURCE, preview_want(3)])
    assert quote["ok"] is accepted
    if accepted:
        assert quote["copy"]["quotedDaily"] == "0.015"


def test_out_of_order_preview_cannot_replace_latest(clone_page):
    install_preview(clone_page)
    pending = []
    clone_page.route("https://www.autodl.com/api/v1/sub_user/order/price/preview", lambda route: pending.append(route))
    clone_page.evaluate("""() => {
      for (const size of [100,90]) {
        const x=new XMLHttpRequest();x.open('POST','https://www.autodl.com/api/v1/sub_user/order/price/preview');
        x.send(JSON.stringify({machine_id:'efgh000001',num:1,charge_type:'payg',expand_data_disk:size*1073741824}));
      }
    }""")
    for _ in range(100):
        if len(pending) == 2: break
        clone_page.wait_for_timeout(10)
    assert len(pending) == 2
    pending[1].fulfill(status=200, content_type="application/json", headers={"access-control-allow-origin":"*"}, body=json.dumps(preview_payload()))
    clone_page.wait_for_function("!window.__autodlQuote.read().pending")
    pending[0].fulfill(status=200, content_type="application/json", headers={"access-control-allow-origin":"*"}, body=json.dumps(preview_payload(100, 999)))
    clone_page.wait_for_timeout(50)
    quote = clone_page.evaluate("([id,w]) => cloneGuard.prepareCreate(id,w)", [SOURCE, preview_want()])
    assert quote["ok"] and quote["copy"]["quotedDaily"] == "0.600"


def test_disabled_candidate_is_unavailable_even_with_free_gpu_count(clone_page, tmp_path, monkeypatch):
    from autodl_grab.errors import NoResource
    mount(clone_page)
    clone_page.evaluate("window.__cfx.setHost('efgh000001',{locked:true})")
    adapter = Browser(tmp_path)
    adapter.form, adapter.names = clone_page, {"fixture-east":"测试乙区"}
    host = Host("efgh000001", "fixture-east", "vGPU-00GB", 2, 32, 12, 62,
                "580.105.08", "13.0", "1.58", 50, 2266)
    adapter.reference = host
    source = Instance(SOURCE,"abcd123456",host.region,host.gpu,1,"stopped",None)
    monkeypatch.setattr(adapter,"open_clone",lambda _:None)
    monkeypatch.setattr(adapter,"inject",lambda *a:None)
    with pytest.raises(NoResource):
        adapter.prepare(Config(SOURCE,"5","2"),source,Choice("clone",host,1,0,30,"1.58","0"))
    assert clone_page.evaluate("window.__cfx.counts()") .get("button:创建并开机",0) == 0
