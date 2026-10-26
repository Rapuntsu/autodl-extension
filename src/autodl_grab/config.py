from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import tomllib

from .errors import Pause


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise Pause("金额必须为有限的非负十进制数") from None


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)*", value):
        raise Pause("无法识别驱动/CUDA 版本")
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * max(0, 6 - len(parts))


@dataclass
class Config:
    source_id: str
    max_hourly: str
    max_disk_daily: str
    alternate_gpus: list[str] = field(default_factory=list)
    other_regions: list[str] = field(default_factory=list)
    min_vram_gb: int | None = None
    min_cpu: int | None = None
    min_memory_gb: int | None = None
    min_cuda: str | None = None
    min_driver: str | None = None
    image: str = ""
    rent_data_expand_gb: int = 0
    rent_system_expand_gb: int = 0
    poll_seconds: int = 15
    max_wait_seconds: int = 86400
    boot_timeout_seconds: int = 600
    clone_timeout_seconds: int = 3600
    max_clones: int = 1

    def __post_init__(self):
        if not isinstance(self.source_id, str) or not re.fullmatch(r"[a-z0-9]{10}-[a-z0-9]{8}", self.source_id):
            raise Pause("source_id 必须是完整 AutoDL 实例 ID")
        if not isinstance(self.image, str):
            raise Pause("image 必须为字符串")
        money(self.max_hourly)
        money(self.max_disk_daily)
        for key in ("alternate_gpus", "other_regions"):
            values = getattr(self, key)
            if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values) or len(values) != len(set(values)):
                raise Pause(f"{key} 必须是不含重复项的非空字符串列表")
        for key in ("poll_seconds", "max_wait_seconds", "boot_timeout_seconds", "clone_timeout_seconds"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise Pause(f"{key} 必须为正整数秒")
        for key in ("rent_data_expand_gb", "rent_system_expand_gb", "max_clones", "min_vram_gb", "min_cpu", "min_memory_gb"):
            val = getattr(self, key)
            optional = key in ("min_vram_gb", "min_cpu", "min_memory_gb")
            if not (optional and val is None) and (type(val) is not int or val < 0):
                raise Pause(f"{key} 必须为非负整数")
        for key in ("min_driver", "min_cuda"):
            if getattr(self, key) is not None:
                version(getattr(self, key))
        if self.other_regions and not self.image.strip():
            raise Pause("启用其他地区新租必须填写 image")

    @classmethod
    def load(cls, path):
        try:
            return cls(**tomllib.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, TypeError, tomllib.TOMLDecodeError) as e:
            raise Pause(f"配置无效: {e}") from e

    def snapshot(self):
        return asdict(self)
