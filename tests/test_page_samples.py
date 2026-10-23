"""Exercise real DOM/layout with a temporary, offline Chrome/Edge profile.

No Playwright dependency or network is needed for this test. The fixture is
upstream's fictional page, not a capture of any user's authenticated session.
"""
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def test_creation_guard_on_sanitized_page(tmp_path):
    if os.environ.get("AUTODL_RUN_BROWSER_TESTS") != "1":
        pytest.skip("设置 AUTODL_RUN_BROWSER_TESTS=1 运行独立 Chrome/Edge 脱敏 DOM 验证")
    choices = [os.environ.get("AUTODL_TEST_BROWSER", ""),
               "C:/Program Files/Google/Chrome/Application/chrome.exe",
               "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"]
    choices += [shutil.which(name) or "" for name in ("chromium", "google-chrome", "microsoft-edge")]
    browser = next((p for p in choices if p and Path(p).is_file()), None)
    if browser is None:
        pytest.skip("需要本地 Chrome/Edge 才能验证脱敏 DOM 样本")
    root = Path(__file__).resolve().parents[1]
    fixture = (root / "tests/fixtures/clone_fixtures.js").read_text(encoding="utf-8")
    source = (root / "src/autodl_grab/assets/create.js").read_text(encoding="utf-8")
    checks = r"""
const checks = [];
function check(name, condition) { checks.push({name, ok:!!condition}); }
function fresh(spec) {
  window.__cfx.mount(spec || {expandGb:0, need:false});
  delete window.__autodlClone;
  return factory('offline-test');
}
let a = fresh();
const id = window.__cfx.SRC;
check('clone-page source validated', a.page(id).ok);
check('alternative GPU selected', a.tickModel(id, 'vGPU-00GB').ok);
check('raw host metadata read', a.rawHosts().rows[0].vramGb === 32);
check('alternative host selected', a.pickHost(id, 'efgh000001').ok);
let p = a.prepareCreate(id, {host:'efgh000001', model:'vGPU-00GB', gpus:1, expandGb:0});
check('alternative model needs no source row in filtered table', p.ok);
check('quote exposes platform fee and disk', p.ok && p.copy.price === '1.58' && p.copy.daily === '0.00' && p.copy.systemDiskGb === 30);
window.__cfx.setHost('efgh000001', {price:'1.60'});
check('changed price blocks final click', !a.confirmCreate(id, p.copy).ok);
a = fresh({expandGb:0, need:false, search:'id=' + id + '&copy_data_disk=0&expand_size=0'});
check('missing data disk consent rejected', !a.page(id).ok);
a = fresh({expandGb:0, need:false, head:['changed']});
check('changed table structure rejected', !a.rawHosts().ok);
a = fresh();
a.tickModel(id, 'vGPU-00GB'); a.pickHost(id, 'efgh000001');
p = a.prepareCreate(id, {host:'efgh000001', model:'vGPU-00GB', gpus:1, expandGb:0});
check('final UI submit accepted', a.confirmCreate(id, p.copy).ok);
check('duplicate final UI submit blocked', !a.confirmCreate(id, p.copy).ok);
const pre = document.createElement('pre'); pre.id='result';
pre.textContent = JSON.stringify(checks); document.body.replaceChildren(pre);
"""
    runner = "const factory = (0,eval)('(' + " + json.dumps(source) + " + ')');\n" + checks
    page = "<!doctype html><meta charset='utf-8'><meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'\"><body><script>" + fixture + "</script><script>try {" + runner + "} catch(e) {document.body.innerHTML='<pre id=\"result\">'+JSON.stringify({error:String(e.stack)})+'</pre>';}</script>"
    path = tmp_path / "sample.html"
    path.write_text(page, encoding="utf-8")
    completed = subprocess.run([browser, "--headless=new", "--disable-gpu", "--in-process-gpu", "--no-first-run", "--disable-extensions",
                                "--no-default-browser-check", f"--user-data-dir={tmp_path / 'profile'}",
                                "--window-size=1600,2600", "--virtual-time-budget=10000", "--dump-dom", path.as_uri()],
                               capture_output=True, timeout=45,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    match = re.search(r'<pre id="result">(.*?)</pre>', completed.stdout.decode("utf-8", "replace"), re.S)
    assert match, f"浏览器未返回样本测试结果: exit={completed.returncode}, {completed.stderr[-500:]!r}"
    results = json.loads(html.unescape(match.group(1)))
    assert isinstance(results, list), results
    assert len(results) == 11
    assert all(c["ok"] for c in results), results
