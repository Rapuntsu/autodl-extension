"""Read-only API shapes derived from kuake-pipe (see THIRD_PARTY.md).

No API mutation endpoints are called by this module.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from .errors import NoResource, Pause, Retryable

BASE = "https://www.autodl.com"
SUCCESS = {"Success", "OK", "success"}
# Only codes that unambiguously mean the operation did not commit belong here.
INVENTORY_CODES = {"GPUNotEnough", "GpuNotEnough", "InsufficientGPU", "MachineNoResource"}
SNAPSHOT_PATH = "/api/v1/instance/snapshot"
# The reference frontend rewrites api/v1 to api/v1/sub_user for child accounts.
SNAPSHOT_ROUTES = {
    "/api/v1/instance": SNAPSHOT_PATH,
    "/api/v1/sub_user/instance": "/api/v1/sub_user/instance/snapshot",
}
READ_PATHS = {"/api/v1/instance", "/api/v1/user/machine/list", SNAPSHOT_PATH}
# Keep application/session headers, including account-specific headers, but let
# Playwright manage cookies and transport framing for its shared browser context.
TRANSPORT_HEADERS = {"host", "cookie", "content-length", "connection", "accept-encoding"}
PAGING_KEYS = (("page_index", "page_size"), ("page", "page_size"), ("pageIndex", "pageSize"), ("page", "pageSize"))


def read_kind(path):
    """Narrow list-route recognition; never guess a mutation route."""
    if path in READ_PATHS:
        return path
    if path in SNAPSHOT_ROUTES.values():
        return SNAPSHOT_PATH
    if not path.startswith("/api/v1/"):
        return None
    parts = path.lower().strip("/").split("/")
    if any(not re.fullmatch(r"[a-z_]+", p) for p in parts[2:]):
        return None
    # This allowlist is about route grammar, not privileges. Alternate routes
    # additionally require a successful paginated response observed in the UI.
    context = {"user", "sub", "subuser", "sub_user", "subaccount", "sub_account", "account", "container"}
    for suffix, canonical in ((("instance",), "/api/v1/instance"), (("instances",), "/api/v1/instance"),
                              (("instance", "list"), "/api/v1/instance"), (("instance_list",), "/api/v1/instance"),
                              (("instancelist",), "/api/v1/instance"),
                              (("machine", "list"), "/api/v1/user/machine/list")):
        tail = parts[2:]
        if tuple(tail[-len(suffix):]) == suffix and all(p in context for p in tail[:-len(suffix)]):
            return canonical
    return None


def diagnostic_route(url):
    """Only ordinary route words, never query strings, account IDs or tokens."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "www.autodl.com" or not parsed.path.startswith("/api/"):
        return None
    parts = parsed.path.split("/")
    return "/".join(p if (p in ("", "api", "v1", "v2") or re.fullmatch(r"[A-Za-z_]{1,24}", p)) else "[redacted]" for p in parts[:12])


