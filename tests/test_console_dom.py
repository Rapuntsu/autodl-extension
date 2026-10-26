"""Real headless DOM tests. All requests are intercepted with fictional data."""
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from autodl_grab.api import RequestSession
from autodl_grab.browser import Browser, CONSOLE
from autodl_grab.errors import Pause
from autodl_grab.models import Instance

playwright = pytest.importorskip("playwright.sync_api", reason="需要安装 Playwright 以运行离线控制台 DOM 测试")
TARGET = "abcd123456-1234abcd"
OTHER = "wxyz987654-9876wxyz"
FIXTURE = (Path(__file__).parent / "fixtures/console_fixtures.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chromium():
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        yield browser
        browser.close()


def fake_console(chromium, tmp_path, *, pages=None, delay=0, mirror=False, outside=False, repeat=False, compact=False):
    pages = pages if pages is not None else [[TARGET]]
    context = chromium.new_context(viewport={"width": 1600, "height": 2600})
    adapter = Browser(tmp_path)
    adapter.context, adapter.sessions = context, RequestSession()
    context.on("request", adapter.sessions.observe)
    context.on("response", adapter.sessions.observe_response)
    page = context.new_page()
    adapter.console = page
    calls = []
    options = json.dumps({"delay": delay, "mirror": mirror, "outside": outside, "pageCount": len(pages), "compact":compact})
    bootstrap = r"""
const options = OPTIONS;
function mount(rows, index) {
  window.__fx.mount({rows:rows.map(r => ({id:r.uuid, state:'已关机', gpuFree:true, spec:'RTX 0000 * 1卡'}))});
  if (options.mirror) {
    const body = document.querySelector('.el-table__body-wrapper');
    const copy = body.cloneNode(true); copy.className='el-table__fixed-body-wrapper';
    document.querySelector('.instance-table').appendChild(copy);
  }
  if (options.outside && rows.length) {
    const duplicate=document.createElement('div'); duplicate.textContent=rows[0].uuid; document.body.appendChild(duplicate);
  }
  const paginator=document.createElement('div'); paginator.className='el-pagination';
  const next=document.createElement('button'); next.className='btn-next'; next.textContent='Next';
  next.disabled = index >= options.pageCount; next.onclick=() => load(index+1);
  paginator.appendChild(next); document.body.appendChild(paginator);
}
async function load(index) {
  const result=await fetch('/api/v1/instance', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({page_index:index,page_size:10})});
  const data=await result.json(); setTimeout(() => mount(data.data.list,index), options.delay);
}
mount([],1);
if (options.compact) fetch('/api/v1/instance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_size:1})}).then(()=>load(1));
else load(1);
""".replace("OPTIONS", options)
    html = "<!doctype html><meta charset='utf-8'><body><script>" + FIXTURE + "</script><script>" + bootstrap + "</script>"
    def serve(route):
        path = urlparse(route.request.url).path
        if path == "/console/instance/list":
            route.fulfill(status=200, content_type="text/html", body=html)
        elif path == "/api/v1/instance":
            if "page_index" not in route.request.post_data_json:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"code":"Success", "data":{
                    "list":[{"uuid":TARGET, "status":"running", "req_gpu_amount":1}], "total":1}}))
                return
            index = route.request.post_data_json["page_index"]
            calls.append(index)
            rows = pages[0 if repeat else index - 1]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"code": "Success", "data": {
                "list": [{"uuid": identifier, "status": "shutdown", "req_gpu_amount": 1} for identifier in rows],
                "total": sum(map(len, pages)),
            }}))
        else:
            route.abort()
    context.route("**/*", serve)
    return adapter, page, calls, context


def test_compact_status_response_is_not_the_main_table(chromium, tmp_path):
    adapter, page, calls, context = fake_console(chromium, tmp_path, pages=[[TARGET,OTHER]], compact=True)
    try:
        assert adapter.console_row(TARGET)["id"] == TARGET
        assert calls == [1]
        assert set(adapter.sessions.profiles['/api/v1/instance']['body']) >= {'page_index','page_size'}
    finally:
        context.close()


@pytest.mark.parametrize("options", [{"delay": 600}, {"mirror": True}, {"outside": True}])
def test_exact_id_survives_render_delay_and_dom_mirrors(chromium, tmp_path, options):
    adapter, page, calls, context = fake_console(chromium, tmp_path, **options)
    try:
        result = adapter.console_row(TARGET)
        assert result["id"] == TARGET
        assert result["gpus"] == 1
        assert calls == [1]
        if options.get("mirror") or options.get("outside"):
            assert page.get_by_text(TARGET, exact=True).count() > 1
    finally:
        context.close()


def test_target_on_second_page(chromium, tmp_path):
    adapter, page, calls, context = fake_console(chromium, tmp_path, pages=[[OTHER], [TARGET]], delay=100)
    try:
        assert adapter.console_row(TARGET)["id"] == TARGET
        assert calls == [1, 2]
    finally:
        context.close()


def test_two_real_main_rows_with_same_id_still_refuse(chromium, tmp_path):
    adapter, page, calls, context = fake_console(chromium, tmp_path, pages=[[TARGET, TARGET]])
    try:
        with pytest.raises(Pause, match="多个相同完整 ID"):
            adapter.console_row(TARGET)
        report = (tmp_path / "console_diagnostics.json").read_text(encoding="utf-8")
        assert TARGET not in report
        assert json.loads(report)["pages"][0]["matches"] == 2
    finally:
        context.close()


