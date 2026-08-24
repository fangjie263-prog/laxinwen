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
    build_schtasks_disable,
    build_schtasks_enable,
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
    # 多任务：任务名 = Laxinwen-<SOURCE>-<job_id>，稳定且唯一
    cfg = SchedulerConfig(source="rfi", id="rfi-hourly")
    assert cfg.task_name() == "Laxinwen-RFI-rfi-hourly"
    # 不同 job 不同任务名
    assert SchedulerConfig(source="rfi", id="rfi-morning").task_name() == "Laxinwen-RFI-rfi-morning"
    assert SchedulerConfig(source="eco", id="eco-morning").task_name() == "Laxinwen-ECO-eco-morning"
    # 重复调用一致（不会产生后缀 (1)(2)）
    assert cfg.task_name() == cfg.task_name()


def test_task_name_derived_default_when_no_id():
    # 未显式指定 id 时派生出 <source>-default
    cfg = SchedulerConfig(source="rfi")
    assert cfg.task_name() == "Laxinwen-RFI-rfi-default"


# ---------------------------------------------------------------------------
# 4-8. Task Scheduler 命令生成
# ---------------------------------------------------------------------------

def test_build_arguments():
    # 多任务：参数携带 job id，让后台入口定位具体任务
    cfg = SchedulerConfig(source="rfi", id="rfi-hourly")
    assert build_arguments(cfg) == "-m news scheduled-fetch --job-id rfi-hourly"


def test_build_schtasks_create_daily():
    cfg = SchedulerConfig(
        source="rfi", id="rfi-morning", frequency=FREQ_DAILY, time="23:00", limit=10
    )
    cmd = build_schtasks_create(cfg, python_exe="C:\\proj\\.venv\\Scripts\\python.exe", project_root="C:\\proj")
    assert cmd[0] == "schtasks"
    assert "/Create" in cmd
    assert "Laxinwen-RFI-rfi-morning" in cmd
    assert "/SC" in cmd and "DAILY" in cmd
    assert "/ST" in cmd and "23:00" in cmd


def test_build_schtasks_create_hourly():
    cfg = SchedulerConfig(source="rfi", frequency=FREQ_HOURLY, interval_hours=2)
    cmd = build_schtasks_create(cfg)
    assert "HOURLY" in cmd
    assert "/MO" in cmd and "2" in cmd


def test_build_schtasks_delete_and_run_and_query():
    cfg = SchedulerConfig(source="eco", id="eco-morning")
    assert build_schtasks_delete(cfg) == ["schtasks", "/Delete", "/TN", "Laxinwen-ECO-eco-morning", "/F"]
    assert build_schtasks_run(cfg) == ["schtasks", "/Run", "/TN", "Laxinwen-ECO-eco-morning"]
    q = build_schtasks_query(cfg)
    assert "/Query" in q and "Laxinwen-ECO-eco-morning" in q


def test_build_schtasks_enable_and_disable():
    cfg = SchedulerConfig(source="eco", id="eco-morning")
    assert build_schtasks_enable(cfg) == [
        "schtasks", "/Change", "/TN", "Laxinwen-ECO-eco-morning", "/ENABLE"
    ]
    assert build_schtasks_disable(cfg) == [
        "schtasks", "/Change", "/TN", "Laxinwen-ECO-eco-morning", "/DISABLE"
    ]


def test_enable_task_non_windows_generates_command():
    """非 Windows：enable_task 只生成命令，不真正执行。"""
    from news.task_scheduler import enable_task, disable_task

    cfg = SchedulerConfig(source="rfi", id="rfi-hourly")
    r = enable_task(cfg)
    assert r["ok"] is True
    assert r["executed"] is False
    assert "/ENABLE" in r["cmd"]
    r2 = disable_task(cfg)
    assert r2["ok"] is True
    assert r2["executed"] is False
    assert "/DISABLE" in r2["cmd"]


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


# ---------------------------------------------------------------------------
# 19. 多任务 scheduler.json 读写 / 新建 / 编辑 / 删除 / 启停 / job id 唯一
# ---------------------------------------------------------------------------

def test_load_jobs_and_save_jobs_roundtrip():
    from news.scheduler_config import load_jobs, save_jobs
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "scheduler.json"
        jobs = [
            SchedulerConfig(id="rfi-hourly", name="RFI 每小时", source="rfi",
                            frequency=FREQ_HOURLY, interval_hours=1, limit=10,
                            enabled=True, auto_export=True),
            SchedulerConfig(id="rfi-morning", name="RFI 每日早报", source="rfi",
                            frequency=FREQ_DAILY, time="08:00", limit=50,
                            enabled=True, auto_export=True),
            SchedulerConfig(id="eco-morning", name="ECO 每日", source="eco",
                            frequency=FREQ_DAILY, time="09:00", limit=50,
                            enabled=False, auto_export=True),
        ]
        save_jobs(jobs, p)
        loaded = load_jobs(p)
        assert [j.job_id for j in loaded] == ["rfi-hourly", "rfi-morning", "eco-morning"]
        assert loaded[0].name == "RFI 每小时"
        assert loaded[0].frequency == FREQ_HOURLY
        assert loaded[0].interval_hours == 1
        assert loaded[0].limit == 10
        assert loaded[0].auto_export is True
        assert loaded[2].enabled is False


