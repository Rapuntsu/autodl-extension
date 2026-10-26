import argparse
import json
from pathlib import Path
import sys

from .browser import Browser
from .config import Config
from .diagnostics import diagnose_error
from .engine import Engine
from .errors import Pause, Retryable
from .store import Store, profile_lock
from . import __version__


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="autodl-grab", description="AutoDL 原实例开机、同区磁盘克隆与其他地区新租")
    parser.add_argument("--version", action="version", version=f"autodl-grab {__version__}")
    parser.add_argument("--home", type=Path, default=Path.cwd() / ".local",
                        help="登录配置目录（默认项目 .local；同目录单进程）")
    parser.add_argument("--headed", action="store_true", help="显示浏览器以排查页面问题")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login").add_argument("--sub-account", action="store_true", help="直接打开 AutoDL 子账号登录入口")
    for name in ("plan", "run"):
        command = commands.add_parser(name)
        command.add_argument("-c", "--config", type=Path, required=True)
    commands.add_parser("status")
    commands.add_parser("resume").add_argument("task_id")
    # Suppress subparser defaults so options preceding the command survive.
    for command in commands.choices.values():
        command.add_argument("--headed", action="store_true", default=argparse.SUPPRESS,
                             help="显示浏览器以排查页面问题")
        command.add_argument("--home", type=Path, default=argparse.SUPPRESS, help="登录配置目录")
    args = parser.parse_args(argv)
    store = None
    browser = None
    stage = "local_setup"
    try:
        home = args.home.resolve()
        if args.command == "status":
            store = Store(home)
            result = []
            for row in store.all():
                result.append({k: json.loads(row[k]) if k in ("candidate", "result") and row[k] else row[k]
                               for k in ("id", "phase", "created", "updated", "target", "detail", "candidate", "result")})
            output(result)
            return 0
        stage = "config_load"
        config = Config.load(args.config) if args.command in ("plan", "run") else None
        stage = "task_store"
        with profile_lock(home):
            store = Store(home)
            if args.command == "login":
                browser = Browser(home, login=True, sub_account=args.sub_account)
                with browser:
                    browser.login()
                print("已验证登录并保存独立状态。")
                return 0
            task = None
            if args.command == "resume":
                task = args.task_id
                row = store.get(task)
                if row["phase"] == "complete":
                    output(json.loads(row["result"]))
                    return 0
            browser = Browser(home, headed=args.headed)
            if args.command == "plan":
                with browser:
                    browser.stage = args.command
                    output(Engine(browser, store).plan(config))
            else:
                # The engine owns initial connection retries as well as later browser recovery.
                task = task or store.new(config)
                browser.task_id = task
                browser.stage = args.command
                print(f"任务 ID: {task}", flush=True)
                try:
                    output(Engine(browser, store).run(task))
                finally:
                    browser.__exit__(None, None, None)
        return 0
    except Pause as e:
        print(f"已暂停: {e}", file=sys.stderr)
        if e.__cause__ is not None:
            print(diagnose_error(args.home, e, getattr(browser, "stage", stage)), file=sys.stderr)
        return 2
    except Retryable:
        print("只读查询暂时失败或限流，请稍后重试。", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("本地监控停止；平台已提交操作继续执行。请使用 status / resume。", file=sys.stderr)
        return 130
    except Exception as e:
        print(diagnose_error(args.home, e, getattr(browser, "stage", stage)), file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
