"""自动抓取（定时任务）的配置持久化层。

用途：把 GUI 中「自动抓取 / 定时任务」设置保存到本地配置文件
``data/scheduler.json``，供：

- GUI 读取/回显当前设置；
- headless 后台入口（``news scheduled-fetch``）读取并执行抓取；
- BAT / Windows Task Scheduler 构建命令。

配置文件中不包含任何密钥 / 敏感信息（仅来源、频率、时间、数量、是否自动导出）。

本项目没有统一的 scheduler 配置机制（AI 配置走 .env，站点配置走 sites/*.yaml），
因此新增独立 JSON 文件。``data/`` 已被 .gitignore 排除，不会污染 Git。
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


@dataclass
class SchedulerConfig:
    """一次定时抓取的完整配置快照。"""

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
        """返回稳定不变的 Windows Task Scheduler 任务名（不含空格）。"""
        return f"Laxinwen-{self.source.upper()}-AutoFetch"

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
        # 下一个整点（去掉秒/微秒）。若 now 已不在整点（分钟>0 或秒/微秒>0），
        # 则取下一个整点；否则取当前整点。
        nxt = now.replace(second=0, microsecond=0)
        if nxt.minute > 0 or now.second > 0 or now.microsecond > 0:
            nxt = nxt + timedelta(hours=1)
        nxt = nxt.replace(minute=0)
        # 对齐到 step 的倍数小时
        aligned = (nxt.hour // step) * step
        nxt = nxt.replace(hour=aligned)
        if nxt <= now:
            nxt = nxt + timedelta(hours=step)
        return nxt


def default_path() -> Path:
    """默认 scheduler 配置文件：项目根 ``data/scheduler.json``。"""
    # src/news/scheduler_config.py → 项目根（向上三级）
    return Path(__file__).resolve().parents[2] / "data" / "scheduler.json"


def load_config(path: str | Path | None = None) -> SchedulerConfig:
    """从文件读取定时抓取配置；文件不存在 / 损坏时返回默认配置。"""
    p = Path(path) if path else default_path()
    if not p.is_file():
        return SchedulerConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return SchedulerConfig()
    except (ValueError, OSError):
        logger.warning("读取 scheduler 配置失败（使用默认值）：%s", p)
        return SchedulerConfig()

    cfg = SchedulerConfig()
    for key, default in DEFAULTS.items():
        setattr(cfg, key, raw.get(key, default))
    # 兜底：limit 必须是正整数，interval 必须在合法区间内
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
    # extra 字段
    if isinstance(raw.get("extra"), dict):
        cfg.extra = raw["extra"]
    return cfg


def save_config(cfg: SchedulerConfig, path: str | Path | None = None) -> Path:
    """把配置写入文件（确保目录存在）。"""
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {k: getattr(cfg, k) for k in DEFAULTS}
    data["extra"] = cfg.extra or {}
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("定时抓取配置已保存：%s", p)
    return p
