"""Value-free error reports: never serialize exception messages or locals."""
import json
from pathlib import Path
import re


NETWORK_CODES = {
    "ERR_NETWORK_ACCESS_DENIED", "ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED", "ERR_CERT_AUTHORITY_INVALID", "ERR_CERT_DATE_INVALID",
    "ERR_SSL_PROTOCOL_ERROR", "ERR_HTTP2_PROTOCOL_ERROR", "ERR_ABORTED", "ERR_FAILED",
}


def error_report(error, stage):
    package = Path(__file__).resolve().parent
    frames, codes, categories = [], set(), set()
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        message = str(error)
        codes.update(set(re.findall(r"\bnet::(ERR_[A-Z0-9_]+)\b", message)) & NETWORK_CODES)
        if isinstance(error, TimeoutError) or type(error).__name__ == "TimeoutError":
            categories.add("timeout")
        if "Executable doesn't exist" in message:
            categories.add("browser_missing")
        if type(error).__name__ == "TargetClosedError":
            categories.add("browser_closed")
        name = type(error).__name__
        name = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "Exception"
        tb = error.__traceback__
        locations = []
        while tb is not None:
            filename = Path(tb.tb_frame.f_code.co_filename).resolve()
            if filename.parent == package:
                locations.append({"file": filename.name, "line": tb.tb_lineno,
                                  "function": tb.tb_frame.f_code.co_name})
            tb = tb.tb_next
        frames.append({"type": name, "frames": locations[-20:]})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return {"stage": stage, "exceptions": frames[:8],
            "network_codes": sorted(codes), "categories": sorted(categories)}


def diagnose_error(home, error, stage):
    report = error_report(error, stage)
    if report["network_codes"]:
        detail = "页面网络访问失败（" + ", ".join(report["network_codes"]) + "）；请检查网络、代理或访问策略"
    elif "browser_missing" in report["categories"]:
        detail = "缺少 Chromium；请运行 python -m playwright install chromium"
    elif "timeout" in report["categories"]:
        detail = "浏览器等待超时；请使用 --headed 检查页面加载及页面适配"
    elif "browser_closed" in report["categories"]:
        detail = "浏览器或页面已关闭；请检查浏览器进程后重试"
    else:
        detail = "浏览器或本地环境异常（" + report["exceptions"][0]["type"] + "）"
    detail += f"；失败阶段: {stage}"
    path = Path(home) / "error_diagnostics.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return detail + "；脱敏诊断文件写入失败，请检查登录目录权限"
    return detail + f"；已保存脱敏异常诊断: {path}（不含凭据、参数值或原始响应）"