def schema_keys(value, limit=40):
    return sorted(k for k in value if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", k))[:limit] if isinstance(value, dict) else []


def safe_code(code):
    if type(code) is int:
        return str(code)
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", code):
        return code
    return "无法安全显示"


class RequestSession:
    """In-memory profiles of real, known read-only browser requests.

    Never serialize these headers or bodies into task records or diagnostics.
    Unknown paths/methods are ignored instead of being replayed speculatively.
    """
    def __init__(self):
        self.profiles = {}
        self.diagnostics = []

    def profile(self, request):
        parsed = urlparse(request.url)
        if parsed.scheme != "https" or parsed.netloc != "www.autodl.com" or request.method not in ("POST", "GET"):
            return None
        try:
            if request.method == "GET":
                pairs = parse_qsl(parsed.query, keep_blank_values=True)
                if len({k for k, _ in pairs}) != len(pairs):
                    return None
                body = dict(pairs)
            else:
                if parsed.query:
                    return None
                body = request.post_data_json
            headers = request.all_headers()
        except Exception:
            return None
        if not isinstance(body, dict) or not isinstance(headers, dict):
            return None
        return {"path": parsed.path, "method": request.method, "body": dict(body),
                "headers": {k.lower(): v for k, v in headers.items()
                            if k.lower() not in TRANSPORT_HEADERS and not k.startswith(":")}}

    def record(self, request, response=None, payload=None):
        path = diagnostic_route(request.url)
        if path is None:
            return
        item = {"path": path, "method": request.method if request.method in ("POST", "GET", "PUT", "DELETE", "OPTIONS") else "other"}
        if response is not None:
            item["http_status"] = response.status
        if isinstance(payload, dict):
            item["code"] = safe_code(payload.get("code"))
            item["top_keys"] = schema_keys(payload)
            data = payload.get("data")
            item["data_type"] = type(data).__name__
            item["data_keys"] = schema_keys(data)
            rows = data.get("list") if isinstance(data, dict) else None
            if isinstance(rows, list) and rows:
                item["row_keys"] = schema_keys(rows[0])
        self.diagnostics.append(item)
        self.diagnostics = self.diagnostics[-80:]

    def observe(self, request):
        self.record(request)
        parsed = urlparse(request.url)
        canonical = read_kind(parsed.path)
        if parsed.path not in READ_PATHS and canonical != SNAPSHOT_PATH:
            return
        if canonical == SNAPSHOT_PATH and request.method != "GET":
            return
        profile = self.profile(request)
        if profile:
            if canonical == SNAPSHOT_PATH:
                identifier = profile["body"].get("instance_uuid")
                if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9]{10}-[a-z0-9]{8}", identifier):
                    return
            elif not any(a in profile["body"] and b in profile["body"] for a, b in PAGING_KEYS):
                return
        if profile:
            if canonical == SNAPSHOT_PATH:
                current = self.profiles.get("/api/v1/instance")
                if current and (SNAPSHOT_ROUTES.get(current["path"]) != profile["path"]
                                or current["headers"].get("authorization") != profile["headers"].get("authorization")):
                    return
            self.bind(canonical, profile)

    def bind(self, canonical, profile):
        previous = self.profiles.get(canonical)
        snapshot = self.profiles.get(SNAPSHOT_PATH)
        if canonical == "/api/v1/instance" and snapshot and (
                snapshot["path"] != SNAPSHOT_ROUTES.get(profile["path"])
                or snapshot["headers"] != profile["headers"]):
            self.profiles.pop(SNAPSHOT_PATH, None)
        if canonical == "/api/v1/instance" and previous and (
                previous["path"] != profile["path"] or previous["headers"] != profile["headers"]):
            # A new account must not retain the previous account's detail/market.
            self.profiles.pop(SNAPSHOT_PATH, None)
            self.profiles.pop("/api/v1/user/machine/list", None)
        self.profiles[canonical] = profile

    def observe_response(self, response):
        request = response.request
        if diagnostic_route(request.url) is None:
            return
        content_type = getattr(response, "headers", {}).get("content-type", "")
        if content_type and "json" not in content_type.lower():
            self.record(request, response)
            return
        try:
            payload = response.json()
        except Exception:
            self.record(request, response)
            return
        self.record(request, response, payload)
        canonical = read_kind(urlparse(request.url).path)
        if canonical == SNAPSHOT_PATH:
            return
        if (canonical is None or response.status != 200 or not isinstance(payload, dict)
                or type(payload.get("code")) not in (str, int) or payload.get("code") not in SUCCESS):
            return
        profile = self.profile(request)
        if not profile or not any(a in profile["body"] and b in profile["body"] for a, b in PAGING_KEYS):
            return
        data = payload.get("data")
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return
        required = {"uuid", "status", "req_gpu_amount"} if canonical == "/api/v1/instance" else {"machine_id", "gpu_idle_num"}
        if any(not isinstance(r, dict) or not required <= r.keys() for r in rows):
            return
        # Empty aliases need a numeric total as well as pagination and route
        # grammar. Do not turn unrelated empty response lists into instance APIs.
        if not rows and type(data.get("total", data.get("total_count"))) is not int:
            return
        self.bind(canonical, profile)

    def has(self, path):
        return path in self.profiles

    def parameters(self, path, body, token=None):
        profile = self.profiles.get(path)
        if path == SNAPSHOT_PATH:
            self.route(path)  # Refuse unverified list aliases before any request.
            current = self.profiles.get("/api/v1/instance") or profile
            if current:
                return dict(body), dict(current["headers"])
        if profile is None:
            # Browser code must first visit the corresponding page for a child
            # session. Do not transplant account headers between different APIs.
            if self.profiles or not token:
                raise Pause(f"尚未捕获网页只读会话: {path}；请检查网页是否可查看对应实例/市场")
            return body, {"Authorization": token}
        # Preserve the current site's filters/account context, while allowing
        # the caller to set page number and the requested inventory filters.
        parameters = {**profile["body"], **body}
        if path == "/api/v1/instance":
            # These are UI search filters, not account authorization/scope.
            for key in ("uuid", "instance_uuid", "name", "keyword", "search", "machine_id"):
                if key not in body:
                    parameters.pop(key, None)
        for page, size in PAGING_KEYS:
            if page in profile["body"] and size in profile["body"]:
                if page != "page_index" and "page_index" in parameters:
                    parameters[page] = parameters.pop("page_index")
                if size != "page_size" and "page_size" in parameters:
                    parameters[size] = parameters.pop("page_size")
                break
        return parameters, dict(profile["headers"])

    def route(self, path):
        profile = self.profiles.get(path)
        if path == SNAPSHOT_PATH and profile is None:
            current = self.profiles.get("/api/v1/instance")
            if current:
                route = SNAPSHOT_ROUTES.get(current["path"])
                if route is None:
                    raise Pause("当前实例列表会话的详情路径尚未核实；请核对网页详情请求，未尝试其他账号接口")
                return route, "GET"
        return (profile["path"], profile["method"]) if profile else (path, "GET" if path == SNAPSHOT_PATH else "POST")


