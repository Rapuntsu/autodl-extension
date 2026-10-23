import json
import random
import time

from .config import Config, money
from .errors import Ambiguous, CloneExhausted, NoResource, Pause, Retryable
from .models import Choice, check_quote, choose


class Engine:
    def __init__(self, backend, store, *, sleep=time.sleep, now=time.time, emit=print):
        self.backend, self.store = backend, store
        self.sleep, self.now, self.emit = sleep, now, emit

    def delay(self, seconds):
        # Short pieces permit prompt Ctrl+C and progress reporting during long backoff.
        remaining = seconds
        while remaining > 0:
            part = min(30, remaining)
            self.sleep(part)
            remaining -= part
            if remaining > 0:
                self.emit(f"退避等待，剩余 {remaining:.0f} 秒")

    def plan(self, config):
        source, reference, hosts = self.backend.inventory(config)
        return {"source": vars(source), "reference": vars(reference),
                "clone": [c.snapshot() for c in choose(config, source, reference, hosts, "clone")],
                "rent": [c.snapshot() for c in choose(config, source, reference, hosts, "rent")]}

    def run(self, task):
        config = Config(**json.loads(self.store.get(task)["config"]))
        if self.store.get(task)["phase"] == "complete":
            return json.loads(self.store.get(task)["result"])
        failures = 0
        try:
            while True:
                try:
                    pending = self.store.pending(task)
                    if pending:
                        result = self.reconcile(task, config, pending)
                        if result:
                            return result
                    elif self.now() - self.store.get(task)["created"] >= config.max_wait_seconds:
                        raise Pause("达到任务最长等待时间")
                    else:
                        result = self.round(task, config)
                        if result:
                            return result
                    failures = 0
                    self.emit(f"任务 {task}: {self.store.get(task)['phase']}")
                    self.delay(config.poll_seconds * random.uniform(0.85, 1.15))
                except Retryable as e:
                    if self.now() - self.store.get(task)["created"] >= config.max_wait_seconds:
                        raise Pause("查询持续失败且已达到任务最长等待时间")
                    failures += 1
                    self.store.update(task, detail="查询网络失败或限流，等待后重新核对")
                    self.delay(max(e.retry_after or 0, min(300, 2 ** min(failures, 8))))
        except KeyboardInterrupt:
            self.store.update(task, phase="paused", detail="本地监控已停止，平台已提交的操作继续执行；resume 将先核对远端")
            raise
        except Pause as e:
            self.store.update(task, phase="paused", detail=str(e))
            raise
        except Exception as e:
            self.store.update(task, phase="paused", detail="浏览器或页面发生未知异常；提交记录已保留，请核对后 resume")
            raise Pause("浏览器或页面发生未知异常，任务已暂停") from e

    def round(self, task, config):
        source = self.backend.instance(config.source_id)
        if source.state == "running":
            if source.gpu_mode is not True:
                raise Pause("源实例正在无卡模式运行或模式不明，请手动关机后 resume")
            return self.complete(task, source, "start", None)
        if source.state in ("starting", "stopping", "creating", "cloning"):
            record = self.store.get(task)
            since = self.now()
            if record["phase"] == "waiting_source":
                try:
                    wait = json.loads(record["detail"])
                    if wait["state"] == source.state:
                        since = wait["since"]
                except (ValueError, KeyError, TypeError):
                    pass
            self.store.update(task, phase="waiting_source", detail=json.dumps({"state": source.state, "since": since}))
            if self.now() - since > config.boot_timeout_seconds:
                raise Pause("源实例过渡状态超时，已核对远端")
            return None
        if source.state != "stopped":
            raise Pause("未知源实例状态")
        if self.backend.can_start(source):
            return self.start(task, config, source)
        source, reference, hosts = self.backend.inventory(config)
        clones = choose(config, source, reference, hosts, "clone") if self.store.clone_count(task) < config.max_clones else []
        rents = choose(config, source, reference, hosts, "rent")
        exhausted = False
        for choice in [*clones, *rents]:
            if exhausted and choice.kind == "clone":
                continue
            # Check again before every alternative, including after a rejection.
            fresh = self.backend.instance(config.source_id)
            if fresh.state != "stopped":
                return None
            if self.backend.can_start(fresh):
                return self.start(task, config, fresh)
            self.store.update(task, phase="preparing", candidate=json.dumps(choice.snapshot()), detail=choice.kind)
            try:
                quote = self.backend.prepare(config, source, choice)
            except CloneExhausted:
                if choice.kind != "clone":
                    raise Pause("新租页面返回未知克隆状态")
                exhausted = True
                continue
            except NoResource:
                continue
            check_quote(config, choice, quote)
            # Source inventory can change while the form is being prepared.
            final_source = self.backend.instance(config.source_id)
            if final_source.state != "stopped" or self.backend.can_start(final_source):
                return None
            before = {i.id for i in self.backend.instances()}
            action = self.store.intent(task, choice.kind, choice, before, quote)
            try:
                target = self.backend.submit(choice, quote)
            except NoResource:
                self.store.action_update(action, "rejected")
                continue
            except Pause:
                # An explicit unknown/auth/balance result requires review even
                # if a target might subsequently become visible.
                raise
            except Exception as e:
                # Even an unknown exception after intent may have committed remotely.
                self.store.update(task, detail="提交响应不明，先核对新增实例")
                return self.reconcile(task, config, self.store.pending(task), uncertain=True)
            if target:
                if target in before or target == config.source_id:
                    raise Ambiguous("提交返回了已有实例 ID，禁止继续创建")
                self.store.action_update(action, "accepted", target)
                self.store.update(task, target=target, phase="monitoring")
            return self.reconcile(task, config, self.store.pending(task))
        self.store.update(task, phase="polling", detail="本轮无可用资源")
        return None

    def start(self, task, config, source):
        quote = self.backend.prepare_start(source)
        if money(quote["hourly"]) > money(config.max_hourly):
            raise Pause("原实例开机价格超限")
        action = self.store.intent(task, "start", None, {i.id for i in self.backend.instances()}, quote)
        try:
            self.backend.submit_start(quote)
        except NoResource:
            self.store.action_update(action, "rejected")
            return None
        except Pause:
            raise
        except Exception:
            return self.reconcile(task, config, self.store.pending(task), uncertain=True)
        self.store.action_update(action, "accepted", source.id)
        return self.reconcile(task, config, self.store.pending(task))

    def reconcile(self, task, config, action, uncertain=False):
        if not action:
            raise Pause("提交记录丢失")
        choice = Choice.restore(json.loads(action["choice"])) if action["choice"] else None
        target = action["target"]
        if action["kind"] == "start":
            target = config.source_id
        elif not target:
            before = set(json.loads(action["before_ids"]))
            new = [i for i in self.backend.instances() if i.id not in before]
            matches = [i for i in new if i.host_id == choice.host.id and i.gpu == choice.host.gpu
                       and i.region == choice.host.region and i.count == choice.count
                       and (choice.kind != "clone" or i.source_id == config.source_id)]
            if len(new) != 1 or len(matches) != 1:
                raise Ambiguous("提交结果无法唯一确认；保留记录，禁止重复提交或新租")
            target = matches[0].id
            self.store.action_update(action["id"], "accepted", target)
            self.store.update(task, target=target, phase="monitoring")
        instance = self.backend.instance(target)
        if choice and (instance.host_id != choice.host.id or instance.region != choice.host.region
                       or instance.gpu != choice.host.gpu or instance.count != choice.count):
            raise Ambiguous("目标实例规格与提交不一致")
        done = instance.state == "running" and instance.gpu_mode is True
        if choice and choice.kind == "clone":
            source = self.backend.instance(config.source_id)
            if instance.copy_complete is None or source.clone_locked is None:
                raise Pause("平台复制状态或源克隆锁字段未知，保留目标并暂停；需核实适配器")
            done = done and instance.copy_complete is True and source.clone_locked is False
        if done:
            result = self.complete(task, instance, action["kind"], json.loads(action["quote"]))
            self.store.action_update(action["id"], "done", target)
            return result
        timeout = config.clone_timeout_seconds if action["kind"] == "clone" else config.boot_timeout_seconds
        if self.now() - action["created"] > timeout:
            raise Pause("操作超时；已核对远端，目标/复制/源锁仍未确认，禁止创建其他实例")
        if instance.state not in ("running", "starting", "creating", "cloning"):
            raise Pause("提交后目标未进入可确认的开机状态，禁止继续创建")
        if instance.state == "running" and instance.gpu_mode is not True:
            raise Pause("目标未确认带卡运行")
        self.store.update(task, phase="monitoring", target=target, detail="等待带卡运行、平台复制完成及源解锁" if action["kind"] == "clone" else "等待带卡运行")
        return None

    def complete(self, task, instance, kind, quote):
        result = {"instance_id": instance.id, "region": instance.region, "gpu": instance.gpu, "count": instance.count,
                  "hourly": quote.get("hourly") if quote else None, "disk_daily": quote.get("disk_daily") if quote else None,
                  "data_status": "新租实例，未迁移原数据" if kind == "rent" else "平台系统盘和数据盘复制完成，未逐文件校验" if kind == "clone" else "原实例数据保留",
                  "kind": kind}
        self.store.update(task, phase="complete", target=instance.id, result=json.dumps(result, ensure_ascii=False), detail="成功")
        return result
