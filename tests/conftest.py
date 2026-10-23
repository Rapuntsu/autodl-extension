from dataclasses import replace
from types import SimpleNamespace

import pytest

from autodl_grab.config import Config
from autodl_grab.engine import Engine
from autodl_grab.models import Host, Instance, choose
from autodl_grab.store import Store

SOURCE = "0123456789-abcdefgh"
TARGET = "9876543210-abcdefgh"


class FakeBackend:
    def __init__(self):
        self.source = Instance(SOURCE, "0123456789", "r1", "RTX 3090", 2, "stopped", None,
                               data_expand_gb=5, system_gb=30, data_gb=50, clone_locked=False)
        self.reference = Host("0123456789", "r1", "RTX 3090", 0, 24, 6, 24, "535.1", "12.1", "1.0", 50, 1000)
        self.hosts = [replace(self.reference, id="9876543210", idle=4),
                      replace(self.reference, id="7654321098", region="r2", idle=4)]
        self.startable = False
        self.target = None
        self.extra = []
        self.submits = []
        self.prepares = []
        self.on_submit = None
        self.on_prepare = None
        self.on_inventory = None
        self.inventory_calls = 0

    def instances(self):
        return [self.source, *([self.target] if self.target else []), *self.extra]

    def instance(self, identifier):
        return next(i for i in self.instances() if i.id == identifier)

    def can_start(self, source):
        return self.startable and source.state == "stopped"

    def inventory(self, config):
        self.inventory_calls += 1
        if self.on_inventory:
            self.on_inventory()
        return self.source, self.reference, self.hosts

    def prepare(self, config, source, choice):
        self.prepares.append(choice)
        if self.on_prepare:
            self.on_prepare(choice)
        return {"host_id": choice.host.id, "region": choice.host.region, "gpu": choice.host.gpu,
                "count": choice.count, "data_expand_gb": choice.data_expand_gb, "system_gb": choice.system_gb,
                "data_gb": choice.host.data_gb, "kind": choice.kind, "image": choice.image,
                "hourly": choice.hourly, "disk_daily": choice.disk_daily}

    def submit(self, choice, quote):
        self.submits.append(choice)
        if self.on_submit:
            return self.on_submit(choice)
        self.create_target(choice)
        return TARGET

    def create_target(self, choice):
        self.target = Instance(TARGET, choice.host.id, choice.host.region, choice.host.gpu, choice.count,
                               "running", True, copy_complete=True, source_id=SOURCE if choice.kind == "clone" else None)

    def prepare_start(self, source):
        return {"source_id": source.id, "hourly": "2.00", "disk_daily": None}

    def submit_start(self, quote):
        self.submits.append("start")
        self.source.state, self.source.gpu_mode = "running", True


@pytest.fixture
def env(tmp_path):
    config = Config(SOURCE, "5", "2", other_regions=["r2"], image="test-image")
    backend = FakeBackend()
    store = Store(tmp_path)
    task = store.new(config)
    clock = SimpleNamespace(value=store.get(task)["created"])
    engine = Engine(backend, store, now=lambda: clock.value, sleep=lambda seconds: setattr(clock, "value", clock.value + seconds), emit=lambda _: None)
    yield SimpleNamespace(config=config, backend=backend, store=store, task=task, engine=engine, clock=clock, home=tmp_path)
    store.close()
