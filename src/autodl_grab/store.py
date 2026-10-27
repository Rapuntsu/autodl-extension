"""Durable intent before effects. The OS releases the advisory lock on process exit."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import warnings

from .errors import Pause


@contextmanager
def profile_lock(home):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with (home / "process.lock").open("a+b") as handle:
        handle.seek(0)
        # Windows byte-range locks also deny reads by another handle. Inspect
        # file length rather than reading the byte we are about to lock.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Pause("同一登录配置已有进程运行") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.home / "tasks.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, config TEXT NOT NULL, phase TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, target TEXT,
            detail TEXT NOT NULL DEFAULT '', result TEXT, candidate TEXT);
          CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY, task TEXT NOT NULL, kind TEXT NOT NULL,
            choice TEXT, before_ids TEXT NOT NULL, target TEXT, state TEXT NOT NULL,
            created REAL NOT NULL, quote TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        with self.db:
            for name, kind in (("wait_state", "TEXT"), ("wait_since", "REAL")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
            # Preserve an in-progress wait from older versions where possible.
            for row in self.db.execute("SELECT id,detail FROM tasks WHERE phase='waiting_source' AND wait_since IS NULL").fetchall():
                try:
                    wait = json.loads(row["detail"])
                    if isinstance(wait["state"], str) and type(wait["since"]) in (int, float):
                        self.db.execute("UPDATE tasks SET wait_state=?,wait_since=? WHERE id=?",
                                        (wait["state"], wait["since"], row["id"]))
                except (ValueError, KeyError, TypeError):
                    pass

    def close(self):
        self.db.close()

    def new(self, config):
        active = self.active()
        if active:
            row = active[0]
            prefix = f'autodl-grab --home "{self.home.resolve()}"'
            raise Pause(f"已有未完成任务 {row['id']}（{row['phase']}）：{row['detail']}\n"
                        f"恢复：{prefix} resume {row['id']}\n"
                        f"取消：{prefix} cancel {row['id']}\n"
                        f"重新开始：{prefix} restart -c config.toml")
        with self.db:
            return self._insert(config)

    def _insert(self, config):
        task = uuid.uuid4().hex[:16]
        now = time.time()
        self.db.execute("INSERT INTO tasks(id,config,phase,created,updated) VALUES(?,?,?,?,?)",
                        (task, json.dumps(config.snapshot()), "polling", now, now))
        return task

    def active(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM tasks WHERE phase NOT IN ('complete','cancelled') ORDER BY created DESC")]

    def _cancel_selection(self, task, force):
        if task is not None:
            row = self.get(task)
        else:
            active = self.active()
            if len(active) > 1:
                raise Pause("存在多个未完成任务：" + ", ".join(row['id'] for row in active)
                            + "；请先使用 cancel <task-id> 分别处理")
            row = active[0] if active else None
        if row is None or row['phase'] in ('complete', 'cancelled'):
            return row, None
        pending = self.pending(row['id'])
        if pending and not force:
            prefix = f'autodl-grab --home "{self.home.resolve()}"'
            target = pending['target'] or row['target'] or '未知'
            raise Pause(f"任务 {row['id']} 有待核对的 {pending['kind']} 提交，目标：{target}。\n"
                        f"恢复：{prefix} resume {row['id']}\n"
                        "确认放弃本地跟踪时为 cancel 或 restart 添加 --force；"
                        "平台操作及计费仍可能继续。")
        return row, pending

    def backup(self):
        path = self.home / 'backups' / (
            'tasks-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            + '-' + uuid.uuid4().hex + '.sqlite3')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as destination:
                self.db.backup(destination)
        except (OSError, sqlite3.Error):
            raise Pause("任务数据库备份失败；未取消或重新启动任务，请检查备份目录权限和磁盘空间") from None
        return str(path.resolve())

    def _cancel(self, row, pending, backup):
        detail = "已放弃本地跟踪；平台操作及计费仍可能继续" if pending else "用户取消本地任务"
        now = time.time()
        self.db.execute("UPDATE tasks SET phase='cancelled',detail=?,updated=? WHERE id=?",
                        (detail, now, row['id']))
        return {'id': row['id'], 'phase': 'cancelled', 'backup': backup,
                'forced': pending is not None,
                'target': (pending['target'] if pending else None) or row['target'], 'detail': detail}

    def cancel(self, task=None, *, force=False):
        """Caller must hold profile_lock, as for other modifying operations."""
        row, pending = self._cancel_selection(task, force)
        if row is None:
            return {'id': None, 'detail': '没有未完成任务'}
        if row['phase'] in ('complete', 'cancelled'):
            return {'id': row['id'], 'phase': row['phase'], 'detail': '任务已结束，无需取消'}
        backup = self.backup()
        with self.db:
            result = self._cancel(row, pending, backup)
        self._log_update(row['id'], phase='cancelled', detail=result['detail'])
        return result

    def restart(self, config, *, force=False):
        """Back up before atomically cancelling and inserting; caller holds profile_lock."""
        row, pending = self._cancel_selection(None, force)
        backup = self.backup() if row else None
        with self.db:
            cancelled = self._cancel(row, pending, backup) if row else None
            task = self._insert(config)
        if cancelled:
            self._log_update(row['id'], phase='cancelled', detail=cancelled['detail'])
        return task, cancelled

    def get(self, task):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        if row is None:
            raise Pause("任务不存在")
        return dict(row)

    def all(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY created DESC")]

    def update(self, task, **values):
        allowed = {"phase", "target", "detail", "result", "candidate", "wait_state", "wait_since"}
        if not set(values) <= allowed:
            raise ValueError("Unknown task fields")
        values["updated"] = time.time()
        with self.db:
            self.db.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in values)} WHERE id=?", (*values.values(), task))
        self._log_update(task, **values)

    def _log_update(self, task, **values):
        # No raw platform replies, tokens, or SSH credentials in the log.
        try:
            with (self.home / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"task": task, **values}, ensure_ascii=False) + "\n")
        except OSError:
            warnings.warn("事件日志写入失败；任务状态已保存到 SQLite", RuntimeWarning)

    def intent(self, task, kind, choice, ids, quote):
        with self.db:
            if self.pending(task):
                raise Pause("存在未核对的提交记录")
            cur = self.db.execute("INSERT INTO actions(task,kind,choice,before_ids,state,created,quote) VALUES(?,?,?,?,?,?,?)",
                                  (task, kind, json.dumps(choice.snapshot()) if choice else None,
                                   json.dumps(sorted(ids)), "pending", time.time(), json.dumps(quote)))
            self.db.execute("UPDATE tasks SET phase='submitted',candidate=?,updated=? WHERE id=?",
                            (json.dumps(choice.snapshot()) if choice else None, time.time(), task))
        return cur.lastrowid

    def action_update(self, action, state, target=None):
        if state not in {"pending", "accepted", "rejected", "aborted", "done"}:
            raise ValueError("Unknown action state")
        with self.db:
            self.db.execute("UPDATE actions SET state=?,target=? WHERE id=?", (state, target, action))

    def pending(self, task):
        row = self.db.execute("SELECT * FROM actions WHERE task=? AND state IN ('pending','accepted') ORDER BY id DESC LIMIT 1", (task,)).fetchone()
        return dict(row) if row else None

    def clone_count(self, task):
        return self.db.execute("SELECT COUNT(*) FROM actions WHERE task=? AND kind='clone' AND state NOT IN ('rejected','aborted')", (task,)).fetchone()[0]