def test_similar_id_is_not_a_match(chromium, tmp_path):
    adapter, page, calls, context = fake_console(chromium, tmp_path, pages=[["abcd123456-1234abce"]])
    try:
        with pytest.raises(Pause, match="主表未显示目标完整 ID"):
            adapter.console_row(TARGET)
        assert calls == [1]
    finally:
        context.close()


def test_broken_pagination_stops_instead_of_looping(chromium, tmp_path):
    adapter, page, calls, context = fake_console(chromium, tmp_path, pages=[[OTHER], [TARGET]], repeat=True)
    try:
        with pytest.raises(Pause, match="重复页面"):
            adapter.console_row(TARGET)
        assert calls == [1, 2]
    finally:
        context.close()


def test_power_guard_clicks_only_exact_main_row_despite_mirrors(chromium, tmp_path):
    adapter, page, calls, context = fake_console(chromium, tmp_path, pages=[[OTHER, TARGET]], mirror=True)
    try:
        adapter.console_row(TARGET)
        opened = adapter.call(page, "__autodl", "startPowerOnGpu", TARGET)
        bound = adapter.call(page, "__autodl", "bindDialog", opened["ctx"])
        assert bound["gpus"] == 1
        adapter.call(page, "__autodl", "confirm", opened["ctx"])
        counts = page.evaluate("() => window.__fx.counts()")
        assert counts["r1:开机"] == 1
        assert counts["box:确定"] == 1
        assert "r0:开机" not in counts
    finally:
        context.close()


@pytest.mark.parametrize("delayed_models", [False, True])
def test_inventory_specs_use_clone_preview_and_never_submit(chromium, tmp_path, delayed_models):
    fixture = (Path(__file__).parent / "fixtures/clone_fixtures.js").read_text(encoding="utf-8")
    delay = """const group=document.querySelector('.el-checkbox-group');
      const models=[...group.children]; group.replaceChildren();
      setTimeout(() => group.append(...models), 400);""" if delayed_models else ""
    html = "<!doctype html><meta charset='utf-8'><body><script>" + fixture + "</script><script>window.__cfx.mount({expandGb:0,need:false});" + delay + "</script>"
    context = chromium.new_context(viewport={"width": 1600, "height": 2600})
    requests, opened = [], []

    def serve(route):
        requests.append(route.request.url)
        if urlparse(route.request.url).path == "/cloneInstance":
            route.fulfill(status=200, content_type="text/html", body=html)
        else:
            route.abort()

    context.route("**/*", serve)
    adapter = Browser(tmp_path)
    adapter.form = context.new_page()
    adapter.inventory_source = Instance(TARGET, "abcd123456", "fixture-east", "RTX 0000", 1, "stopped", None)
    adapter.inventory_preview = None

    def preview(source):
        opened.append(source.id)
        adapter.form.goto("https://www.autodl.com/cloneInstance?id=" + source.id + "&copy_data_disk=1&expand_size=0")

    adapter.open_clone = preview
    try:
        raw = {"machine_id": "abcd123456", "region_sign": "fixture-east", "gpu_name": "RTX 0000"}
        source = adapter.market_host(raw)
        other = adapter.market_host({**raw, "machine_id": "wxyz000001"})
        assert source.id == "abcd123456" and source.max_data_expand_gb == 2881
        assert other.id == "wxyz000001" and other.vram_gb == 12
        assert len(opened) == 1  # Same GPU reuses the preview without ticking twice.
        assert adapter.form.evaluate("() => window.__cfx.counts()") == {"model:RTX 0000": 1}
        alternate = adapter.market_host({**raw, "machine_id": "efgh000001", "gpu_name": "vGPU-00GB"})
        assert alternate.gpu == "vGPU-00GB" and alternate.vram_gb == 32
        assert len(opened) == 2  # Another GPU needs a fresh preview for its guard.
        assert adapter.form.evaluate("() => window.__cfx.counts()") == {"model:vGPU-00GB": 1}
        assert all(urlparse(url).path == "/cloneInstance" for url in requests)
        with pytest.raises(Pause, match="其他地区"):
            adapter.market_host({**raw, "region_sign": "fixture-west"})
        assert len(opened) == 2
    finally:
        context.close()


@pytest.mark.parametrize("enabled", [True, False])
def test_clone_unlock_is_positive_readonly_menu_evidence(chromium, tmp_path, enabled):
    adapter, page, calls, context = fake_console(chromium, tmp_path)
    original = adapter.console_row

    def locate(identifier):
        row = original(identifier)
        page.evaluate("""enabled => {
          for (const item of document.querySelectorAll('li.el-dropdown-menu__item')) {
            if (item.textContent.trim()==='克隆实例新') {
              item.textContent='克隆实例新'; item.classList.toggle('is-disabled',!enabled);
            }
          }
        }""", enabled)
        return row

    adapter.console_row = locate
    try:
        source = Instance(TARGET, "abcd123456", "fixture-east", "RTX 0000", 1, "stopped", None)
        assert adapter.verify_clone_unlocked(source) is enabled
    finally:
        context.close()
