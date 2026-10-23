"""Playwright adapter. All paid effects are final UI clicks, never API POSTs.

The pinned upstream console guard checks complete IDs, the dialog text and the
other rows. The adapted creation guard reads every selected spec and fee again
inside the final click. Unknown schemas intentionally stop the task.
"""
import json
import hashlib
from pathlib import Path
import re
import time
from contextlib import suppress
from urllib.parse import urlparse

from .api import BASE, ReadAPI, RequestSession, envelope, schema_keys
from .config import money
from .errors import CloneExhausted, NoResource, Pause, Retryable
from .models import Host, Instance, Requirements

CONSOLE = BASE + "/console/instance/list"
ASSETS = Path(__file__).parent / "assets"
STATES = {"running": "running", "shutdown": "stopped", "stopped": "stopped", "starting": "starting",
          "stopping": "stopping", "creating": "creating", "cloning": "cloning"}
UI_STATES = {"运行中": "running", "已关机": "stopped", "开机中": "starting", "关机中": "stopping", "克隆中": "cloning", "创建中": "creating"}


def required(raw, *names):
    for name in names:
        if raw.get(name) is not None:
            return raw[name]
    raise Pause(f"网页接口缺少必要字段 {names[0]}，需要核对适配器")


def integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise Pause("网页接口数字格式变化")
    return int(value)


def gib(raw, gb_names, byte_names):
    for key in gb_names:
        if raw.get(key) is not None:
            return integer(raw[key])
    for key in byte_names:
        if raw.get(key) is not None:
            value = integer(raw[key])
            if value % (1024 ** 3):
                raise Pause("磁盘或内存不是整 GiB，不能推断容量")
            return value // 1024 ** 3
    raise Pause(f"网页接口缺少容量字段 {gb_names[0]}")


def parse_instance(raw):
    identifier = str(required(raw, "uuid"))
    if not re.fullmatch(r"[a-z0-9]{10}-[a-z0-9]{8}", identifier):
        raise Pause("平台实例 ID 格式变化")
    state = STATES.get(raw.get("status"), "unknown")
    gpu_mode = raw.get("gpu_mode") if type(raw.get("gpu_mode")) is bool else None
    if type(raw.get("is_no_gpu")) is bool:
        gpu_mode = not raw["is_no_gpu"]
    copy = raw.get("copy_data_disk_status")
    complete = True if copy == "completed" else False if copy in ("pending", "copying", "failed") else None
    locked = raw.get("clone_locked") if type(raw.get("clone_locked")) is bool else None
    return Instance(identifier, str(raw.get("machine_id") or identifier.split("-")[0]),
                    str(required(raw, "region_sign")), instance_gpu_name(raw),
                    integer(required(raw, "req_gpu_amount")), state, gpu_mode,
                    copy_complete=complete, clone_locked=locked, source_id=raw.get("clone_source_uuid"))


def instance_gpu_name(raw):
    # Instance lists store the creation-time GPU under snapshot_* fields.
    # The displayed alias is also what the market filter and console row use.
    # See upstream kuake commands/instances.py and commands/clone.py.
    names = ("snapshot_gpu_alias_name", "gpu_name", "gpu_type_name", "snapshot_gpu_name")
    for name in names:
        value = raw.get(name)
        if value is None or isinstance(value, str) and not value.strip():
            continue
        if not isinstance(value, str):
            raise Pause(f"网页实例 GPU 字段 {name} 类型变化，需要核对适配器")
        return value.strip()
    raise Pause("网页实例缺少 GPU 型号（snapshot_gpu_alias_name / gpu_name / gpu_type_name / snapshot_gpu_name）")


def field_structure(value, depth=3):
    """Field names and types only: never retain actual instance/account values."""
    if depth and isinstance(value, dict):
        return {key: field_structure(value[key], depth - 1) for key in schema_keys(value, limit=200)}
    if depth and isinstance(value, list):
        return {"type": "list", "item_structure": field_structure(value[0], depth - 1) if value else None}
    return type(value).__name__


