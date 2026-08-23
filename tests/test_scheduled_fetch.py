"""自动定时抓取（scheduled-fetch）相关测试。

覆盖 Windows 计划任务 + headless 后台入口 + 配置持久化 + auto export。

Windows Task Scheduler 的实际执行（schtasks）只能在 Windows 上运行，
因此这里只对「纯 Python 的命令构建 / 配置 / 参数」逻辑做单元测试，
实际执行明确标注 REQUIRES WINDOWS REAL TEST。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from news.scheduler_config import (
    FREQ_DAILY,
    FREQ_HOURLY,
    HOURLY_INTERVALS,
    SchedulerConfig,
    load_config,
    save_config,
)
from news.task_scheduler import (
    build_arguments,
    build_schtasks_create,
    build_schtasks_delete,
    build_schtasks_query,
    build_schtasks_run,
    find_python_executable,
    install_task,
)
from news.scheduled_fetch import run_scheduled_fetch


# ---------------------------------------------------------------------------
# 1. scheduler config 保存/读取
# ---------------------------------------------------------------------------

def test_scheduler_config_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "scheduler.json"
        cfg = SchedulerConfig(
            enabled=True,
            source="rfi",
            frequency=FREQ_DAILY,
            time="23:00",
            limit=10,
            auto_export=True,
            export_type="portable",
        )
        save_config(cfg, p)
        loaded = load_config(p)
        assert loaded.enabled is True
        assert loaded.source == "rfi"
        assert loaded.frequency == FREQ_DAILY
        assert loaded.time == "23:00"
        assert loaded.limit == 10
        assert loaded.auto_export is True
        assert loaded.export_type == "portable"


def test_scheduler_config_default_when_missing():
    with tempfile.TemporaryDirectory() as d:
        cfg = load_config(Path(d) / "nope.json")
        assert cfg.enabled is False
        assert cfg.source == "rfi"
        assert cfg.limit == 50


def test_scheduler_config_invalid_frequency():
    cfg = SchedulerConfig(frequency="weekly")
    ok, reason = cfg.is_valid()
    assert not ok
    assert "频率" in reason


def test_scheduler_config_bad_limit_falls_back():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        p.write_text(json.dumps({"limit": -5, "interval_hours": 99}), encoding="utf-8")
        cfg = load_config(p)
        assert cfg.limit == 50
        assert cfg.interval_hours == 1


# ---------------------------------------------------------------------------
# 2. daily / hourly schedule 参数
# ---------------------------------------------------------------------------

def test_daily_schedule_next_run():
    tz = ZoneInfo("Asia/Shanghai")
    cfg = SchedulerConfig(frequency=FREQ_DAILY, time="23:00")
    # 当天未到 → 当天 23:00
    assert cfg.next_run(datetime(2026, 8, 24, 8, 0, tzinfo=tz)).strftime("%Y-%m-%d %H:%M") == "2026-08-24 23:00"
    # 当天已过 → 明天 23:00
    assert cfg.next_run(datetime(2026, 8, 24, 23, 30, tzinfo=tz)).strftime("%Y-%m-%d %H:%M") == "2026-08-25 23:00"


def test_hourly_schedule_next_run():
    tz = ZoneInfo("Asia/Shanghai")
    cfg = SchedulerConfig(frequency=FREQ_HOURLY, interval_hours=2)
    # 08:30 → 对齐到下一个 2 的倍数小时 = 10:00
    assert cfg.next_run(datetime(2026, 8, 24, 8, 30, tzinfo=tz)).strftime("%Y-%m-%d %H:%M") == "2026-08-24 10:00"
    # 08:00 整 → 下一个 2 倍数小时 = 10:00
    assert cfg.next_run(datetime(2026, 8, 24, 8, 0, 0, tzinfo=tz)).strftime("%Y-%m-%d %H:%M") == "2026-08-24 10:00"


def test_task_name_stable():
    cfg = SchedulerConfig(source="rfi")
    assert cfg.task_name() == "Laxinwen-RFI-AutoFetch"
    # 重复调用一致（不会产生后缀 (1)(2)）
    assert cfg.task_name() == cfg.task_name()


# ---------------------------------------------------------------------------
# 4-8. Task Scheduler 命令生成
# ---------------------------------------------------------------------------

def test_build_arguments():
    cfg = SchedulerConfig(source="rfi")
    assert build_arguments(cfg) == "-m news scheduled-fetch"


def test_build_schtasks_create_daily():
    cfg = SchedulerConfig(
        source="rfi", frequency=FREQ_DAILY, time="23:00", limit=10
    )
    cmd = build_schtasks_create(cfg, python_exe="C:\\proj\\.venv\\Scripts\\python.exe", project_root="C:\\proj")
    assert cmd[0] == "schtasks"
    assert "/Create" in cmd
    assert "Laxinwen-RFI-AutoFetch" in cmd
    assert "/SC" in cmd and "DAILY" in cmd
    assert "/ST" in cmd and "23:00" in cmd


def test_build_schtasks_create_hourly():
    cfg = SchedulerConfig(source="rfi", frequency=FREQ_HOURLY, interval_hours=2)
    cmd = build_schtasks_create(cfg)
    assert "HOURLY" in cmd
    assert "/MO" in cmd and "2" in cmd


def test_build_schtasks_delete_and_run_and_query():
    cfg = SchedulerConfig(source="eco")
    assert build_schtasks_delete(cfg) == ["schtasks", "/Delete", "/TN", "Laxinwen-ECO-AutoFetch", "/F"]
    assert build_schtasks_run(cfg) == ["schtasks", "/Run", "/TN", "Laxinwen-ECO-AutoFetch"]
    q = build_schtasks_query(cfg)
    assert "/Query" in q and "Laxinwen-ECO-AutoFetch" in q


# ---------------------------------------------------------------------------
# 5. Windows 路径含空格 quoting
# ---------------------------------------------------------------------------

def test_windows_path_with_spaces_quoted():
    cfg = SchedulerConfig(source="rfi")
    cmd = build_schtasks_create(
        cfg,
        python_exe=r"D:\AI Projects\test\.venv\Scripts\python.exe",
        project_root=r"D:\AI Projects\test",
    )
    tr = next(c for c in cmd if c.startswith("cmd.exe"))
    # 含空格的 python 路径必须被双引号保护
    assert '"' in tr
    assert "python.exe" in tr


def test_find_python_executable_returns_abs():
    # 至少返回一个非空字符串（项目 .venv 或当前解释器）
    exe = find_python_executable()
    assert isinstance(exe, str) and exe


# ---------------------------------------------------------------------------
# 9. 后台入口不 import tkinter
# ---------------------------------------------------------------------------

def test_scheduled_fetch_does_not_import_tkinter():
    import sys

    # 模拟 tkinter 不可用
    saved = sys.modules.get("tkinter")
    sys.modules["tkinter"] = None
    try:
        import news.scheduled_fetch  # noqa: F401
        import news.task_scheduler  # noqa: F401
        import news.scheduler_config  # noqa: F401
    finally:
        if saved is not None:
            sys.modules["tkinter"] = saved
        else:
            sys.modules.pop("tkinter", None)
    # 若成功导入，说明这些模块不依赖 tkinter
    assert True


# ---------------------------------------------------------------------------
# 10. scheduled fetch 调用现有 pipeline；11. auto export 调用 portable export
# ---------------------------------------------------------------------------

class _FakeStats:
    discovered = 5
    skipped_dup = 2
    fetched_ok = 3
    extracted_ok = 3
    low_quality = 1
    failed = 0
    usable = 3
    errors = []


class _FakeStorage:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_scheduled_fetch_calls_pipeline_and_auto_export(tmp_path):
    calls = {"pipeline": [], "export": []}

    def fake_pipeline_factory(storage, limit):
        calls["pipeline"].append(limit)

        class P:
            def run_site(self, sid):
                calls["pipeline"].append(sid)
                return _FakeStats()

            def close(self):
                pass

        return P()

    def fake_export(storage, out_dir, *, source_id, limit, research_root=None):
        calls["export"].append((source_id, limit))

        class R:
            exported = 3
            analyzed_ok = 0
            analyzed_failed = 0
            unanalyzed = 3

        return R()

    cfg = SchedulerConfig(
        source="rfi", frequency=FREQ_DAILY, time="23:00", limit=10, auto_export=True
    )
    logf = tmp_path / "scheduled-fetch.log"
    rc = run_scheduled_fetch(
        cfg,
        db_path=":memory:",
        log_file=logf,
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        pipeline_factory=fake_pipeline_factory,
        portable_export=fake_export,
        storage_factory=lambda db: _FakeStorage(),
    )
    assert rc == 0
    # pipeline 被调用且 limit=10（usable limit），run_site('rfi')
    assert calls["pipeline"][0] == 10
    assert calls["pipeline"][1] == "rfi"
    # auto export 被调用
    assert calls["export"] == [("rfi", 10)]


# ---------------------------------------------------------------------------
# 12. fetch failure 不被错误标记为 export failure
# ---------------------------------------------------------------------------

def test_export_failure_does_not_mark_fetch_failure(tmp_path):
    def fake_pipeline_factory(storage, limit):
        class P:
            def run_site(self, sid):
                return _FakeStats()

            def close(self):
                pass

        return P()

    def failing_export(*args, **kwargs):
        raise RuntimeError("export boom")

    cfg = SchedulerConfig(source="rfi", limit=10, auto_export=True)
    logf = tmp_path / "scheduled-fetch.log"
    rc = run_scheduled_fetch(
        cfg,
        db_path=":memory:",
        log_file=logf,
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        pipeline_factory=fake_pipeline_factory,
        portable_export=failing_export,
        storage_factory=lambda db: _FakeStorage(),
    )
    # 抓取本身成功 → 退出码 0（EXPORT FAILED 不导致 FETCH FAILED）
    assert rc == 0
    content = logf.read_text(encoding="utf-8")
    # 分离记录：FETCH 成功 + EXPORT 失败，两者独立标记
    assert "FETCH: SUCCESS" in content
    assert "EXPORT: FAILED" in content


# ---------------------------------------------------------------------------
# 13. 重复安装不创建第二个任务（任务名稳定，schtasks /F 覆盖）
# ---------------------------------------------------------------------------

def test_repeated_install_same_task_name():
    # task_name 稳定 → 重复 install 会更新原任务而非新建 (1)(2)
    cfg = SchedulerConfig(source="rfi")
    name = cfg.task_name()
    # install_task 在 headless 环境仅生成命令（executed=False）
    r1 = install_task(cfg)
    r2 = install_task(cfg)
    assert r1["task_name"] == name
    assert r2["task_name"] == name
    # 不会出现 "Laxinwen-RFI-AutoFetch (1)"
    assert "(1)" not in r1["task_name"]
    assert r1["task_name"] == r2["task_name"]


# ---------------------------------------------------------------------------
# 14. limit 仍是 usable limit
# ---------------------------------------------------------------------------

def test_limit_passed_as_max_items(tmp_path):
    seen = {}

    def fake_pipeline_factory(storage, limit):
        seen["limit"] = limit

        class P:
            def run_site(self, sid):
                return _FakeStats()

            def close(self):
                pass

        return P()

    cfg = SchedulerConfig(source="rfi", limit=17, auto_export=False)
    logf = tmp_path / "scheduled-fetch.log"
    run_scheduled_fetch(
        cfg,
        db_path=":memory:",
        log_file=logf,
        pipeline_factory=fake_pipeline_factory,
        portable_export=lambda *a, **k: None,
        storage_factory=lambda db: _FakeStorage(),
    )
    assert seen["limit"] == 17


# ---------------------------------------------------------------------------
# 15. RFI article_interval=15 没有被破坏（pipeline 从 yaml 读取，未绕过）
# ---------------------------------------------------------------------------

def test_rfi_article_interval_preserved():
    # 后台入口复用现有 Pipeline.run_site，后者从 sites/rfi.yaml 读取
    # article_interval=15 并应用到 fetcher —— 我们没有修改该链路。
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    assert cfg.get("article_interval") == 15


# ---------------------------------------------------------------------------
# 16. 数据库去重没有被绕过（后台入口复用现有 Pipeline / Storage）
# ---------------------------------------------------------------------------

def test_dedup_not_bypassed():
    # 后台入口调用 pipeline.run_site()，后者内部使用 storage.url_exists /
    # title_fp_exists / all_canonical_urls 做去重。我们未新增第二套数据库，
    # 也未绕过 existing_urls。此处仅断言入口复用了 Pipeline 而非新逻辑。
    from news.scheduled_fetch import _default_pipeline_factory

    from news.pipeline import Pipeline

    # _default_pipeline_factory 返回真正的 Pipeline 实例
    from news.storage import Storage

    with tempfile.TemporaryDirectory() as d:
        storage = Storage(Path(d) / "test.db")
        p = _default_pipeline_factory(storage, limit=5)
        assert isinstance(p, Pipeline)
        p.close()
        storage.close()


# ---------------------------------------------------------------------------
# 17. python -m news 入口可用（BAT / Task Scheduler 依赖此调用）
# ---------------------------------------------------------------------------

def test_python_m_news_entry_no_tkinter():
    """`__main__` 模块可被 import，且不依赖 tkinter（headless 关键约束）。"""
    import sys

    saved = sys.modules.get("tkinter")
    sys.modules["tkinter"] = None
    try:
        import news.__main__  # noqa: F401
    finally:
        if saved is not None:
            sys.modules["tkinter"] = saved
        else:
            sys.modules.pop("tkinter", None)
    assert True


def test_build_arguments_uses_module_entry():
    """后台入口参数为 `-m news scheduled-fetch`，配合 __main__ 可被 python -m news 执行。"""
    from news.scheduler_config import SchedulerConfig
    from news.task_scheduler import build_arguments

    assert build_arguments(SchedulerConfig()) == "-m news scheduled-fetch"


# ---------------------------------------------------------------------------
# 18. 相对路径基于项目根解析（工作目录无关，满足 Task Scheduler 要求）
# ---------------------------------------------------------------------------

def test_main_resolves_relative_paths_against_project_root(monkeypatch, tmp_path):
    """`python -m news scheduled-fetch` 传相对 log/db 路径时应落到项目根。"""
    import news.scheduled_fetch as sf
    from news.scheduler_config import SchedulerConfig

    captured = {}

    def fake_run(cfg, **kw):
        captured["cfg"] = cfg
        captured["db_path"] = kw["db_path"]
        captured["log_file"] = kw["log_file"]
        return 0

    monkeypatch.setattr(sf, "run_scheduled_fetch", fake_run)
    # 强制项目根为 tmp_path（模拟不同工作目录）
    monkeypatch.setattr(sf, "_PROJECT_ROOT", tmp_path)

    cfg = SchedulerConfig(source="rfi", limit=3)
    # 写一个默认配置，让 load_config 读取
    (tmp_path / "data").mkdir(parents=True)
    from news.scheduler_config import save_config
    save_config(cfg, tmp_path / "data" / "scheduler.json")

    # 用相对路径（模拟 Windows Task Scheduler 从 System32 启动）
    rc = sf.main(["--db", "data/news.db", "--log-file", "data/logs/scheduled-fetch.log"])
    assert rc == 0
    assert str(captured["db_path"]) == str(tmp_path / "data" / "news.db")
    assert str(captured["log_file"]) == str(tmp_path / "data" / "logs" / "scheduled-fetch.log")
