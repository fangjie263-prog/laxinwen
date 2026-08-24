"""自动抓取（定时任务）的配置持久化层。

用途：把 GUI 中「自动抓取 / 定时任务」设置保存到本地配置文件
``data/scheduler.json``，供：

- GUI 读取/回显任务列表；
- headless 后台入口（``news scheduled-fetch --job-id <id>``）读取并执行单个任务；
- BAT / Windows Task Scheduler 构建命令。

自多任务版本起，``data/scheduler.json`` 采用 ``{"jobs": [...]}`` 结构，可同时
配置多个**独立**定时任务（独立 source / 频率 / 数量 / 启用状态 / Windows 任务）。

**向后兼容**：旧版单任务扁平格式（顶层直接是 ``source/enabled/...``）在读取时
会被自动转换为 ``{"jobs": [...]}``，保证老用户升级后不崩溃。

配置文件中不包含任何密钥 / 敏感信息（仅来源、频率、时间、数量、是否自动导出）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 支持的最高频率 / 调度类型
FREQ_DAILY = "daily"
FREQ_HOURLY = "hourly"
FREQUENCIES = (FREQ_DAILY, FREQ_HOURLY)

# 自动导出类型（对应现有 portable export）
EXPORT_PORTABLE = "portable"

# 支持的新闻来源（与 GUI _SOURCE_OPTIONS 中的实际站点 id 保持一致）
SUPPORTED_SOURCES = ("rfi", "eco", "hkej")

# 每小时模式的间隔选项（小时）
HOURLY_INTERVALS = (1, 2, 3, 6)

# 默认配置
DEFAULTS = {
    "enabled": False,
    "source": "rfi",
    "frequency": FREQ_DAILY,
    "time": "08:00",          # 每日模式的时间 HH:MM
    "interval_hours": 1,      # 每小时模式的间隔（小时）
    "limit": 50,
    "auto_export": True,
    "export_type": EXPORT_PORTABLE,
}

# 单个 job 的额外字段（id / name）
JOB_EXTRA_DEFAULTS = {
    "id": "",
    "name": "",
}


def default_job_id(source: str) -> str:
    """根据来源生成稳定的默认 job id（不含空格，可在 CLI/任务名中安全使用）。"""
    return f"{source}-default"


@dataclass
class SchedulerConfig:
    """单个定时抓取任务的完整配置快照（一个 job）。

    多任务版本：``data/scheduler.json`` 中 ``jobs[]`` 的每一项即是一个
    ``SchedulerConfig``。``id`` 全局唯一、稳定、可预测；``name`` 为展示名。
    """

    id: str = ""                 # 唯一、稳定 job id（如 "rfi-hourly"）
    name: str = ""               # 展示名（如 "RFI 每小时"）
    enabled: bool = False
    source: str = "rfi"
    frequency: str = FREQ_DAILY
    time: str = "08:00"
    interval_hours: int = 1
    limit: int = 50
    auto_export: bool = True
    export_type: str = EXPORT_PORTABLE
    # 额外字段（保留，不参与核心字段）
    extra: dict = field(default_factory=dict)

    # 兼容旧字段名：旧代码可能把 source 当作唯一标识，保留别名。
    @property
    def job_id(self) -> str:
        """返回稳定 job id；为空时自动派生（``<source>-default``）。"""
        if self.id:
            return self.id
        return default_job_id(self.source)

    def display_name(self) -> str:
        """展示名；为空时用 id。"""
        return self.name or self.job_id

    def is_valid(self) -> tuple[bool, str]:
        """校验配置是否可用于创建定时任务。返回 (ok, reason)。"""
        if self.source not in SUPPORTED_SOURCES:
            return False, f"不支持的新闻来源：{self.source}"
        if self.frequency not in FREQUENCIES:
            return False, f"不支持的抓取频率：{self.frequency}"
        if self.frequency == FREQ_DAILY:
            try:
                datetime.strptime(self.time, "%H:%M")
            except ValueError:
                return False, f"每日时间格式无效（应为 HH:MM）：{self.time}"
        else:
            if self.interval_hours not in HOURLY_INTERVALS:
                return False, f"每小时间隔无效：{self.interval_hours}"
        if not isinstance(self.limit, int) or self.limit <= 0:
            return False, f"抓取数量无效：{self.limit}"
        return True, "ok"

    def task_name(self) -> str:
        """返回稳定唯一的 Windows Task Scheduler 任务名（不含空格）。

        多任务下必须使用稳定且唯一的任务名，例如：:

            Laxinwen-RFI-rfi-hourly
            Laxinwen-RFI-rfi-morning
            Laxinwen-ECO-eco-morning

        规则：``Laxinwen-<SOURCE>-<job_id>``。重复安装会更新原任务（不产生
        ``(1)``/``(2)`` 后缀）。
        """
        return f"Laxinwen-{self.source.upper()}-{self.job_id}"

    def next_run(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """计算下一次计划运行时间（北京时间，24 小时制）。

        每日模式：当天该时间点；若已过则顺延到次日同一时刻。
        每小时模式：下一个整点对齐后的时间点。

        返回带北京时区（Asia/Shanghai）的 datetime；配置无效时返回 None。
        """
        ok, _ = self.is_valid()
        if not ok:
            return None
        from .beijing import BEIJING_TZ

        now = (now or datetime.now(BEIJING_TZ))
        if now.tzinfo is None:
            now = now.replace(tzinfo=BEIJING_TZ)
        now = now.astimezone(BEIJING_TZ)

        if self.frequency == FREQ_DAILY:
            hh, mm = self.time.split(":")
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            return target

        # hourly：下一个整点，间隔对齐到 hours 的倍数
        step = int(self.interval_hours)
        nxt = now.replace(second=0, microsecond=0)
        if nxt.minute > 0 or now.second > 0 or now.microsecond > 0:
            nxt = nxt + timedelta(hours=1)
        nxt = nxt.replace(minute=0)
        aligned = (nxt.hour // step) * step
        nxt = nxt.replace(hour=aligned)
        if nxt <= now:
            nxt = nxt + timedelta(hours=step)
        return nxt


# 兼容别名：旧代码 / 测试中的 ScheduledJob 即单 job 配置。
ScheduledJob = SchedulerConfig


def default_path() -> Path:
    """默认 scheduler 配置文件：项目根 ``data/scheduler.json``。"""
    return Path(__file__).resolve().parents[2] / "data" / "scheduler.json"


def _coerce_job_fields(cfg: SchedulerConfig, raw: dict) -> None:
    """对单 job 做字段类型兜底（limit/interval 合法化）。"""
    try:
        cfg.limit = int(cfg.limit)
    except (TypeError, ValueError):
        cfg.limit = DEFAULTS["limit"]
    if cfg.limit <= 0:
        cfg.limit = DEFAULTS["limit"]
    try:
        cfg.interval_hours = int(cfg.interval_hours)
    except (TypeError, ValueError):
        cfg.interval_hours = DEFAULTS["interval_hours"]
    if cfg.interval_hours not in HOURLY_INTERVALS:
        cfg.interval_hours = DEFAULTS["interval_hours"]
    if not isinstance(cfg.id, str) or not cfg.id.strip():
        cfg.id = default_job_id(cfg.source)
    cfg.id = cfg.id.strip()
    if not isinstance(cfg.name, str) or not cfg.name.strip():
        cfg.name = ""
    else:
        cfg.name = cfg.name.strip()
    if isinstance(raw.get("extra"), dict):
        cfg.extra = raw["extra"]


def _job_from_dict(raw: dict) -> SchedulerConfig:
    """把单个 job 的 dict 转成 SchedulerConfig（含字段兜底）。"""
    cfg = SchedulerConfig()
    for key, default in DEFAULTS.items():
        setattr(cfg, key, raw.get(key, default))
    for key, default in JOB_EXTRA_DEFAULTS.items():
        setattr(cfg, key, raw.get(key, default))
    _coerce_job_fields(cfg, raw)
    return cfg


def _job_to_dict(cfg: SchedulerConfig) -> dict:
    """把 SchedulerConfig 转成持久化 dict（jobs[] 中的一项）。"""
    data = {k: getattr(cfg, k) for k in DEFAULTS}
    data["id"] = cfg.job_id
    data["name"] = cfg.name
    data["extra"] = cfg.extra or {}
    return data


def load_jobs(path: str | Path | None = None) -> list[SchedulerConfig]:
    """从文件读取全部定时任务；文件不存在 / 损坏时返回空列表。

    兼容旧版扁平单任务格式：顶层是 ``source/enabled/...`` 时，自动转换为
    ``[{"id": "<source>-default", ...}]``。
    """
    p = Path(path) if path else default_path()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return []
    except (ValueError, OSError):
        logger.warning("读取 scheduler 配置失败（视为无任务）：%s", p)
        return []

    jobs: list[SchedulerConfig] = []
    if isinstance(raw.get("jobs"), list):
        for item in raw["jobs"]:
            if isinstance(item, dict):
                jobs.append(_job_from_dict(item))
    elif "source" in raw:
        # 旧版扁平单任务格式 → 转换为多任务 jobs 列表
        jobs.append(_job_from_dict(raw))
    return jobs


def save_jobs(jobs: list[SchedulerConfig], path: str | Path | None = None) -> Path:
    """把多个定时任务写入文件（确保目录存在）。

    写为多任务格式：``{"jobs": [...]}``。
    """
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"jobs": [_job_to_dict(cfg) for cfg in jobs]}
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("定时任务配置已保存：%s（%d 个任务）", p, len(jobs))
    return p


def load_config(path: str | Path | None = None) -> SchedulerConfig:
    """读取单个定时任务配置（向后兼容旧接口）。

    若文件为多任务格式，返回**第一个** job；否则返回扁平格式转换后的 job。
    文件不存在 / 损坏时返回默认配置。
    """
    jobs = load_jobs(path)
    if not jobs:
        return SchedulerConfig()
    return jobs[0]


def load_job(job_id: str, path: str | Path | None = None) -> Optional[SchedulerConfig]:
    """按 job id 读取单个定时任务；找不到返回 None。"""
    for job in load_jobs(path):
        if job.job_id == job_id:
            return job
    return None


def save_config(cfg: SchedulerConfig, path: str | Path | None = None) -> Path:
    """把单个定时任务配置写入文件（兼容旧接口）。

    若目标文件已是多任务格式且包含同名 id 的 job，则更新该 job；否则：
    - 文件为空 → 以单任务方式写入（多任务格式，jobs 列表含该项）；
    - 否则追加为新 job。
    """
    p = Path(path) if path else default_path()
    existing = load_jobs(p)
    # 若存在相同 id，则原地更新；否则追加
    replaced = False
    for i, job in enumerate(existing):
        if job.job_id == cfg.job_id:
            existing[i] = cfg
            replaced = True
            break
    if not replaced:
        existing.append(cfg)
    return save_jobs(existing, p)


def save_default_single_job(cfg: SchedulerConfig, path: str | Path | None = None) -> Path:
    """（兼容旧 BAT 首次初始化）以唯一 job 写入配置。

    当文件不存在时，写入一个包含该 job 的多任务文件。
    """
    p = Path(path) if path else default_path()
    existing = load_jobs(p)
    if not existing:
        existing = [cfg]
    else:
        # 更新同 id 或覆盖第一个
        replaced = False
        for i, job in enumerate(existing):
            if job.job_id == cfg.job_id:
                existing[i] = cfg
                replaced = True
                break
        if not replaced:
            existing[0] = cfg
    return save_jobs(existing, p)
