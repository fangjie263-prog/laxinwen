"""命令行入口。

用法：
    news fetch [--site <id>] [--limit N] [--retry-failed]
    news list [--source <id>] [--limit N]
    news export --format jsonl|markdown|html|news-html|portable|package [--site <id>] [--article-id <id>] [--limit N]
    news status [--source <id>]
    news process [--site <id>] [--limit N] [--article-id <id>] [--retry-failed]
    news scheduled-fetch [--source <id>] [--limit N] [--config <path>]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from .config import list_available_sites, load_site_config
from .export import export_jsonl, export_markdown
from .html_export import export_html
from .news_archive import export_news_archive
from .portable import export_independent_html, export_portable_package
from .scheduler_config import EXPORT_BOTH, EXPORT_PORTABLE, EXPORT_WORD
from .word_export import export_word_package, default_word_path
from .run_identity import new_run_identity, portable_package_name
from .fetch import FetcherOptions, HttpxFetcher
from .pipeline import Pipeline
from .storage import Storage

logger = logging.getLogger("news")

# 默认路径（可通过环境变量覆盖）
DEFAULT_DB = Path(
    os.environ.get("NEWS_DB", str(Path(__file__).resolve().parents[2] / "data" / "news.db"))
)
DEFAULT_EXPORTS = Path(
    os.environ.get("NEWS_EXPORTS", str(Path(__file__).resolve().parents[2] / "exports"))
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _open_storage(db: str | Path) -> Storage:
    return Storage(db)


def cmd_fetch(args: argparse.Namespace) -> int:
    storage = _open_storage(args.db)
    fetcher = HttpxFetcher(
        FetcherOptions(
            timeout=args.timeout,
            retries=args.retries,
            min_interval=args.interval,
            max_interval=args.interval * 2,
        )
    )
    pipeline = Pipeline(storage, fetcher=fetcher, max_items=args.limit)
    results: dict[str, dict] = {}
    try:
        if args.site:
            sites = [args.site]
        else:
            sites = list_available_sites()
        for sid in sites:
            try:
                stats = pipeline.run_site(sid)
                results[sid] = stats.as_dict()
                print(f"[{sid}] 发现 {stats.discovered} 篇 | "
                      f"跳过重复 {stats.skipped_dup} | 正文成功 {stats.fetched_ok} | "
                      f"抓取失败 {stats.failed} | 可读新闻 {stats.usable}")
            except Exception as exc:  # 单个站点失败不影响其它站点
                logger.error("[%s] 站点处理失败: %s", sid, exc)
                results[sid] = {"error": str(exc)}
        if args.retry_failed:
            stats = pipeline.retry_failed(source_id=args.site)
            print(f"[retry] 重试成功 {stats.fetched_ok} 篇 | 仍失败 {stats.failed} 篇")
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        pipeline.close()
        storage.close()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    storage = _open_storage(args.db)
    try:
        articles = storage.list_articles(source_id=args.source, limit=args.limit)
        for art in articles:
            pub = art.published_at.isoformat() if art.published_at else "-"
            print(f"#{art.id:<6} [{art.source_id}] {pub}  {art.title}")
            print(f"        {art.canonical_url}")
    finally:
        storage.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    storage = _open_storage(args.db)
    try:
        total = storage.count(args.source)
        by_status = storage.count_by_status(args.source)
        print(f"数据库: {args.db}")
        print(f"站点: {', '.join(list_available_sites()) or '(无)'}")
        print(f"文章总数: {total}")
        for status, count in sorted(by_status.items()):
            print(f"  - {status}: {count}")
        # 第二阶段：AI 分析统计
        print(f"AI 分析记录: {storage.count_analysis(source_id=args.source)}")
        print(
            f"  - 成功: {storage.count_analysis(source_id=args.source, status='success')}"
        )
        print(
            f"  - 失败: {storage.count_analysis(source_id=args.source, status='failed')}"
        )
    finally:
        storage.close()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """news gui —— 启动 Windows 桌面版 Laxinwen News Reader。"""
    from .gui import run_gui

    return run_gui(db_path=args.db, site=args.site)


def cmd_serve(args: argparse.Namespace) -> int:
    """news serve —— 启动本地 HTTP 阅读服务器（仅 127.0.0.1）。

    把 ``data/export/`` 作为静态目录，让浏览器扩展（如 Immersive Translate）
    能像处理普通网页一样处理本地 News Archive / AI Research 页面。
    端口被占用时自动选择可用端口；Ctrl+C 停止。
    """
    from .reader_server import ReaderServer

    export_root = Path(args.export_root)
    server = ReaderServer(export_root)
    server.start()
    print(f"本地 HTTP 阅读模式已启动：http://127.0.0.1:{server.port}/ （仅本机 127.0.0.1）")
    try:
        print("  新闻库：   http://127.0.0.1:%d/news-html/eco/index.html" % server.port)
        print("  AI 研究：  http://127.0.0.1:%d/html/index.html" % server.port)
        print("按 Ctrl+C 停止……")
        while True:
            import time

            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        server.stop()
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    """news process —— 把已入库的文章交给 AI 生成结构化分析并保存。

    默认只处理：已成功抓取、但还没有 AI analysis 的文章。
    单篇失败不影响其它文章。
    """
    from .ai import ArticleProcessor, AIProviderConfig, build_provider, load_dotenv

    load_dotenv()  # 支持项目根 .env（仅当环境未设置时）
    storage = _open_storage(args.db)
    processor: Optional[ArticleProcessor] = None
    try:
        if args.ai_provider or args.ai_base_url or args.ai_api_key or args.ai_model:
            # 允许通过 CLI 临时覆盖（便于切换 Provider），Key 仍来自环境变量/.env
            cfg = AIProviderConfig.from_env()
            if args.ai_provider:
                cfg.provider = args.ai_provider
            if args.ai_base_url:
                cfg.base_url = args.ai_base_url
            if args.ai_api_key:
                cfg.api_key = args.ai_api_key
            if args.ai_model:
                cfg.model = args.ai_model
            provider = build_provider(cfg)
            processor = ArticleProcessor(storage, provider=provider, config=cfg)
        else:
            processor = ArticleProcessor(storage)

        print("AI Provider 配置:")
        for k, v in processor.config.redacted().items():
            print(f"  {k}: {v}")
        print()

        stats = processor.process_batch(
            source_id=args.site,
            limit=args.limit,
            article_id=args.article_id,
            retry_failed=args.retry_failed,
        )
        print(
            f"处理结果: 共 {stats.total} 篇 | 成功 {stats.ok} | 失败 {stats.failed}"
        )
        if stats.errors:
            print("失败明细:")
            for err in stats.errors:
                print(f"  - {err}")
        # token usage / cost 汇总（若 API 返回）
        usage_total = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        credit_total = 0.0
        # 从数据库读取本次成功记录的 usage
        rows = storage.list_analysis(limit=1000)
        for row in rows:
            if row["status"] != "success":
                continue
            import json as _json

            try:
                u = _json.loads(row["usage_json"] or "{}")
            except (ValueError, TypeError):
                u = {}
            for key in usage_total:
                usage_total[key] += int(u.get(key, 0) or 0)
            credit_total += float(u.get("credit", 0) or 0)
        print("token usage（累计成功记录）:")
        for k, v in usage_total.items():
            print(f"  {k}: {v}")
        if credit_total:
            print(f"  credit/cost: {credit_total:.4f}")
        return 0
    except Exception as exc:
        print(f"AI 处理失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if processor is not None:
            processor.close()
        storage.close()


def _export_format(args: argparse.Namespace, storage: Storage, fmt: str) -> int:
    """导出单个格式，返回退出码。``fmt`` 为 --format 值（word/portable/reader/...）。"""
    if fmt == "html":
        # HTML 默认输出到 data/export/html/（研究结果展示层专用目录）
        out_dir = Path(args.output) if args.output else Path("data") / "export" / "html"
        result = export_html(
            storage,
            out_dir,
            source_id=args.site or args.source,
            article_id=args.article_id,
        )
        print(f"HTML 导出目录：\n{out_dir}/")
        print(f"成功导出：\n{result.exported}")
        print(f"跳过：\n{result.skipped}")
        print(f"失败：\n{result.failed}")
        return 0

    if fmt == "word":
        # Word 研究阅读包：单个 .docx 文件，默认输出到 data/export/word/
        site = args.site or args.source
        if not site:
            sites = list_available_sites()
            if len(sites) == 1:
                site = sites[0]
            else:
                print(
                    "Word 导出需要指定站点：--site <id>（当前可用："
                    + ", ".join(sites)
                    + "）",
                    file=sys.stderr,
                )
                return 2
        identity = new_run_identity(job_id=args.job_id, output_root=Path("data") / "export" / "word", source_id=site)
        out = Path(args.output) if args.output else default_word_path(
            site, job_id=args.job_id, run_id=identity.run_id
        )
        result = export_word_package(
            storage,
            out,
            source_id=site,
            limit=args.limit,
            job_id=args.job_id,
            run_id=identity.run_id,
        )
        print(f"Word 研究阅读包导出：\n{out}")
        print(f"共 {result.exported} 篇（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）")
        print("目录条目可点击跳转到对应新闻；原文链接可在 Word 中点击打开浏览器。")
        return 0

    if fmt in ("portable", "package"):
        # 便携式导出：独立 HTML / HTML 新闻包（默认输出到 data/export/portable/）
        site = args.site or args.source
        if not site:
            sites = list_available_sites()
            if len(sites) == 1:
                site = sites[0]
            else:
                print(
                    "便携式导出需要指定站点：--site <id>（当前可用："
                    + ", ".join(sites)
                    + "）",
                    file=sys.stderr,
                )
                return 2
        from .portable import default_independent_path, default_package_path

        research_root = Path("data") / "export" / "html"
        if fmt == "portable":
            out = (
                Path(args.output)
                if args.output
                else default_independent_path(site)
            )
            result = export_independent_html(
                storage,
                out,
                source_id=site,
                limit=args.limit,
                research_root=research_root,
            )
            print(f"独立 HTML 导出：\n{out}")
            print(f"共 {result.exported} 篇（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）")
            print("双击该 .html 即可在没有 laxinwen 的电脑上直接阅读。")
            return 0

        identity = new_run_identity(job_id=args.job_id, output_root=Path("data") / "export" / "portable", source_id=site)
        job_suffix = f"-{args.job_id}" if args.job_id else ""
        out = Path(args.output) if args.output else Path("data") / "export" / "portable" / portable_package_name(
            site, identity.started_at, identity.run_id, args.job_id
        )
        result = export_portable_package(
            storage,
            out,
            source_id=site,
            limit=args.limit,
            research_root=research_root,
            run_id=identity.run_id,
        )
        print(f"HTML 新闻包导出目录：\n{out}/")
        print(f"共 {result.exported} 篇（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）")
        print("可直接复制整个目录到其它电脑，双击 index.html 阅读。")
        return 0

    if fmt in ("reader", "portable-reader"):
        # 便携阅读包：index + articles + server.py + Open-Reader.bat
        site = args.site or args.source
        if not site:
            sites = list_available_sites()
            if len(sites) == 1:
                site = sites[0]
            else:
                print(
                    "便携阅读包导出需要指定站点：--site <id>（当前可用："
                    + ", ".join(sites)
                    + "）",
                    file=sys.stderr,
                )
                return 2
        from .portable import export_portable_reader_package, default_reader_path

        research_root = Path("data") / "export" / "html"
        identity = new_run_identity(job_id=args.job_id, output_root=Path("data") / "export" / "portable", source_id=site)
        job_suffix = f"-{args.job_id}" if args.job_id else ""
        out = Path(args.output) if args.output else Path("data") / "export" / "portable" / portable_package_name(
            site, identity.started_at, identity.run_id, args.job_id
        )
        result = export_portable_reader_package(
            storage,
            out,
            source_id=site,
            limit=args.limit,
            research_root=research_root,
            job_id=args.job_id,
            run_id=identity.run_id,
        )
        print(f"便携阅读包导出目录：\n{out}/")
        print(f"共 {result.exported} 篇（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）")
        print("给他人使用：复制整个目录，双击 Open-Reader.bat，")
        print("浏览器将通过 http://127.0.0.1 打开（而非 file://），沉浸式翻译等扩展可正常工作。")
        return 0

    if fmt == "news-html":
        # News Archive HTML：默认输出到 data/export/news-html/<site>/（阅读目录专用目录）
        site = args.site or args.source
        if not site:
            # 若数据库只有一个站点则自动推断；否则报错提示 --site
            from .config import list_available_sites as _las

            sites = _las()
            if len(sites) == 1:
                site = sites[0]
            else:
                print(
                    "News Archive 导出需要指定站点：--site <id>（当前可用："
                    + ", ".join(sites)
                    + "）",
                    file=sys.stderr,
                )
                return 2
        out_dir = (
            Path(args.output)
            if args.output
            else Path("data") / "export" / "news-html" / site
        )
        result = export_news_archive(
            storage,
            out_dir,
            source_id=site,
            limit=args.limit,
        )
        print(f"News Archive 导出目录：\n{out_dir}/")
        print(f"最近 {result.exported} 条：")
        print(f"  ✓ AI 已分析 {result.analyzed_ok}")
        print(f"  ⚠ AI 分析失败 {result.analyzed_failed}")
        print(f"  ○ 尚未分析 {result.unanalyzed}")
        print(f"  失败（导出错误）{result.failed}")
        return 0

    output = Path(args.output) if args.output else Path(DEFAULT_EXPORTS)
    if fmt == "jsonl":
        path = output / "news.jsonl"
        n = export_jsonl(storage, path, source_id=args.site or args.source)
        print(f"已导出 {n} 篇 → {path}")
        return 0
    if fmt == "markdown":
        n = export_markdown(storage, output, source_id=args.site or args.source)
        print(f"已导出 {n} 篇 → {output}/YYYY/MM/")
        return 0
    print(f"不支持的格式: {fmt}", file=sys.stderr)
    return 2


def cmd_export(args: argparse.Namespace) -> int:
    storage = _open_storage(args.db)
    try:
        # --type 批量导出：portable / word / both（等价于对应 --format 组合）
        if args.type:
            if args.type == EXPORT_BOTH:
                rc = _export_format(args, storage, "reader")  # portable 阅读包
                if rc != 0:
                    return rc
                return _export_format(args, storage, "word")
            if args.type == EXPORT_WORD:
                return _export_format(args, storage, "word")
            # EXPORT_PORTABLE → 便携阅读包
            return _export_format(args, storage, "reader")

        if args.format is None:
            print("需要指定 --format 或 --type", file=sys.stderr)
            return 2
        return _export_format(args, storage, args.format)
    finally:
        storage.close()


def cmd_scheduled_fetch(args: argparse.Namespace) -> int:
    """news scheduled-fetch —— headless 自动定时抓取后台入口。

    被 Windows Task Scheduler 调用；复用现有 pipeline，不 import tkinter。
    读取 data/scheduler.json 配置（可被 --source / --limit 覆盖）。
    """
    from .scheduled_fetch import main as scheduled_fetch_main

    return scheduled_fetch_main(argv=_build_scheduled_fetch_argv(args))


def cmd_scheduler(args: argparse.Namespace) -> int:
    """news scheduler <install|delete|run|status> [job_id] —— Windows Task Scheduler 管理。

    从 data/scheduler.json 读取指定 job（默认第一个）执行对应操作。
    供 GUI / BAT 复用同一套逻辑。
    """
    from .scheduler_config import NotionSyncSchedulerConfig, load_config, load_job
    from .task_scheduler import (
        delete_task,
        install_task,
        query_task,
        run_now,
        delete_notion_sync_task,
        install_notion_sync_task,
        query_notion_sync_task,
        run_notion_sync_task,
    )

    action = args.scheduler_action
    target = args.job_id
    notion_alias = action.endswith("-notion-sync")
    if notion_alias:
        action = action.removesuffix("-notion-sync")
        target = "notion-sync"
    if target == "notion-sync":
        cfg = NotionSyncSchedulerConfig(
            enabled=True,
            frequency=args.frequency,
            time=args.time,
            minute_offset=args.minute_offset,
        )
        op = {"install": install_notion_sync_task, "delete": delete_notion_sync_task,
              "run": run_notion_sync_task, "status": query_notion_sync_task}[action]
        result = op(cfg, project_root=args.project_root) if action == "install" else op(cfg)
        ok = result.get("ok")
        print(f"{'OK' if ok else 'ERROR'}: {result.get('message', '')}")
        if result.get("cmd"):
            print("命令:", " ".join(result["cmd"]))
        if action == "status" and not result.get("executed", True):
            print("预期配置:", cfg.frequency, cfg.time if cfg.frequency == "daily" else f"minute={cfg.minute_offset}")
        return 0 if ok else 1

    if args.job_id:
        cfg = load_job(args.job_id, args.config)
        if cfg is None:
            print(f"ERROR: 未找到定时任务 id：{args.job_id}")
            return 1
    else:
        cfg = load_config(args.config)
    op = {
        "install": install_task,
        "delete": delete_task,
        "run": run_now,
        "status": query_task,
    }[action]
    result = op(cfg, project_root=args.project_root) if action == "install" else op(cfg)
    ok = result.get("ok")
    print(f"{'OK' if ok else 'ERROR'}: {result.get('message', '')}")
    cmd = result.get("cmd")
    if cmd:
        print("命令:", " ".join(cmd))
    if action == "status" and ok:
        print(result.get("message", ""))
    if not ok:
        return 1
    if result.get("executed") is False:
        # 在 Linux/headless 上仅生成命令，不真正执行
        print("（当前环境无法执行 Windows Task Scheduler —— REQUIRES WINDOWS REAL TEST）")
    return 0


def cmd_notion_sync(args: argparse.Namespace) -> int:
    """扫描 Portable Reader 包并同步到 Notion。"""
    from .notion_sync import NotionSyncError, run_sync

    log_path = Path(__file__).resolve().parents[2] / "data" / "logs" / "notion-sync.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(message: str) -> None:
        print(message)
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except OSError:
            logger.warning("无法写入 Notion Sync 日志：%s", log_path)

    emit("NOTION SYNC START")
    try:
        messages = run_sync(
            token=args.notion_token,
            root_page_id=args.root_page_id,
            export_root=args.export_root,
            state_path=args.state,
            timeout=args.timeout,
            dry_run=args.dry_run,
            researchreader_output=args.researchreader_output,
            researchreader_books=args.researchreader_books,
        )
    except NotionSyncError as exc:
        message = f"NOTION SYNC FAILED · {exc}"
        emit(message)
        print(message, file=sys.stderr)
        return 1
    for message in messages:
        emit(message)
    failed = any(message.startswith("SYNC FAILED") for message in messages)
    if failed:
        emit("NOTION SYNC FAILED · one or more packages failed")
    elif not messages or all(message.startswith("SYNC SKIP") for message in messages):
        emit("NOTION SYNC COMPLETE · nothing to upload")
    else:
        emit("NOTION SYNC COMPLETE")
    return 1 if failed else 0


def _build_scheduled_fetch_argv(args: argparse.Namespace) -> list[str]:
    """把 cli 的 args 转发给 scheduled_fetch.main（避免重复构造 argparse）。"""
    argv = ["--db", str(args.db), "--log-file", str(args.log_file)]
    if args.config:
        argv += ["--config", args.config]
    if getattr(args, "job_id", None):
        argv += ["--job-id", args.job_id]
    if args.source:
        argv += ["--source", args.source]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if getattr(args, "verbose", False):
        argv += ["-v"]
    return argv


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news",
        description="laxinwen —— 个人金融新闻采集与研究数据库",
    )
    parser.add_argument("--version", action="version", version="laxinwen 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="抓取新闻")
    p_fetch.add_argument("--site", help="只抓取指定站点（默认全部）")
    p_fetch.add_argument("--limit", type=int, default=30, help="每站最多处理候选文章数")
    p_fetch.add_argument("--timeout", type=float, default=20.0, help="HTTP 超时（秒）")
    p_fetch.add_argument("--retries", type=int, default=3, help="HTTP 重试次数")
    p_fetch.add_argument("--interval", type=float, default=2.0, help="同域请求最小间隔（秒）")
    p_fetch.add_argument("--retry-failed", action="store_true", help="抓取后重试失败文章")
    _add_common_args(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_list = sub.add_parser("list", help="列出最近新闻")
    p_list.add_argument("--source", help="按站点过滤")
    p_list.add_argument("--limit", type=int, default=30, help="条数")
    _add_common_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="显示数据库和抓取状态")
    p_status.add_argument("--source", help="按站点过滤")
    _add_common_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_gui = sub.add_parser("gui", help="启动 Windows 桌面 GUI（Laxinwen News Reader）")
    p_gui.add_argument("--site", default="eco", help="初始来源 id：eco / hkej / all（默认 eco）")
    _add_common_args(p_gui)
    p_gui.set_defaults(func=cmd_gui)

    p_serve = sub.add_parser(
        "serve",
        help="启动本地 HTTP 阅读服务器（仅 127.0.0.1，端口自动选择）",
    )
    p_serve.add_argument(
        "--export-root",
        default=str(Path("data") / "export"),
        help="静态目录（默认 data/export）",
    )
    _add_common_args(p_serve)
    p_serve.set_defaults(func=cmd_serve)

    p_process = sub.add_parser("process", help="AI 处理已入库文章（生成结构化分析）")
    p_process.add_argument("--site", help="只处理指定站点（默认全部）")
    p_process.add_argument("--limit", type=int, default=5, help="最多处理篇数（默认 5）")
    p_process.add_argument("--article-id", type=int, default=None, help="只处理指定文章 ID")
    p_process.add_argument("--retry-failed", action="store_true", help="重新处理之前 AI 失败的文章")
    # 临时覆盖 Provider 配置（Key 仍来自环境变量/.env）
    p_process.add_argument("--ai-provider", default=None, help="临时覆盖 AI_PROVIDER")
    p_process.add_argument("--ai-base-url", default=None, help="临时覆盖 AI_BASE_URL")
    p_process.add_argument("--ai-api-key", default=None, help="临时覆盖 AI_API_KEY（仅命令行临时传入，不入代码）")
    p_process.add_argument("--ai-model", default=None, help="临时覆盖 AI_MODEL")
    _add_common_args(p_process)
    p_process.set_defaults(func=cmd_process)

    p_sched = sub.add_parser(
        "scheduled-fetch",
        help="headless 自动定时抓取（被 Windows Task Scheduler 调用）",
    )
    p_sched.add_argument("--job-id", default=None, help="要执行的定时任务 id（默认取第一个 job）")
    p_sched.add_argument("--source", default=None, help="覆盖新闻来源（默认读取配置）")
    p_sched.add_argument("--limit", type=int, default=None, help="覆盖抓取数量（默认读取配置）")
    p_sched.add_argument("--config", default=None, help="scheduler 配置文件路径（默认 data/scheduler.json）")
    p_sched.add_argument(
        "--log-file",
        default=str(Path("data") / "logs" / "scheduled-fetch.log"),
        help="日志文件路径（默认 data/logs/scheduled-fetch.log）",
    )
    _add_common_args(p_sched)
    p_sched.set_defaults(func=cmd_scheduled_fetch)

    p_scheduler = sub.add_parser(
        "scheduler",
        help="Windows Task Scheduler 管理：install / delete / run / status",
    )
    p_scheduler.add_argument(
        "scheduler_action",
        choices=["install", "delete", "run", "status", "install-notion-sync", "delete-notion-sync", "run-notion-sync", "status-notion-sync"],
        help="操作：install=安装/更新，delete=删除，run=立即运行，status=查询",
    )
    p_scheduler.add_argument(
        "job_id",
        nargs="?",
        default=None,
        help="定时任务 id（默认取第一个 job）",
    )
    p_scheduler.add_argument("--config", default=None, help="scheduler 配置文件路径（默认 data/scheduler.json）")
    p_scheduler.add_argument("--project-root", default=None, help="项目根目录（默认自动探测）")
    p_scheduler.add_argument("--frequency", choices=["hourly", "daily"], default="hourly", help="Notion Sync 频率")
    p_scheduler.add_argument("--time", default="08:10", help="Notion Sync 每日时间 HH:MM")
    p_scheduler.add_argument("--minute-offset", type=int, default=10, help="Notion Sync 每小时分钟偏移（0-59）")
    _add_common_args(p_scheduler)
    p_scheduler.set_defaults(func=cmd_scheduler)

    p_notion = sub.add_parser(
        "notion-sync",
        help="扫描 Portable 阅读包并同步到 Notion",
    )
    p_notion.add_argument("--export-root", default=str(Path("data") / "export" / "portable"), help="Portable 导出目录")
    p_notion.add_argument("--state", default=str(Path("data") / "notion-sync.json"), help="本地同步状态文件")
    p_notion.add_argument("--notion-token", default=None, help="临时覆盖 NOTION_TOKEN（不写入文件）")
    p_notion.add_argument("--root-page-id", default=None, help="临时覆盖 NOTION_ROOT_PAGE_ID")
    p_notion.add_argument("--timeout", type=float, default=60.0, help="Notion API 请求超时（秒）")
    p_notion.add_argument("--dry-run", action="store_true", help="只扫描并显示待同步包，不调用 Notion API")
    p_notion.add_argument("--researchreader-output", default=None, help="ResearchReader HTML 输出目录")
    p_notion.add_argument("--researchreader-books", default=None, help="ResearchReader EPUB/PDF 目录")
    p_notion.set_defaults(func=cmd_notion_sync, verbose=False)

    p_export = sub.add_parser(
        "export",
        help="导出 JSONL / Markdown / HTML / News Archive HTML / 便携 HTML / Word",
    )
    p_export.add_argument(
        "--format",
        choices=["jsonl", "markdown", "html", "news-html", "portable", "package", "reader", "word"],
        help="导出格式（word = Word DOCX 研究阅读包）",
    )
    p_export.add_argument(
        "--type",
        choices=[EXPORT_PORTABLE, EXPORT_WORD, EXPORT_BOTH],
        default=None,
        help="按自动导出类型批量导出：portable / word / both（等价于对应 --format 组合）",
    )
    p_export.add_argument("--site", help="按站点过滤（HTML / news-html / word 导出）")
    p_export.add_argument("--source", help="按站点过滤（兼容旧参数）")
    p_export.add_argument("--article-id", type=int, default=None, help="只导出指定文章（HTML）")
    p_export.add_argument("--limit", type=int, default=100, help="News Archive 显示最近 N 条（默认 100）")
    p_export.add_argument("--job-id", default=None, help="job id（用于 Word 文件名的 job 后缀，区分不同任务）")
    p_export.add_argument("--output", default=None, help="导出目录（HTML 默认 data/export/html/，news-html 默认 data/export/news-html/<site>/，portable/package 默认 data/export/portable/，word 默认 data/export/word/）")
    _add_common_args(p_export)
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
