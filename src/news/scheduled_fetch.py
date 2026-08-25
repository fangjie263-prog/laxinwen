"""自动定时抓取的 headless 后台入口。

被 Windows Task Scheduler 在后台启动（``python -m news scheduled-fetch``）。

设计约束（严格遵守）：
- **不 import tkinter**：后台任务完全 headless，不弹 GUI、不等待点击；
- **复用现有 pipeline**：调用 ``Pipeline.run_site()``，不复制 / 不重新实现
  discovery / fetch_article / extraction / quality / storage / 去重逻辑；
- **保留 RFI 15 秒 throttle**：``article_interval=15`` 由 ``pipeline.run_site``
  从 ``sites/rfi.yaml`` 读取并应用到 fetcher，本模块不绕过；
- **保留数据库去重**：通过 ``Pipeline`` → ``storage.url_exists`` /
  ``title_fp_exists``，同一篇新闻不会重复抓；
- **limit = usable limit**：``max_items`` 表示目标可读新闻数；
- **自动导出**：启用时调用现有 portable export（默认便携阅读包）。
  导出失败**不影响抓取结果**，日志明确区分 ``FETCH SUCCESS`` / ``EXPORT FAILED``；
- **重复运行保护**：应用层 lock 文件（flock），避免两个 pipeline 同时抓同一来源。

日志写入 ``data/logs/scheduled-fetch.log``（追加，不无限生成文件）。
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import list_available_sites
from .scheduler_config import (
    SchedulerConfig,
    load_config,
)
from .run_identity import new_run_identity, parse_run_id

logger = logging.getLogger("news.scheduled_fetch")

# 项目根：src/news/scheduled_fetch.py → 向上三级
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 日志目录与文件
DEFAULT_LOG_DIR = _PROJECT_ROOT / "data" / "logs"
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "scheduled-fetch.log"

# 便携阅读包输出根目录（与 GUI / CLI 保持一致）
DEFAULT_PORTABLE_DIR = _PROJECT_ROOT / "data" / "export" / "portable"
# Word 研究阅读包输出根目录（与 GUI / CLI 保持一致）
DEFAULT_WORD_DIR = _PROJECT_ROOT / "data" / "export" / "word"
# AI 研究结果根目录（portable export 需要，用于内嵌 AI 详情）
DEFAULT_RESEARCH_DIR = _PROJECT_ROOT / "data" / "export" / "html"

# 默认数据库路径
DEFAULT_DB = _PROJECT_ROOT / "data" / "news.db"


def _setup_logging(log_file: str | Path) -> None:
    """配置日志：把 FileHandler 指向指定日志文件（追加）+ 输出到 stderr。

    同一进程内多次调用时，会移除旧的 FileHandler 并指向新的 ``log_file``，
    保证每次 ``run_scheduled_fetch`` 使用各自的日志文件（便于测试/多来源）。
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 移除旧的 FileHandler（若指向不同路径），避免日志写到上次的路径
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            try:
                h.close()
            except Exception:
                pass
            root.removeHandler(h)
    # 确保有 stderr handler（只加一次）
    has_stream = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if not has_stream:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


