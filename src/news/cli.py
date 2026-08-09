"""命令行入口。

用法：
    news fetch [--site <id>] [--limit N] [--retry-failed]
    news list [--source <id>] [--limit N]
    news export --format jsonl|markdown|html [--source <id>] [--article-id <id>]
    news status [--source <id>]
    news process [--site <id>] [--limit N] [--article-id <id>] [--retry-failed]
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
                      f"跳过重复 {stats.skipped_dup} | 下载 {stats.fetched_ok} | "
                      f"提取 {stats.extracted_ok} | 失败 {stats.failed}")
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


def cmd_export(args: argparse.Namespace) -> int:
    storage = _open_storage(args.db)
    try:
        if args.format == "html":
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

        output = Path(args.output) if args.output else Path(DEFAULT_EXPORTS)
        if args.format == "jsonl":
            path = output / "news.jsonl"
            n = export_jsonl(storage, path, source_id=args.site or args.source)
            print(f"已导出 {n} 篇 → {path}")
        elif args.format == "markdown":
            n = export_markdown(storage, output, source_id=args.site or args.source)
            print(f"已导出 {n} 篇 → {output}/YYYY/MM/")
        else:
            print(f"不支持的格式: {args.format}", file=sys.stderr)
            return 2
    finally:
        storage.close()
    return 0


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

    p_export = sub.add_parser("export", help="导出 JSONL / Markdown / HTML")
    p_export.add_argument("--format", choices=["jsonl", "markdown", "html"], required=True)
    p_export.add_argument("--site", help="按站点过滤（HTML 导出）")
    p_export.add_argument("--source", help="按站点过滤（兼容旧参数）")
    p_export.add_argument("--article-id", type=int, default=None, help="只导出指定文章（HTML）")
    p_export.add_argument("--output", default=None, help="导出目录（HTML 默认 data/export/html/）")
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