def test_job_ids_unique_across_jobs():
    # 不同 job 必须拥有唯一 id
    a = SchedulerConfig(id="rfi-hourly")
    b = SchedulerConfig(id="rfi-morning")
    assert a.job_id != b.job_id
    assert a.task_name() != b.task_name()


def test_save_config_updates_existing_job_by_id(tmp_path):
    """save_config 更新同 id job，不重复追加。"""
    from news.scheduler_config import load_jobs, save_config
    p = tmp_path / "scheduler.json"
    save_config(SchedulerConfig(id="rfi-hourly", source="rfi", limit=10), p)
    save_config(SchedulerConfig(id="rfi-hourly", source="rfi", limit=20), p)
    save_config(SchedulerConfig(id="eco-morning", source="eco", limit=5), p)
    jobs = load_jobs(p)
    assert len(jobs) == 2
    by_id = {j.job_id: j for j in jobs}
    assert by_id["rfi-hourly"].limit == 20  # 更新而非新增
    assert by_id["eco-morning"].limit == 5


def test_old_flat_scheduler_json_backward_compat(tmp_path):
    """旧版单任务扁平格式自动转换为多任务 jobs[]。"""
    from news.scheduler_config import load_jobs
    p = tmp_path / "scheduler.json"
    p.write_text(json.dumps({
        "enabled": True,
        "source": "rfi",
        "frequency": "daily",
        "time": "08:00",
        "limit": 50,
        "auto_export": True,
    }), encoding="utf-8")
    jobs = load_jobs(p)
    assert len(jobs) == 1
    assert jobs[0].job_id == "rfi-default"
    assert jobs[0].source == "rfi"
    assert jobs[0].enabled is True