class _Lock:
    """基于 lock 文件的重复运行保护（应用层兜底）。

    Windows 计划任务没有原生“不启动新实例”的简单 flag，因此应用层用
    lock 文件保证同一来源不会有两个 pipeline 同时抓取。跨进程安全。
    """

    def __init__(self, lock_path: str | Path):
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> bool:
        """尝试获取锁。成功返回 True；已被其它进程持有返回 False。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl  # Unix

            self._fh = open(self.lock_path, "a+")
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                self._fh.close()
                self._fh = None
                return False
        except ImportError:
            # Windows：用 msvcrt.locking（需要文件至少有 1 字节才能加锁）
            import msvcrt

            try:
                self._fh = open(self.lock_path, "a+")
                # 确保文件至少 1 字节（msvcrt.locking 需要非空文件）
                self._fh.seek(0, 2)
                if self._fh.tell() == 0:
                    self._fh.write(" ")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                self._fh.close()
                self._fh = None
                return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            try:
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


def _default_pipeline_factory(storage, limit: int):
    """与 GUI 完全相同的 pipeline 构造（复用现有逻辑）。"""
    from .fetch import FetcherOptions, HttpxFetcher
    from .pipeline import Pipeline

    fetcher = HttpxFetcher(
        FetcherOptions(timeout=20.0, retries=3, min_interval=2.0, max_interval=4.0)
    )
    return Pipeline(storage, fetcher=fetcher, max_items=limit)


def _default_portable_export(storage, out_dir, *, source_id, limit, research_root=None, job_id="", run_id=""):
    """便携阅读包导出（与 GUI 的默认导出方式一致）。

    Portable 阅读包 = HTML + Word（DOCX）一次生成（见 portable.py）。
    """
    from .portable import export_portable_reader_package

    return export_portable_reader_package(
        storage, out_dir, source_id=source_id, limit=limit, research_root=research_root,
        job_id=job_id,
    )


def _default_word_export(storage, out_path, *, source_id, limit, job_id="", run_id=""):
    """Word（DOCX）研究阅读包导出（与 CLI / GUI 一致）。"""
    from .word_export import export_word_package

    return export_word_package(
        storage, out_path, source_id=source_id, limit=limit, job_id=job_id, run_id=run_id
    )


@dataclass
class AutoExportResult:
    """自动导出结果：HTML 与 Word 是否分别成功。

    Portable 阅读包 = HTML + Word（DOCX）一次生成。因此每次自动导出
    都会同时尝试 HTML 与 Word，二者独立标记成功/失败，调用方据此判定
    整体 ``EXPORT: SUCCESS`` / ``EXPORT: FAILED``。
    """

    html_ok: bool = False
    word_ok: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        """整体是否成功：HTML 与 Word 都必须成功。"""
        return self.html_ok and self.word_ok

    def summary(self) -> str:
        return f"HTML: {'SUCCESS' if self.html_ok else 'FAILED'} / WORD: {'SUCCESS' if self.word_ok else 'FAILED'}"


def _source_label(source_id: str) -> str:
    """来源 id → 显示名（用于日志/导出目录）。"""
    return source_id.upper()


def run_scheduled_fetch(
    cfg: SchedulerConfig,
    *,
    db_path: str | Path = DEFAULT_DB,
    log_file: str | Path = DEFAULT_LOG_FILE,
    portable_dir: str | Path = DEFAULT_PORTABLE_DIR,
    word_dir: str | Path = DEFAULT_WORD_DIR,
    research_dir: str | Path = DEFAULT_RESEARCH_DIR,
    pipeline_factory=None,
    portable_export=None,
    word_export=None,
    storage_factory=None,
) -> int:
    """执行一次 headless 自动抓取（复用现有 pipeline）。

    返回进程退出码（0=成功/候选耗尽，1=异常）。

    注意：不调用 ``_setup_logging``（由 CLI main 统一配置），便于测试注入。
    """
    from .storage import Storage

    _setup_logging(log_file)
    source_id = cfg.source
    limit = cfg.limit
    auto_export = cfg.auto_export
    export_type = cfg.export_type

    logger.info("========== 自动定时抓取开始 ==========")
    logger.info("JOB: %s", cfg.job_id)
    if cfg.name:
        logger.info("NAME: %s", cfg.name)
    logger.info("SOURCE: %s", source_id)
    logger.info("TARGET: %d（usable limit）", limit)
    logger.info("自动导出: %s", "开启" if auto_export else "关闭")

    # 重复运行保护（应用层 lock）：按 job id 加锁，保证同一 job 不会并发启动两次，
    # 同时不同 job（即使同 source，如 rfi-hourly / rfi-morning）可以独立并行。
    lock = _Lock(
        _PROJECT_ROOT
        / "data"
        / "scheduler"
        / "locks"
        / f"{cfg.job_id}.lock"
    )
    if not lock.acquire():
        logger.warning("检测到已有 job %s 在运行，跳过本次调度（Do not start a new instance）。", cfg.job_id)
        return 0
    try:
        run_identity = new_run_identity(
            job_id=cfg.job_id, output_root=portable_dir, source_id=source_id
        )
        logger.info("RUN_ID: %s", run_identity.run_id)
        storage_factory = storage_factory or _default_storage_factory
        with storage_factory(db_path) as storage:
            pipeline_factory = pipeline_factory or _default_pipeline_factory
            pipeline = pipeline_factory(storage, limit)
            try:
                stats = pipeline.run_site(source_id)
            finally:
                try:
                    pipeline.close()
                except Exception:
                    pass

            # 记录统计
            s = stats
            logger.info("JOB: %s", cfg.job_id)
            logger.info("SOURCE: %s", source_id)
            logger.info("TARGET: %d", limit)
            logger.info("发现数量: %d", s.discovered)
            logger.info("重复数量: %d", s.skipped_dup)
            logger.info("正文下载成功: %d", s.fetched_ok)
            logger.info("正文提取成功: %d", s.extracted_ok)
            logger.info("质量不合格: %d", s.low_quality)
            logger.info("抓取/提取失败: %d", s.failed)
            logger.info("可读新闻: %d / 目标 %d", s.usable, limit)
            if s.usable < limit and s.discovered > 0:
                logger.info("候选已耗尽，可读新闻 %d / 目标 %d", s.usable, limit)
            if s.usable < limit and s.discovered == 0:
                logger.info("无可发现的新候选（数据库可能已包含全部可用文章）")

            fetch_ok = True  # 候选耗尽不算失败；只有异常才算
            for err in s.errors:
                logger.error("失败明细: %s", err)
            # 明确标记抓取阶段结果（候选耗尽不视为失败）
            logger.info("FETCH: SUCCESS（usable=%d / 目标 %d）", s.usable, limit)

            # 自动导出（Portable 阅读包 = HTML + Word（DOCX）一次生成）
            if auto_export:
                try:
                    logger.info("开始自动导出（HTML + Word）……")
                    export_result = _run_auto_export(
                        storage,
                        source_id,
                        limit,
                        export_type,
                        job_id=cfg.job_id,
                        run_id=run_identity.run_id,
                        portable_dir=portable_dir,
                        word_dir=word_dir,
                        research_dir=research_dir,
                        portable_export=portable_export,
                        word_export=word_export,
                    )
                    # 整体成功要求 HTML 与 Word 都成功；任一失败即 EXPORT: FAILED。
                    if export_result.ok:
                        logger.info("EXPORT: SUCCESS → %s (%s)",
                                    export_result.summary(), export_result.message)
                    else:
                        logger.error("EXPORT: FAILED → %s", export_result.summary())
                        logger.error("%s", export_result.message)
                except Exception:
                    # 导出失败不影响抓取结果（已在上方标记 FETCH: SUCCESS）
                    logger.error("EXPORT: FAILED")
                    logger.error("导出失败 traceback:\n%s", traceback.format_exc())
            else:
                logger.info("EXPORT: SKIPPED（未开启自动导出）")

            logger.info("结束时间: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            logger.info("========== 自动定时抓取结束（exit=0）==========")
            return 0
    except Exception:
        logger.error("自动定时抓取异常（ERROR）:")
        logger.error(traceback.format_exc())
        logger.info("结束时间: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("========== 自动定时抓取结束（exit=1）==========")
        return 1
    finally:
        lock.release()


def _default_storage_factory(db_path):
    from .storage import Storage

    return Storage(db_path)


def _run_portable_export(
    storage,
    source_id: str,
    limit: int,
    *,
    job_id: str = "",
    run_id: str = "",
    portable_dir: str | Path,
    research_dir: str | Path,
    portable_export=None,
):
    """便携阅读包导出。返回 ``(out_dir, PortableResult)``。

    Portable 阅读包 = HTML + Word（DOCX）一次生成（见 portable.py）。
    """
    portable_dir = Path(portable_dir)
    if not run_id:
        run_id = new_run_identity(
            job_id=job_id, output_root=portable_dir, source_id=source_id
        ).run_id
    job_suffix = f"-{job_id}" if job_id else ""
    run_dt = parse_run_id(run_id)
    run_date = run_dt.strftime("%Y-%m-%d") if run_dt else datetime.now().strftime("%Y-%m-%d")
    base = portable_dir / (
        f"Laxinwen-{_source_label(source_id)}-"
        f"{run_date}-{run_id}{job_suffix}"
    )
    out_dir = _unique_dir(base)
    export = portable_export or _default_portable_export
    kwargs = dict(
        source_id=source_id,
        limit=limit,
        research_root=research_dir,
        run_id=run_id,
        job_id=job_id,
    )
    try:
        result = export(storage, out_dir, **kwargs)
    except TypeError as exc:
        if "run_id" not in str(exc):
            raise
        kwargs.pop("run_id")
        result = export(storage, out_dir, **kwargs)
    return out_dir, result


def _run_word_export(
    storage,
    source_id: str,
    limit: int,
    *,
    job_id: str = "",
    run_id: str = "",
    word_dir: str | Path = DEFAULT_WORD_DIR,
    word_export=None,
):
    """Word（DOCX）研究阅读包导出。返回描述字符串。"""
    word_dir = Path(word_dir)
    if not run_id:
        run_id = new_run_identity(job_id=job_id).run_id
    job_suffix = f"-{job_id}" if job_id else ""
    out_path = word_dir / (
        f"Laxinwen-{_source_label(source_id)}-"
        f"{run_id}{job_suffix}.docx"
    )
    out_path = _unique_path(out_path)
    export = word_export or _default_word_export
    kwargs = dict(
        source_id=source_id,
        limit=limit,
        job_id=job_id,
        run_id=run_id,
    )
    try:
        result = export(storage, out_path, **kwargs)
    except TypeError as exc:
        if "run_id" not in str(exc):
            raise
        kwargs.pop("run_id")
        result = export(storage, out_path, **kwargs)
    return f"Word 阅读包已导出 {result.exported} 篇 → {out_path}"


def _run_auto_export(
    storage,
    source_id: str,
    limit: int,
    export_type: str,
    *,
    job_id: str = "",
    run_id: str = "",
    portable_dir: str | Path,
    word_dir: str | Path = DEFAULT_WORD_DIR,
    research_dir: str | Path,
    portable_export=None,
    word_export=None,
) -> AutoExportResult:
    """执行自动导出，**统一生成 Portable HTML + Word（DOCX）**。

    Portable 阅读包 = HTML + Word（DOCX）一次生成。因此无论旧配置里的
    ``export_type`` 是什么（portable / word / both，或未知值），本次运行都
    统一解释为 ``auto_export=true → HTML + DOCX``，与用户期望一致，同时
    保证老配置不崩溃（字段内部继续保留，但不影响本次输出）。

    返回 ``AutoExportResult``，其中 HTML 与 Word 的成败**独立标记**：
    整体成功要求两者都成功；任一失败则整体为失败，调用方据此输出
    ``EXPORT: SUCCESS`` / ``EXPORT: FAILED`` 及 ``HTML: X / WORD: Y``。
    """
    result = AutoExportResult()

    # ---- 便携阅读包：HTML + Word（DOCX）一次生成 ----
    # 复用 _default_portable_export（portable_reader_package），它会在同一目录
    # 同时写出 index.html 与 Laxinwen-<SITE>-<date>.docx。
    html_msg = ""
    word_msg = ""
    try:
        out_dir, presult = _run_portable_export(
            storage,
            source_id,
            limit,
            job_id=job_id,
            run_id=run_id,
            portable_dir=portable_dir,
            research_dir=research_dir,
            portable_export=portable_export,
        )
        # HTML 成功标志：index.html 已生成
        html_ok = bool(out_dir and (out_dir / "index.html").exists())
        # Word 成功标志：同目录下的 .docx 已生成
        docx_path = out_dir / f"{out_dir.name}.docx"
        word_ok = docx_path.exists()
        result.html_ok = html_ok
        result.word_ok = word_ok
        html_msg = f"HTML 阅读包已导出 {presult.exported} 篇 → {out_dir}"
        word_msg = f"Word 阅读包已导出 {presult.exported} 篇 → {docx_path}"
        if not html_ok:
            logger.error("HTML 导出失败：未找到 index.html → %s", out_dir)
        if not word_ok:
            logger.error("Word 导出失败：未找到 .docx → %s", docx_path)
    except Exception as exc:  # noqa: BLE001
        # 便携导出整体抛异常 → HTML 与 Word 均视为失败
        result.html_ok = False
        result.word_ok = False
        logger.error("便携阅读包（HTML + Word）导出异常: %s", exc)
        result.message = f"便携阅读包导出异常: {exc}"

    result.message = f"{html_msg}; {word_msg}".strip("; ")
    return result


def _unique_dir(base: Path) -> Path:
    """若目录已存在（同一秒重复执行），追加 -2 / -3 … 序号避免覆盖。"""
    return _unique_path(base)


def _unique_path(base: Path) -> Path:
    """若文件/目录已存在（同一秒重复执行），追加 -2 / -3 … 序号避免覆盖。"""
    cand = base
    n = 2
    while cand.exists():
        cand = Path(f"{base}-{n}")
        n += 1
    return cand


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：``news scheduled-fetch [--job-id <id>]``。

    不 import tkinter，完全 headless。读取 data/scheduler.json 中指定 job
    （或默认第一个 job）执行一次抓取。
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="news scheduled-fetch",
        description="自动定时抓取后台入口（headless，被 Windows Task Scheduler 调用）",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="日志文件路径")
    parser.add_argument("--job-id", default=None, help="要执行的定时任务 id（默认取第一个 job）")
    parser.add_argument("--source", default=None, help="覆盖新闻来源（默认读取配置）")
    parser.add_argument("--limit", type=int, default=None, help="覆盖抓取数量（默认读取配置）")
    parser.add_argument(
        "--config",
        default=None,
        help="scheduler 配置文件路径（默认 data/scheduler.json）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = parser.parse_args(argv)

    # 解析路径：相对路径统一基于项目根解析，避免因工作目录不同
    # （Windows Task Scheduler 默认从 C:\Windows\System32 启动）导致
    # 日志/数据库/配置写到错误位置。
    def _abs(p: str) -> str:
        pth = Path(p)
        return str(pth if pth.is_absolute() else (_PROJECT_ROOT / pth))

    db_path = _abs(args.db)
    log_file = _abs(args.log_file)
    config = _abs(args.config) if args.config else None

    # 配置日志（verbose 时提升为 DEBUG）
    _setup_logging(log_file)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    from .scheduler_config import load_job

    if args.job_id:
        cfg = load_job(args.job_id, config)
        if cfg is None:
            logger.error("未找到定时任务 id：%s（请检查 data/scheduler.json）", args.job_id)
            return 1
        if not cfg.enabled:
            # 明确拒绝执行已停用任务，避免误触发。
            logger.warning("定时任务「%s」当前为停用状态，跳过执行。", args.job_id)
            return 0
    else:
        cfg = load_config(config)
    if args.source:
        cfg.source = args.source
    if args.limit is not None:
        cfg.limit = args.limit

    ok, reason = cfg.is_valid()
    if not ok:
        logger.error("自动抓取配置无效：%s", reason)
        return 1

    # 说明：即使 enabled=false，被「立即运行一次」手动触发时也执行。
    # enabled 开关只控制 GUI 是否安装/保留计划任务（见 gui 的安装逻辑），
    # 这里不拦截，保证手动运行始终可用。
    return run_scheduled_fetch(
        cfg,
        db_path=db_path,
        log_file=log_file,
        portable_dir=DEFAULT_PORTABLE_DIR,
        word_dir=DEFAULT_WORD_DIR,
        research_dir=DEFAULT_RESEARCH_DIR,
    )