def retry_seconds(value):
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else None
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            return None


def envelope(payload):
    if not isinstance(payload, dict):
        raise Pause("平台返回结构变化")
    code = payload.get("code")
    if type(code) not in (str, int):
        raise Pause("平台返回未知状态（code=无法安全显示）；未继续操作")
    if code in INVENTORY_CODES:
        raise NoResource("平台明确返回 GPU 库存不足")
    if code not in SUCCESS:
        # Do not log platform text, which may contain credentials or request data.
        label = safe_code(code)
        if code in ("AuthorizeFailed", "Unauthorized", "TokenExpired"):
            raise Pause(f"登录已失效，请重新 login（code={label}）")
        if code in ("PermissionDenied", "AccessDenied", "Forbidden", "NoPermission", "SubAccountPermissionDenied"):
            raise Pause(f"当前账号无权限（code={label}）；子账号请由主账号检查容器实例、源实例可见性及镜像权限")
        if code in ("BalanceNotEnough", "InsufficientBalance"):
            raise Pause("余额不足")
        raise Pause(f"平台返回未知状态（code={label}）；未继续操作；子账号请核对网页会话及权限")
    return payload.get("data")


class _PaginationProblem(Exception):
    pass


class ReadAPI:
    def __init__(self, context, token=None, session=None, diagnostics_home=None):
        self.context, self.token = context, token
        self.session = session or RequestSession()
        self.diagnostics_home = diagnostics_home

    def post(self, path, body):
        if path not in READ_PATHS:
            raise Pause("只读查询拒绝访问未适配接口")
        body, headers = self.session.parameters(path, body, self.token)
        route, method = self.session.route(path)
        try:
            if method == "GET":
                params = {k: json.dumps(v, ensure_ascii=False, separators=(",", ":")) if isinstance(v, (dict, list)) else v for k, v in body.items()}
                response = self.context.request.get(BASE + route, params=params, headers=headers, timeout=30000)
            else:
                response = self.context.request.post(BASE + route, data=body, headers=headers, timeout=30000)
        except Exception:
            raise Retryable("资源查询网络失败") from None
        if response.status == 429:
            raise Retryable("平台限流", retry_seconds(response.headers.get("retry-after")))
        if response.status == 401:
            raise Pause(f"登录失效（HTTP 401，{path}），请重新 login")
        if response.status == 403:
            raise Pause(f"当前账号无权限（HTTP 403，{path}）；子账号请由主账号检查容器实例、源实例可见性及镜像权限")
        if response.status >= 500:
            raise Retryable("资源查询服务端暂时失败")
        if response.status != 200:
            raise Pause("资源查询返回未知 HTTP 状态")
        try:
            payload = response.json()
        except Exception:
            raise Retryable("资源查询响应体读取或 JSON 解析暂时失败") from None
        try:
            return envelope(payload)
        except Pause as e:
            raise Pause(f"{e}；接口: {route}") from None

    def pages(self, path, body, identity):
        history = []
        for attempt in range(1, 4):
            try:
                return self._scan_pages(path, body, identity, history, attempt)
            except _PaginationProblem as error:
                reason = str(error)
                if reason in ("cross_page_overlap", "total_changed") and attempt < 3:
                    continue  # Discard the entire unstable scan, never merge it.
                labels = {
                    "invalid_page": "分页结构变化",
                    "invalid_id": "分页 ID 缺失或类型无效",
                    "duplicate_in_page": "同一页内 ID 重复",
                    "cross_page_overlap": "连续三轮查询均出现跨页 ID 重叠，列表排序或库存可能正在变化",
                    "invalid_total": "分页总数无效或小于已读取记录数",
                    "total_changed": "连续三轮查询的分页总数均发生变化",
                    "early_end": "分页提前结束",
                    "page_limit": "分页超过安全上限",
                }
                session = getattr(self, "session", None)
                route = session.route(path)[0] if session is not None else path
                route = diagnostic_route(BASE + route) or "[redacted]"
                detail = f"{labels[reason]}，无法确认完整库存；接口: {route}；第 {history[-1]['page']} 页；ID 字段: {identity}"
                home = getattr(self, "diagnostics_home", None)
                if home is not None:
                    report = Path(home) / "pagination_diagnostics.json"
                    try:
                        report.write_text(json.dumps({"path": route, "identity_field": identity,
                                                      "reason": reason, "pages": history[-80:]},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
                        detail += f"；已保存脱敏分页诊断: {report}（不含 ID、凭据或响应值）"
                    except OSError:
                        detail += "；脱敏分页诊断写入失败"
                raise Pause(detail) from None

    def _scan_pages(self, path, body, identity, history, attempt):
        result, seen = [], {}
        expected_total = None
        size = 50
        for page in range(1, 10001):
            data = self.post(path, {**body, "page_index": page, "page_size": size})
            summary = {"attempt": attempt, "page": page, "requested_page_size": size,
                       "previous_row_count": len(result)}
            history.append(summary)
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise _PaginationProblem("invalid_page")
            items = data["list"]
            summary["row_count"] = len(items)
            summary["row_keys"] = schema_keys(items[0]) if items else []
            summary["identity_types"] = sorted({type(item.get(identity)).__name__ if isinstance(item, dict)
                                                 else "non_object" for item in items})
            keys = []
            for item in items:
                value = item.get(identity) if isinstance(item, dict) else None
                if type(value) not in (str, int) or not value:
                    raise _PaginationProblem("invalid_id")
                keys.append(value)
            summary["duplicate_in_page_count"] = len(keys) - len(set(keys))
            summary["cross_page_overlap_count"] = len(set(keys) & seen.keys())
            first_on_page, changed_fields, exact_repeats = {}, set(), 0
            for key, item in zip(keys, items):
                previous = first_on_page.get(key, seen.get(key))
                if previous is not None:
                    if previous == item:
                        exact_repeats += 1
                    for field in schema_keys({**previous, **item}, limit=200):
                        if (field in previous) != (field in item) or previous.get(field) != item.get(field):
                            changed_fields.add(field)
                first_on_page[key] = item
            summary["repeated_id_changed_fields"] = sorted(changed_fields)[:40]
            summary["identical_repeat_count"] = exact_repeats
            if summary["duplicate_in_page_count"]:
                raise _PaginationProblem("duplicate_in_page")
            if summary["cross_page_overlap_count"]:
                raise _PaginationProblem("cross_page_overlap")
            seen.update(zip(keys, items))
            result.extend(items)
            total = data.get("total", data.get("total_count"))
            summary["total_type"] = type(total).__name__
            if expected_total is not None and total is None:
                raise _PaginationProblem("total_changed")
            if total is not None:
                if type(total) is not int or total < 0:
                    raise _PaginationProblem("invalid_total")
                if expected_total is not None and expected_total != total:
                    raise _PaginationProblem("total_changed")
                if total < len(result):
                    raise _PaginationProblem("invalid_total")
                expected_total = total
                if len(result) == total:
                    return result
                if not items:
                    raise _PaginationProblem("early_end")
            elif not items or len(items) < size:
                return result
        raise _PaginationProblem("page_limit")

    def instances(self):
        return self.pages("/api/v1/instance", {"date_from": "", "date_to": "", "status": [], "charge_type": []}, "uuid")

    def snapshot(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9]{10}-[a-z0-9]{8}", identifier):
            raise Pause("实例详情查询必须使用完整实例 ID")
        data = self.post(SNAPSHOT_PATH, {"instance_uuid": identifier})
        if not isinstance(data, dict):
            raise Pause("实例详情接口返回结构变化")
        if data.get("uuid") is not None and data["uuid"] != identifier:
            raise Pause("实例详情接口返回了不同的实例 ID")
        return data

    def machines(self, regions, gpus):
        return self.pages("/api/v1/user/machine/list", {
            "charge_type": "payg", "region_sign": "", "gpu_type_name": gpus,
            "machine_tag_name": [], "gpu_idle_num": 0, "mount_net_disk": False,
            "instance_disk_size_order": "", "date_range": "", "date_from": "", "date_to": "",
            "pay_price_order": "", "gpu_idle_type": "", "default_order": True,
            "region_sign_list": regions,
        }, "machine_id")