def instance_system_disk_gb(raw):
    """The exact quota displayed by the reference frontend's detail panel."""
    if raw.get("max_writable_size_in_byte") is not None:
        total = gib(raw, (), ("max_writable_size_in_byte",))
        if any(raw.get(k) is not None for k in ("system_disk_gb", "system_disk_size")):
            if total != gib(raw, ("system_disk_gb",), ("system_disk_size",)):
                raise Pause("源实例系统盘容量字段不一致")
    else:
        total = gib(raw, ("system_disk_gb",), ("system_disk_size",))
    if total <= 0:
        raise Pause("实例详情中的系统盘配额无效")
    return total


def instance_data_expansion_gb(raw):
    return gib(raw, ("data_expand_gb",), ("expand_data_disk_size", "expand_data_disk"))


def instance_data_base_gb(raw, source_host=None):
    machine = raw.get("machine_info_snapshot")
    if isinstance(machine, dict) and machine.get("max_instance_disk_size") is not None:
        return gib(machine, (), ("max_instance_disk_size",))
    if any(raw.get(k) is not None for k in ("data_disk_gb", "data_disk_size")):
        return gib(raw, ("data_disk_gb",), ("data_disk_size",))
    # Regular instance snapshots omit the free data quota. Read it from the
    # source host's market spec, not usage_info (possibly stale while stopped).
    if source_host is None or source_host.data_gb <= 0:
        raise Pause("源主机免费数据盘配额未知")
    return source_host.data_gb


def parse_host(raw):
    return Host(str(required(raw, "machine_id")), str(required(raw, "region_sign")), str(required(raw, "gpu_name")),
                integer(required(raw, "gpu_idle_num")),
                gib(raw, ("gpu_memory_gb", "vram_gb"), ("gpu_memory", "gpu_memory_in_byte")),
                integer(required(raw, "cpu_per_gpu", "cpu_limit")), gib(raw, ("memory_gb",), ("mem_per_gpu", "mem_limit_in_byte")),
                str(required(raw, "driver_version", "driver")), str(required(raw, "highest_cuda_version", "cuda_version", "cuda")),
                str(money(required(raw, "payg_price")) / 1000),
                gib(raw, ("data_disk_gb",), ("max_instance_disk_size", "data_disk_size")),
                gib(raw, ("max_data_expand_gb",), ("max_data_disk_expand_size", "disk_expand_available_size")),
                gib(raw, ("system_disk_gb",), ("system_disk_size",))
                if any(raw.get(k) is not None for k in ("system_disk_gb", "system_disk_size")) else 30,
                gib(raw, ("max_system_expand_gb",), ("system_disk_expand_available_size",))
                if any(raw.get(k) is not None for k in ("max_system_expand_gb", "system_disk_expand_available_size")) else 0)