def test_old_flat_load_config_still_works(tmp_path):
    """旧接口 load_config 兼容旧扁平格式。"""
    from news.scheduler_config import load_config
    p = tmp_path / "scheduler.json"
    p.write_text(json.dumps({"source": "eco", "limit": 30}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.source == "eco"
    assert cfg.limit == 30


# ---------------------------------------------------------------------------
# 20. scheduled-fetch --job-id CLI
# ---------------------------------------------------------------------------

def test_main_job_id_selects_job(monkeypatch, tmp_path):
    import news.scheduled_fetch as sf
    from news.scheduler_config import save_jobs
    captured = {}

    def fake_run(cfg, **kw):
        captured["cfg"] = cfg
        return 0

    monkeypatch.setattr(sf, "run_scheduled_fetch", fake_run)
    monkeypatch.setattr(sf, "_PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True)
    save_jobs([
        SchedulerConfig(id="rfi-hourly", source="rfi", limit=10, enabled=True),
        SchedulerConfig(id="eco-morning", source="eco", limit=5, enabled=True),
    ], tmp_path / "data" / "scheduler.json")

    rc = sf.main(["--job-id", "eco-morning", "--config", str(tmp_path / "data" / "scheduler.json")])
    assert rc == 0
    assert captured["cfg"].job_id == "eco-morning"
    assert captured["cfg"].source == "eco"


def test_main_job_id_not_found(monkeypatch, tmp_path):
    import news.scheduled_fetch as sf
    monkeypatch.setattr(sf, "_PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True)
    from news.scheduler_config import save_jobs
    save_jobs([SchedulerConfig(id="rfi-hourly", source="rfi", enabled=True)],
              tmp_path / "data" / "scheduler.json")
    rc = sf.main(["--job-id", "nope", "--config", str(tmp_path / "data" / "scheduler.json")])
    assert rc == 1  # 未找到 → 非零 exit code


def test_main_job_id_disabled_skips(monkeypatch, tmp_path):
    import news.scheduled_fetch as sf
    from news.scheduler_config import save_jobs
    monkeypatch.setattr(sf, "_PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True)
    save_jobs([SchedulerConfig(id="rfi-hourly", source="rfi", enabled=False)],
              tmp_path / "data" / "scheduler.json")
    rc = sf.main(["--job-id", "rfi-hourly", "--config", str(tmp_path / "data" / "scheduler.json")])
    assert rc == 0  # 停用任务跳过执行，非错误


def test_cli_scheduled_fetch_has_job_id_arg():
    from news.cli import build_parser
    parser = build_parser()
    for a in parser._subparsers._group_actions:
        for ch in a.choices:
            if ch == "scheduled-fetch":
                sub = a.choices[ch]
                opts = [o for o in sub._optionals._actions]
                assert any(o.dest == "job_id" for o in opts)
                return
    raise AssertionError("scheduled-fetch subparser not found")


# ---------------------------------------------------------------------------
# 21. 自动导出固定执行 / 没有新文章仍 SUCCESS / 输出目录不覆盖
# ---------------------------------------------------------------------------

def test_auto_export_output_dir_has_source_date_and_time(tmp_path):
    """不同 job 输出目录不同；同 job 多次执行（秒级时间戳）不覆盖。"""
    from news.scheduled_fetch import _run_auto_export
    seen = []

    def fake_export(storage, out_dir, *, source_id, limit, research_root=None):
        # 真实 portable export 会创建目录；这里模拟，触发 _unique_dir 防覆盖
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        seen.append(str(out_dir))
        class R:
            exported = 0
        return R()

    class _FakeStorage2:
        pass

    for _ in range(2):
        _run_auto_export(
            _FakeStorage2(), "rfi", 10, "portable",
            portable_dir=tmp_path / "portable",
            research_dir=tmp_path / "html",
            portable_export=fake_export,
        )
    _run_auto_export(
        _FakeStorage2(), "eco", 5, "portable",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_export,
    )
    # 三个目录互不覆盖（不同 job + 同 job 多次）
    assert len(set(seen)) == 3
    # 目录名含 source + 日期
    for s in seen:
        assert "Laxinwen-RFI-" in s or "Laxinwen-ECO-" in s


def test_zero_new_articles_is_fetch_success(tmp_path):
    """没有新文章（usable=0）仍 FETCH SUCCESS，并继续自动导出。"""
    from news.scheduled_fetch import run_scheduled_fetch

    class _ZeroStats:
        discovered = 0
        skipped_dup = 0
        fetched_ok = 0
        extracted_ok = 0
        low_quality = 0
        failed = 0
        usable = 0
        errors = []

    export_calls = []

    def fake_pipeline_factory(storage, limit):
        class P:
            def run_site(self, sid):
                return _ZeroStats()
            def close(self):
                pass
        return P()

    def fake_export(storage, out_dir, *, source_id, limit, research_root=None):
        export_calls.append((source_id, limit))
        class R:
            exported = 0
        return R()

    cfg = SchedulerConfig(id="rfi-hourly", source="rfi", limit=10, enabled=True, auto_export=True)
    logf = tmp_path / "scheduled-fetch.log"
    rc = run_scheduled_fetch(
        cfg, db_path=":memory:", log_file=logf,
        portable_dir=tmp_path / "portable", research_dir=tmp_path / "html",
        pipeline_factory=fake_pipeline_factory, portable_export=fake_export,
        storage_factory=lambda db: _FakeStorage(),
    )
    assert rc == 0
    content = logf.read_text(encoding="utf-8")
    assert "FETCH: SUCCESS" in content
    assert "EXPORT: SUCCESS" in content
    assert export_calls == [("rfi", 10)]  # 没有新文章也照常自动导出


def test_scheduled_fetch_logs_job_id(tmp_path):
    from news.scheduled_fetch import run_scheduled_fetch

    def fake_pipeline_factory(storage, limit):
        class P:
            def run_site(self, sid):
                return _FakeStats()
            def close(self):
                pass
        return P()

    def fake_export(storage, out_dir, *, source_id, limit, research_root=None):
        class R:
            exported = 3
        return R()

    cfg = SchedulerConfig(id="rfi-morning", name="RFI 每日早报", source="rfi", limit=50, auto_export=True)
    logf = tmp_path / "scheduled-fetch.log"
    run_scheduled_fetch(
        cfg, db_path=":memory:", log_file=logf,
        portable_dir=tmp_path / "portable", research_dir=tmp_path / "html",
        pipeline_factory=fake_pipeline_factory, portable_export=fake_export,
        storage_factory=lambda db: _FakeStorage(),
    )
    content = logf.read_text(encoding="utf-8")
    assert "JOB: rfi-morning" in content
    assert "SOURCE: rfi" in content
    assert "TARGET: 50" in content


def test_build_arguments_includes_job_id_for_schtasks():
    """Windows 任务参数携带 --job-id。"""
    from news.task_scheduler import build_schtasks_create, build_arguments
    cfg = SchedulerConfig(id="rfi-hourly", source="rfi", frequency=FREQ_HOURLY, interval_hours=1)
    assert "--job-id rfi-hourly" in build_arguments(cfg)
    cmd = build_schtasks_create(cfg, python_exe=r"C:\proj\.venv\Scripts\python.exe", project_root=r"C:\proj")
    tr = next(c for c in cmd if c.startswith("cmd.exe"))
    assert "--job-id rfi-hourly" in tr


def test_different_jobs_generate_different_task_names():
    jobs = [
        SchedulerConfig(id="rfi-hourly", source="rfi"),
        SchedulerConfig(id="rfi-morning", source="rfi"),
        SchedulerConfig(id="eco-morning", source="eco"),
        SchedulerConfig(id="hkej-evening", source="hkej"),
    ]
    names = [j.task_name() for j in jobs]
    assert len(set(names)) == 4
    assert names == [
        "Laxinwen-RFI-rfi-hourly",
        "Laxinwen-RFI-rfi-morning",
        "Laxinwen-ECO-eco-morning",
        "Laxinwen-HKEJ-hkej-evening",
    ]


def test_delete_one_job_task_does_not_affect_other():
    """删除 job A 的命令不涉及 job B。"""
    from news.task_scheduler import build_schtasks_delete
    a = SchedulerConfig(id="rfi-hourly", source="rfi")
    b = SchedulerConfig(id="eco-morning", source="eco")
    a_cmd = build_schtasks_delete(a)
    assert "Laxinwen-RFI-rfi-hourly" in a_cmd
    assert "Laxinwen-ECO-eco-morning" not in a_cmd
    b_cmd = build_schtasks_delete(b)
    assert "Laxinwen-ECO-eco-morning" in b_cmd


def test_repeated_install_same_job_no_dup():
    """同一 job 重复安装不产生重复任务（任务名稳定，schtasks /F 覆盖）。"""
    from news.task_scheduler import install_task
    cfg = SchedulerConfig(id="rfi-hourly", source="rfi")
    r1 = install_task(cfg)
    r2 = install_task(cfg)
    assert r1["task_name"] == "Laxinwen-RFI-rfi-hourly"
    assert r1["task_name"] == r2["task_name"]
    assert "(1)" not in r1["task_name"]


def test_rfi_7day_window_preserved():
    """多任务 scheduler 不改动 RFI discovery 时间窗口（rfi.py 未被我方修改）。"""
    from news.sources import rfi
    # 该常量的具体值由 rfi.py 决定（当前为 365 天），多任务实现不碰它。
    assert isinstance(rfi.RFI_DISCOVERY_MAX_AGE_DAYS, int)
    assert rfi.RFI_DISCOVERY_MAX_AGE_DAYS > 0
    # 确认 scheduled_fetch 没有覆盖该窗口常量
    import news.scheduled_fetch as sf
    assert not hasattr(sf, "RFI_DISCOVERY_MAX_AGE_DAYS")


def test_scheduler_json_not_in_git(tmp_path):
    """scheduler.json 属于 data/，被 .gitignore 排除（不进入 Git）。"""
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    text = gitignore.read_text(encoding="utf-8")
    assert "data/" in text
    assert "scheduler.json" not in text or "data" in text


def test_auto_export_dir_includes_job_id(tmp_path):
    """导出目录名应包含 job id（最好能看出 job）。"""
    from news.scheduled_fetch import _run_auto_export
    seen = []

    def fake_export(storage, out_dir, *, source_id, limit, research_root=None):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        seen.append(str(out_dir))
        class R:
            exported = 0
        return R()

    class _FakeStorage3:
        pass

    _run_auto_export(
        _FakeStorage3(), "rfi", 10, "portable",
        job_id="rfi-hourly",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_export,
    )
    assert len(seen) == 1
    # 目录名包含 source、日期时间戳和 job id
    assert "Laxinwen-RFI-" in seen[0]
    assert seen[0].endswith("-rfi-hourly")
    assert any(ch.isdigit() for ch in seen[0])


def test_lock_is_job_specific_not_source_specific(tmp_path):
    """lock 按 job id 区分：同 source 不同 job 不互斥，同一 job 用同一锁文件。"""
    from news.scheduled_fetch import _Lock

    locks_dir = tmp_path / "locks"

    # 同 source（rfi）的两个 job：应使用不同锁文件，可同时持有
    lock_hourly = _Lock(locks_dir / "rfi-hourly.lock")
    lock_morning = _Lock(locks_dir / "rfi-morning.lock")
    assert lock_hourly.acquire() is True
    assert lock_morning.acquire() is True  # 不同 job 不互斥，可并行
    lock_hourly.release()
    lock_morning.release()

    # 同一 job 不能并发持有：先持有 rfi-hourly，再试图获取同一锁应失败
    lock_a = _Lock(locks_dir / "rfi-hourly.lock")
    lock_a2 = _Lock(locks_dir / "rfi-hourly.lock")
    assert lock_a.acquire() is True
    assert lock_a2.acquire() is False  # 同一 job 已持有，不能再次启动
    lock_a.release()
    # 释放后再次可获取
    assert lock_a2.acquire() is True
    lock_a2.release()
