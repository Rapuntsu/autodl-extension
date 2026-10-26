from dataclasses import asdict, dataclass
from decimal import Decimal

from .config import money, version
from .errors import Pause, QuoteOverBudget


@dataclass(frozen=True)
class Host:
    id: str
    region: str
    gpu: str
    idle: int
    vram_gb: int
    cpu_per_gpu: int
    memory_per_gpu_gb: int
    driver: str
    cuda: str
    hourly_per_gpu: str
    data_gb: int
    max_data_expand_gb: int
    system_gb: int = 30
    max_system_expand_gb: int = 0


@dataclass
class Instance:
    id: str
    host_id: str
    region: str
    gpu: str
    count: int
    state: str
    gpu_mode: bool | None
    data_expand_gb: int = 0
    system_gb: int = 30
    data_gb: int = 50
    copy_complete: bool | None = None
    clone_locked: bool | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class Requirements:
    vram: int
    cpu: int
    memory: int
    cuda: str
    driver: str

    @classmethod
    def inherit(cls, config, source, host):
        return cls(config.min_vram_gb if config.min_vram_gb is not None else host.vram_gb,
                   config.min_cpu if config.min_cpu is not None else host.cpu_per_gpu * source.count,
                   config.min_memory_gb if config.min_memory_gb is not None else host.memory_per_gpu_gb * source.count,
                   config.min_cuda or host.cuda, config.min_driver or host.driver)


@dataclass(frozen=True)
class Choice:
    kind: str
    host: Host
    count: int
    data_expand_gb: int
    system_gb: int
    hourly: str
    disk_daily: str
    image: str = ""

    def snapshot(self):
        return asdict(self)

    @classmethod
    def restore(cls, data):
        return cls(**{**data, "host": Host(**data["host"])})


def choose(config, source, reference, hosts, kind):
    req = Requirements.inherit(config, source, reference)
    gpus = list(dict.fromkeys([source.gpu, *config.alternate_gpus]))
    regions = [source.region] if kind == "clone" else [r for r in config.other_regions if r != source.region]
    expansion = source.data_expand_gb if kind == "clone" else config.rent_data_expand_gb
    system = source.system_gb if kind == "clone" else 30 + config.rent_system_expand_gb
    ranked = []
    for h in hosts:
        if h.region not in regions or h.gpu not in gpus or h.id == source.host_id or h.idle < source.count:
            continue
        if h.vram_gb < req.vram or h.cpu_per_gpu * source.count < req.cpu or h.memory_per_gpu_gb * source.count < req.memory:
            continue
        if version(h.driver) < version(req.driver) or version(h.cuda) < version(req.cuda):
            continue
        if h.max_data_expand_gb < expansion or h.system_gb + h.max_system_expand_gb < system:
            continue
        if kind == "clone" and h.data_gb + expansion < source.data_gb + source.data_expand_gb:
            continue
        hourly = money(h.hourly_per_gpu) * source.count
        # Preliminary estimate only. The final, platform-displayed daily fee is authoritative.
        daily = Decimal(expansion + max(0, system - h.system_gb)) * Decimal("0.0066")
        if hourly > money(config.max_hourly):
            continue
        choice = Choice(kind, h, source.count, expansion, system, str(hourly), str(daily), config.image if kind == "rent" else "")
        ranked.append(((regions.index(h.region), gpus.index(h.gpu), hourly, -h.idle, h.id), choice))
    return [item[1] for item in sorted(ranked, key=lambda item: item[0])]


def check_quote(config, choice, quote):
    expected = {"host_id": choice.host.id, "region": choice.host.region, "gpu": choice.host.gpu,
                "count": choice.count, "data_expand_gb": choice.data_expand_gb, "system_gb": choice.system_gb,
                "data_gb": choice.host.data_gb, "kind": choice.kind, "image": choice.image}
    if any(quote.get(k) != v for k, v in expected.items()):
        raise Pause("提交前页面规格与候选不一致")
    if money(quote["hourly"]) > money(config.max_hourly) or money(quote["disk_daily"]) > money(config.max_disk_daily):
        raise QuoteOverBudget("提交前最终价格超过配置上限")