class Browser:
    def __init__(self, home, headed=False, login=False, sub_account=False):
        self.home, self.headed, self.is_login = Path(home), headed, login
        self.sub_account = sub_account
        self.stage = "browser_start"

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Pause("请先安装项目依赖及浏览器: pip install -e . ; python -m playwright install chromium") from None
        self.home.mkdir(parents=True, exist_ok=True)
        state = self.home / "storage_state.json"
        if not self.is_login and not state.exists():
            raise Pause("缺少登录状态，请运行 autodl-grab login")
        self.stage = "browser_start"
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(headless=not (self.headed or self.is_login))
            self.context = self.browser.new_context(storage_state=str(state) if state.exists() and not self.is_login else None,
                                                     viewport={"width": 1600, "height": 2600})
            self.sessions = RequestSession()
            self.context.on("request", self.sessions.observe)
            self.context.on("response", self.sessions.observe_response)
            if not self.is_login:
                self.stage = "session_restore"
                self.restore_session_storage()
            self.console = self.context.new_page()
            self.form = self.context.new_page()
            self.names = {}
            if not self.is_login:
                self.stage = "console_navigation"
                self.console.goto(CONSOLE, wait_until="domcontentloaded")
                self.stage = "session_verification"
                self.refresh_token()
            return self
        except BaseException:
            if hasattr(self, "browser"):
                with suppress(Exception):
                    self.browser.close()
            with suppress(Exception):
                self.playwright.stop()
            raise

    def __exit__(self, *_):
        try:
            self.browser.close()
        finally:
            self.playwright.stop()

    def refresh_token(self):
        # Keep the method name for existing callers; authentication now follows
        # the real page request, without guessing a main/child storage key.
        parsed = urlparse(self.console.url)
        if parsed.scheme != "https" or parsed.netloc != "www.autodl.com":
            raise Pause("登录页面已跳转，请重新 login")
        self.wait_session(self.console, "/api/v1/instance")
        self.api = ReadAPI(self.context, session=self.sessions, diagnostics_home=self.home)

    def wait_session(self, page, path):
        for _ in range(150):
            if self.sessions.has(path):
                return
            if "login" in urlparse(page.url).path.lower():
                raise Pause("网页仍处于登录页；子账号可使用 autodl-grab login --sub-account")
            page.wait_for_timeout(100)
        report = self.home / "login_diagnostics.json"
        report.write_text(json.dumps({"page_path": self.diagnostic_page_path(page.url),
                                      "expected_api": path, "requests": self.sessions.diagnostics},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        raise Pause(f"未捕获网页只读请求 {path}；已生成脱敏接口诊断: {report}（不含请求头、参数或响应值）。请提供此文件内容以核对实际接口")

    @staticmethod
    def diagnostic_page_path(url):
        # Do not include account-switch arguments, fragments or redirected hosts.
        path = urlparse(url).path
        known = {"", "console", "instance", "list", "market", "login", "subAccountLogin"}
        return "/".join(p if p in known else "[redacted]" for p in path.split("/"))

    def restore_session_storage(self):
        path = self.home / "session_storage.json"
        if not path.exists():
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            fingerprint = hashlib.sha256((self.home / "storage_state.json").read_bytes()).hexdigest()
        except (OSError, ValueError):
            raise Pause("保存的浏览器会话损坏，请重新 login") from None
        if not isinstance(saved, dict) or saved.get("storage_sha256") != fingerprint:
            raise Pause("登录状态与浏览器会话不匹配，请重新 login")
        values = saved.get("values")
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise Pause("保存的浏览器会话结构无效，请重新 login")
        self.install_session_storage(values)

    def install_session_storage(self, values):
        # storage_state omits sessionStorage. Restore only on the exact AutoDL
        # origin and never overwrite an already refreshed value in the tab.
        self.context.add_init_script("(() => { if(location.origin !== " + json.dumps(BASE)
                                     + ") return; const saved = " + json.dumps(values)
                                     + "; for(const [k,v] of Object.entries(saved)) {"
                                     + "if(sessionStorage.getItem(k) === null) sessionStorage.setItem(k,v); } })();")

    def login(self):
        self.stage = "login_navigation"
        self.console.goto(BASE + ("/subAccountLogin" if self.sub_account else "/login"), wait_until="domcontentloaded")
        print("请在打开的独立 Chromium 窗口登录 AutoDL，完成后在终端按 Enter。")
        input()
        self.stage = "login_verification"
        if not self.sessions.has("/api/v1/instance"):
            self.console.goto(CONSOLE, wait_until="domcontentloaded")
        self.refresh_token()
        self.api.instances()  # Verify a real authenticated, read-only endpoint.
        values = self.console.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
        self.save_login_state(values)

    def save_login_state(self, values):
        session_path = self.home / "session_storage.json"
        session_temp = self.home / "session_storage.tmp"
        path = self.home / "storage_state.json"
        temp = self.home / "storage_state.tmp"
        self.context.storage_state(path=str(temp))
        temp.chmod(0o600)
        saved = {"storage_sha256": hashlib.sha256(temp.read_bytes()).hexdigest(), "values": values}
        session_temp.write_text(json.dumps(saved), encoding="utf-8")
        session_temp.chmod(0o600)
        temp.replace(path)
        session_temp.replace(session_path)

    def inject(self, page, filename, object_name):
        script = (ASSETS / filename).read_text(encoding="utf-8-sig")
        page.evaluate("code => { const fn = (0,eval)('(' + code + ')'); fn(); }", script)
        return object_name

    def call(self, page, obj, method, *args):
        for _ in range(40):
            result = page.evaluate("([obj,method,args]) => window[obj][method](...args)", [obj, method, args])
            if isinstance(result, dict) and result.get("ok") is True:
                return result
            if isinstance(result, dict) and result.get("pending") is True:
                page.wait_for_timeout(250)
                continue
            raise Pause("页面结构或确认内容变化: " + str((result or {}).get("refused", (result or {}).get("why", "未知页面返回")))[:180])
        raise Pause("页面仍未稳定")

    def console_row(self, identifier, page=None):
        page = page or self.console
        history = []
        try:
            expected_ids = self.console_response(page, lambda: page.goto(CONSOLE, wait_until="domcontentloaded"))
            seen_pages = set()
            for number in range(1, 1001):
                entry = {"page": number, "api_rows": len(expected_ids), "api_contains_target": identifier in expected_ids}
                history.append(entry)
                signature = tuple(sorted(expected_ids))
                if signature in seen_pages:
                    raise Pause("控制台翻页返回了重复页面，已停止定位")
                seen_pages.add(signature)
                self.inject(page, "console.js", "__autodl")
                for _ in range(150):
                    status = self.call(page, "__autodl", "locate", identifier, expected_ids)
                    entry.update(status)
                    if status.get("login"):
                        raise Pause("控制台跳转到登录页，请重新 login")
                    if status.get("ready"):
                        break
                    page.wait_for_timeout(100)
                else:
                    raise Pause("控制台表格未完成渲染，或页面行结构与接口不一致")
                if status["matches"] > 1:
                    raise Pause("控制台主表中存在多个相同完整 ID 的实例行，已停止定位")
                if status["matches"] == 1:
                    return self.call(page, "__autodl", "row", identifier)
                nxt = page.locator(".el-pagination:visible .btn-next:visible")
                entry["next_buttons"] = nxt.count()
                if nxt.count() == 0:
                    break
                if nxt.count() != 1:
                    raise Pause("控制台分页按钮不唯一，需核对页面结构")
                entry["next_enabled"] = nxt.is_enabled()
                if not entry["next_enabled"]:
                    break
                expected_ids = self.console_response(page, nxt.click)
            raise Pause("已核对控制台已加载的实例页面，但主表未显示目标完整 ID；请检查列表筛选或页面结构")
        except Pause as error:
            report = self.home / "console_diagnostics.json"
            report.write_text(json.dumps({"page_path": self.diagnostic_page_path(page.url), "pages": history},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
            raise Pause(f"{error}；已保存脱敏页面诊断: {report}（不含实例 ID 或页面原文）") from None

    def console_response(self, page, action):
        """Wait for the actual instance response, then let locate() await DOM alignment."""
        path, method = self.sessions.route("/api/v1/instance")
        def is_instance_response(response):
            parsed = urlparse(response.url)
            return parsed.scheme == "https" and parsed.netloc == "www.autodl.com" and parsed.path == path and response.request.method == method
        try:
            with page.expect_response(is_instance_response, timeout=20000) as pending:
                action()
            response = pending.value
        except Exception:
            raise Pause("控制台未收到实例列表刷新响应，无法确认页面已加载") from None
        if response.status != 200:
            raise Pause(f"控制台实例列表刷新失败（HTTP {response.status}）")
        try:
            payload = response.json()
        except Exception:
            raise Pause("控制台实例刷新未返回 JSON") from None
        data = envelope(payload)
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("uuid"), str) for r in rows):
            raise Pause("控制台实例刷新返回结构变化")
        return [r["uuid"] for r in rows]

    def instances(self):
        return [self.parse_instance_record(raw) for raw in self.api.instances()]

    def schema_pause(self, context, raw, error):
        report = self.home / "schema_diagnostics.json"
        report.write_text(json.dumps({"context": context, "fields": field_structure(raw)},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        return Pause(f"{error}；已保存脱敏字段结构: {report}（仅字段名和类型，不含字段值）")

    def parse_instance_record(self, raw):
        try:
            return parse_instance(raw)
        except Pause as error:
            raise self.schema_pause("instance", raw, error) from None

    def instance(self, identifier):
        raw = [r for r in self.api.instances() if r.get("uuid") == identifier]
        if len(raw) != 1:
            raise Pause("远端未找到唯一完整实例 ID")
        instance = self.parse_instance_record(raw[0])
        row = self.console_row(identifier)
        instance.state = UI_STATES.get(row["state"], "unknown")
        instance.gpu_mode = row["mode"] == "gpu" if row["mode"] in ("gpu", "nogpu") else None
        if row["gpus"] != instance.count or row["spec"] != f"{instance.gpu} * {instance.count}卡":
            # Whitespace is normalized, but GPU names and card counts are exact.
            if re.sub(r"\s+", "", str(row["spec"])) != re.sub(r"\s+", "", f"{instance.gpu}*{instance.count}卡"):
                raise Pause("实例页面与接口 GPU 规格不一致")
        # Never infer copy completion from running, lack of a toast, or lack of a lock.
        if raw[0].get("copy_data_disk_status") == "failed":
            raise Pause("平台报告数据盘复制失败，目标保留供检查")
        return instance

    def can_start(self, source):
        if source.state != "stopped":
            return False
        return self.console_row(source.id)["gpuFree"] is True

    def inventory(self, config):
        self.stage = "source_lookup"
        source = self.instance(config.source_id)
        self.stage = "source_snapshot"
        snapshot = self.api.snapshot(source.id)
        try:
            source.system_gb = instance_system_disk_gb(snapshot)
            source.data_expand_gb = instance_data_expansion_gb(snapshot)
            source.data_gb = instance_data_base_gb(snapshot)
        except Pause as error:
            raise self.schema_pause("source_snapshot_disks", snapshot, error) from None
        gpus = list(dict.fromkeys([source.gpu, *config.alternate_gpus]))
        regions = list(dict.fromkeys([source.region, *config.other_regions]))
        self.stage = "market_inventory"
        if not self.sessions.has("/api/v1/user/machine/list"):
            self.form.goto(BASE + "/market/list", wait_until="domcontentloaded")
            self.wait_session(self.form, "/api/v1/user/machine/list")
        raw_hosts = self.api.machines(regions, gpus)
        self.inventory_source = source
        self.inventory_preview = None
        hosts = []
        for raw in raw_hosts:
            region = str(required(raw, "region_sign"))
            self.names[region] = str(required(raw, "region_name"))
            try:
                hosts.append(parse_host(raw))
            except Pause as parse_error:
                # Missing API fields can be read from the verified clone preview,
                # whose nine-column table is different from the market listing.
                try:
                    host = self.market_host(raw)
                except (Pause, KeyError) as error:
                    message = error if isinstance(error, Pause) else Pause("市场接口缺少页面筛选所需字段")
                    raise self.schema_pause("market_host", raw, message) from None
                except (Retryable, KeyboardInterrupt):
                    raise
                except Exception as error:
                    raise self.schema_pause("market_host", raw,
                                            Pause(f"{parse_error}；只读主机规格预览失败")) from error
                hosts.append(host)
        refs = [h for h in hosts if h.id == source.host_id]
        if len(refs) != 1:
            raise Pause("完整库存未找到源主机规格，无法继承要求")
        self.reference = refs[0]
        return source, refs[0], hosts

    def select_filter(self, page, label, text):
        line = page.locator(".filter-item").filter(has=page.locator(".filter-label", has_text=label))
        if line.count() != 1:
            raise Pause("页面筛选项结构变化")
        target = line.get_by_text(text, exact=True)
        if target.count() != 1:
            raise Pause("页面筛选值无法唯一识别")
        target.click()
        page.wait_for_timeout(500)

    def market_host(self, raw):
        source = self.inventory_source
        if raw["region_sign"] != source.region:
            raise Pause("其他地区市场接口缺少必要规格；当前已核实的克隆预览只支持源实例同区，需补充市场字段适配")
        if source.state != "stopped":
            raise Pause("市场接口缺少必要规格，源实例未关机，无法打开只读克隆规格预览")
        self.stage = "clone_preview_navigation"
        preview_key = (source.id, raw["gpu_name"])
        if self.inventory_preview != preview_key:
            try:
                self.open_clone(source)
            except CloneExhausted:
                raise Pause("源实例已无可用克隆次数，无法打开只读主机规格预览") from None
            self.form.locator("#app > .create-wrap .select-server .machine-table").wait_for(state="visible", timeout=15000)
            self.inject(self.form, "create.js", "__autodlClone")
            self.call(self.form, "__autodlClone", "tickModel", source.id, raw["gpu_name"])
            self.inventory_preview = preview_key
        self.stage = "clone_preview_specs"
        rows = self.load_host_rows(self.form, raw["machine_id"])
        matches = [r for r in rows if r["host"] == raw["machine_id"]]
        if len(matches) != 1:
            raise Pause("市场页面缺少源主机或候选主机，不能推断配置")
        r = matches[0]
        # This table does not advertise system-disk expansion capacity; use the
        # platform default unless the API explicitly advertises a larger limit.
        return Host(r["host"], raw["region_sign"], r["model"], r["free"], r["vramGb"], r["cpu"], r["memGb"],
                    r["driver"], r["cuda"], r["price"], r["diskGb"], r["expandGb"],
                    gib(raw, ("system_disk_gb",), ("system_disk_size",))
                    if any(raw.get(k) is not None for k in ("system_disk_gb", "system_disk_size")) else 30,
                    gib(raw, ("max_system_expand_gb",), ("system_disk_expand_available_size",))
                    if any(raw.get(k) is not None for k in ("max_system_expand_gb", "system_disk_expand_available_size")) else 0)

    def load_host_rows(self, page, wanted):
        # Stop only once the wanted host is loaded. API pagination already
        # supplies the complete candidate set; scrolling is for rechecking a row.
        previous, stable = -1, 0
        for _ in range(1000):
            rows = self.call(page, "__autodlClone", "rawHosts")["rows"]
            if any(r["host"] == wanted for r in rows):
                return rows
            stable = stable + 1 if len(rows) == previous else 0
            if stable >= 5:
                return rows
            previous = len(rows)
            page.locator(".machine-table .el-table__body-wrapper").evaluate("e => {e.scrollTop=e.scrollHeight}")
            page.wait_for_timeout(300)
        raise Pause("页面主机表未完成加载")

    def open_clone(self, source):
        row = self.console_row(source.id, self.form)
        if row["state"] != "已关机":
            raise Pause("克隆前源实例未关机")
        for _ in range(20):
            result = self.call(self.form, "__autodl", "menu", source.id)
            if result["open"]:
                break
            self.form.wait_for_timeout(250)
        ctx = self.call(self.form, "__autodl", "startClone", source.id)["ctx"]
        dialog = self.call(self.form, "__autodl", "bindCloneDialog", ctx)
        if dialog["remaining"] == 0:
            self.call(self.form, "__autodl", "dismiss", ctx)
            raise CloneExhausted()
        self.call(self.form, "__autodl", "tickCloneDataDisk", ctx)
        dialog = self.call(self.form, "__autodl", "bindCloneDialog", ctx)
        if not dialog["dataDisk"] or dialog["expandGb"] != source.data_expand_gb:
            raise Pause("克隆对话框的数据盘或所需扩容与源实例不一致")
        self.call(self.form, "__autodl", "continueClone", ctx)
        self.form.wait_for_url("**/cloneInstance?**", timeout=15000)

    def prepare(self, config, source, choice):
        if choice.kind == "clone":
            self.open_clone(source)
        else:
            self.form.goto(BASE + "/market/list", wait_until="domcontentloaded")
            self.form.locator(".machine-table").wait_for(state="visible", timeout=15000)
            self.select_filter(self.form, "选择地区:", self.names[choice.host.region])
            self.select_filter(self.form, "GPU型号:", choice.host.gpu)
            self.inject(self.form, "create.js", "__autodlClone")
            rows = self.load_host_rows(self.form, choice.host.id)
            if not any(r["host"] == choice.host.id and r["free"] >= choice.count for r in rows):
                raise NoResource()
            # The market row must name the complete host ID. Never choose by index.
            row = self.form.locator(".el-table__body tr").filter(has=self.form.get_by_text(choice.host.id, exact=True))
            if row.count() != 1:
                raise Pause("市场候选主机行不唯一")
            row.get_by_role("button", name="立即购买", exact=True).click()
            self.form.locator("#app > .create-wrap").wait_for(state="visible", timeout=15000)
            image = self.form.get_by_text(choice.image, exact=True)
            if image.count() != 1:
                raise Pause("新租页面未找到唯一指定镜像，请核对镜像选择器")
            image.click()
        self.form.locator("#app > .create-wrap").wait_for(state="visible", timeout=15000)
        self.inject(self.form, "create.js", "__autodlClone")
        if choice.kind == "rent":
            self.call(self.form, "__autodlClone", "configureRent", {"id": source.id, "image": choice.image,
                      "region": self.names[choice.host.region], "expandGb": choice.data_expand_gb})
        page = self.call(self.form, "__autodlClone", "page", source.id)
        if page["all"] or any(m["ticked"] for m in page["models"]):
            # Real rent pages can arrive with a model preselected. Reloading may
            # keep that default, so explicitly uncheck via the labelled checkbox.
            line = self.form.locator(".filter-item").filter(has=self.form.locator(".filter-label", has_text="GPU型号:"))
            for name in [m["name"] for m in page["models"] if m["ticked"]] + (["全部"] if page["all"] else []):
                line.locator("label.el-checkbox").filter(has=self.form.get_by_text(name, exact=True)).click()
        self.call(self.form, "__autodlClone", "tickModel", source.id, choice.host.gpu)
        self.call(self.form, "__autodlClone", "pickCount", source.id, choice.count)
        rows = self.load_host_rows(self.form, choice.host.id)
        found = [r for r in rows if r["host"] == choice.host.id]
        if not found or found[0]["free"] < choice.count:
            raise NoResource()
        r = found[0]
        actual = Host(r["host"], choice.host.region, r["model"], r["free"], r["vramGb"], r["cpu"], r["memGb"], r["driver"], r["cuda"], r["price"], r["diskGb"], r["expandGb"], choice.host.system_gb, choice.host.max_system_expand_gb)
        from .models import choose
        # Use the original reference (not the candidate) for inherited minimums.
        if not choose(config, source, self.reference, [actual], choice.kind):
            raise Pause("提交前主机规格不再满足要求")
        self.call(self.form, "__autodlClone", "pickHost", source.id, choice.host.id)
        if choice.kind == "rent" and choice.system_gb > choice.host.system_gb:
            # Accessible labels are required; no positional guesses among disk inputs.
            system_input = self.form.get_by_label("系统盘扩容", exact=True)
            if system_input.count() != 1:
                raise Pause("新租页面未找到唯一系统盘扩容输入框")
            system_input.fill(str(choice.system_gb - choice.host.system_gb))
            system_input.press("Tab")
            self.form.wait_for_timeout(500)
        expansion = self.call(self.form, "__autodlClone", "expansion", source.id)
        if expansion["gb"] != choice.data_expand_gb:
            if not expansion["need"] and choice.data_expand_gb:
                self.form.get_by_text("需要扩容", exact=True).click()
            self.call(self.form, "__autodlClone", "focusExpansion", source.id)
            self.form.keyboard.press("ControlOrMeta+A")
            self.form.keyboard.type(str(choice.data_expand_gb))
            self.form.keyboard.press("Tab")
            self.form.wait_for_timeout(500)
        prepared = self.call(self.form, "__autodlClone", "prepareCreate", source.id,
                             {"host": choice.host.id, "model": choice.host.gpu, "gpus": choice.count, "expandGb": choice.data_expand_gb})
        cp = prepared["copy"]
        if cp["region"] != self.names[choice.host.region]:
            raise Pause("提交前页面地区不一致")
        self.prepared_source, self.prepared_copy = source.id, cp
        return {"host_id": cp["host"], "region": choice.host.region, "gpu": cp["model"], "count": cp["gpus"],
                "data_expand_gb": cp["expandGb"], "system_gb": cp["systemDiskGb"], "data_gb": cp["diskGb"],
                "kind": choice.kind, "image": choice.image, "hourly": str(money(cp["price"]) * choice.count), "disk_daily": cp["daily"]}

    def prepare_start(self, source):
        self.console_row(source.id, self.form)
        self.power_context = self.call(self.form, "__autodl", "startPowerOnGpu", source.id)["ctx"]
        bound = self.call(self.form, "__autodl", "bindDialog", self.power_context)
        return {"hourly": bound["price"], "disk_daily": None, "source_id": source.id}

    def capture_submit(self, click, *, create):
        # Observe the site's own mutation response; never manufacture a request.
        responses = []
        def observe(response):
            request = response.request
            if urlparse(response.url).hostname == "www.autodl.com" and request.method == "POST" and "/api/v1/" in response.url:
                if (create and ("/create/" in response.url or "/clone" in response.url)) or (not create and ("/start" in response.url or "/power" in response.url)):
                    responses.append(response)
        self.form.on("response", observe)
        try:
            click()
            for _ in range(60):
                if responses:
                    break
                self.form.wait_for_timeout(250)
            if len(responses) != 1:
                raise TimeoutError("未捕获唯一提交响应，必须核对远端")
            response = responses[0]
            if response.status != 200:
                if response.status in (401, 403):
                    raise Pause("提交时登录失效，需重新 login 并核对远端")
                raise TimeoutError("提交 HTTP 响应不明，必须核对远端")
            data = envelope(response.json())
            if not create:
                return None
            if isinstance(data, str) and re.fullmatch(r"[a-z0-9]{10}-[a-z0-9]{8}", data):
                return data
            if isinstance(data, dict):
                ids = {data[k] for k in ("instance_uuid", "uuid") if isinstance(data.get(k), str)}
                if len(ids) == 1 and re.fullmatch(r"[a-z0-9]{10}-[a-z0-9]{8}", next(iter(ids))):
                    return next(iter(ids))
            return None  # Reconcile the persisted before-set; never resubmit.
        finally:
            self.form.remove_listener("response", observe)

    def submit(self, choice, quote):
        return self.capture_submit(lambda: self.call(self.form, "__autodlClone", "confirmCreate", self.prepared_source, self.prepared_copy), create=True)

    def submit_start(self, quote):
        self.capture_submit(lambda: self.call(self.form, "__autodl", "confirm", self.power_context), create=False)
