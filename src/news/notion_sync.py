"""独立的 Notion 新闻阅读包归档同步层。

本模块只扫描现有 Portable Reader 导出，不参与抓取、AI 处理或 Word/HTML 生成。
Notion API 通过 httpx 调用，文件使用官方 File Upload API 后作为 file block
附加到日期页面；本地状态文件保证重试和重复运行幂等。
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
from selectolax.parser import HTMLParser

from .ai.provider import load_dotenv
from .run_identity import parse_run_id

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
DEFAULT_EXPORT_ROOT = Path("data") / "export" / "portable"
DEFAULT_STATE_PATH = Path("data") / "notion-sync.json"
_PACKAGE_RE = re.compile(
    r"^Laxinwen-(?P<source>[A-Za-z0-9_]+)-"
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:-(?P<run_date>\d{8})-(?P<run_time>\d{6})(?:-(?P<run_suffix>\d{2}))?)?"
    r"(?:-(?P<time>\d{6}))?"
    r"(?:-(?P<job>.+))?$",
    re.IGNORECASE,
)
_ARTICLE_RE = re.compile(r"(?:id|data-article-id)=[\"']article-(\d+)")
DEFAULT_NOTION_MAX_UPLOAD_MB = 4.5
_NOTION_MB = 1024 * 1024
_TRANSLATED_HTML_RE = re.compile(
    r"^.+-(?:zh-)?CN-translation(?:\s*\(\d+\))?\.html$", re.IGNORECASE
)
_DUAL_HTML_RE = re.compile(
    r"^.+-(?:zh-)?CN-dual(?:\s*\(\d+\))?\.html$", re.IGNORECASE
)


class NotionSyncError(RuntimeError):
    """可安全展示给 CLI/GUI 的 Notion 同步错误。"""


@dataclass(frozen=True)
class ExportPackage:
    source: str
    date: str
    job_id: str
    package_path: Path
    index_path: Path
    docx_path: Path
    article_count: Optional[int]
    package_key: str
    origin: str = "laxinwen"
    run_id: str = ""
    run_timestamp: str = ""
    artifact_variant: str = "original"
    html_files: tuple[Path, ...] = ()
    extra_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class UploadArtifact:
    """一个将独立上传到 Notion 的、已通过大小检查的文件。"""

    key: str
    path: Path
    label: str
    kind: str
    content_type: str
    fingerprint: str
    artifact_variant: str = "original"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def notion_max_upload_bytes(value: Optional[float] = None) -> int:
    """返回 Notion 单文件安全上限；默认 4.5 MiB，可由环境变量覆盖。"""
    raw = value
    if raw is None:
        raw = os.environ.get("NOTION_MAX_UPLOAD_MB", str(DEFAULT_NOTION_MAX_UPLOAD_MB))
    try:
        mb = float(raw)
    except (TypeError, ValueError) as exc:
        raise NotionSyncError("NOTION_MAX_UPLOAD_MB 必须是正数") from exc
    if mb <= 0:
        raise NotionSyncError("NOTION_MAX_UPLOAD_MB 必须是正数")
    return max(1, int(mb * _NOTION_MB))


def _package_key(root: Path, package_dir: Path, files: Iterable[Path]) -> str:
    """生成包含路径和文件元数据的身份，不只依赖目录名。"""
    try:
        relative = package_dir.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = package_dir.resolve().as_posix()
    parts = [relative]
    for path in sorted(files):
        stat = path.stat()
        parts.append(f"{path.relative_to(package_dir).as_posix()}:{stat.st_size}:{stat.st_mtime_ns}")
    return _sha256_text("\n".join(parts))


def _count_articles(index_path: Path) -> Optional[int]:
    try:
        text = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ids = [int(value) for value in _ARTICLE_RE.findall(text)]
    return max(ids) if ids else None


class ExportPackageScanner:
    """扫描 Portable Reader 目录中的完整阅读包。"""

    def __init__(self, export_root: str | Path = DEFAULT_EXPORT_ROOT):
        self.export_root = Path(export_root)

    def scan(self) -> list[ExportPackage]:
        if not self.export_root.exists():
            return []
        packages: list[ExportPackage] = []
        for path in sorted(self.export_root.iterdir()):
            if not path.is_dir():
                continue
            match = _PACKAGE_RE.match(path.name)
            if not match:
                continue
            index_path = path / "index.html"
            docx_candidates = sorted(path.glob("*.docx"))
            if not index_path.is_file() or len(docx_candidates) != 1:
                logger.debug("忽略不完整 Portable 包：%s", path)
                continue
            data = match.groupdict()
            run_id = ""
            if data.get("run_date") and data.get("run_time"):
                run_id = f"{data['run_date']}-{data['run_time']}"
                if data.get("run_suffix"):
                    run_id += f"-{data['run_suffix']}"
            elif data.get("time"):
                run_id = f"{data['date'].replace('-', '')}-{data['time']}"
            files = [index_path, docx_candidates[0]]
            html_files = tuple(
                item for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() != ".docx"
            )
            packages.append(
                ExportPackage(
                    source=data["source"].lower(),
                    date=data["date"],
                    job_id=data.get("job") or "",
                    package_path=path,
                    index_path=index_path,
                    docx_path=docx_candidates[0],
                    article_count=_count_articles(index_path),
                    package_key=_package_key(self.export_root, path, files),
                    run_id=run_id,
                    run_timestamp=run_id,
                    html_files=html_files,
                )
            )
        return sorted(packages, key=lambda item: (item.date, item.source, item.package_path.name))


class ResearchReaderScanner:
    """扫描 ResearchReader 输出，仅归档 daily.html、images 和原始书籍文件。"""

    _DIR_RE = re.compile(r"^(?P<source>[a-z0-9][a-z0-9-]*)_(?P<date>\d{2}-\d{2}-\d{4})_Kobo$", re.IGNORECASE)
    _SOURCE_NAMES = {
        "barrons": "Barron's",
        "the-wall-street-journal": "WSJ",
        "the-economist-asia-pacific": "Economist",
    }

    def __init__(self, output_root: str | Path, books_root: str | Path):
        self.output_root = Path(output_root)
        self.books_root = Path(books_root)

    def _source_name(self, value: str) -> str:
        return self._SOURCE_NAMES.get(value.lower(), value.replace("-", " ").title())

    def scan(self) -> list[ExportPackage]:
        packages: list[ExportPackage] = []
        if self.output_root.exists():
            for path in sorted(self.output_root.iterdir()):
                if not path.is_dir():
                    continue
                match = self._DIR_RE.match(path.name)
                daily = path / "daily.html"
                images = path / "images"
                if not match or not daily.is_file():
                    continue
                date = datetime.strptime(match.group("date"), "%d-%m-%Y").strftime("%Y-%m-%d")
                html_files = [daily]
                if images.is_dir():
                    html_files.extend(item for item in images.rglob("*") if item.is_file())
                variant_files = sorted(
                    item for item in path.glob("*.html")
                    if item.is_file() and _html_variant(item)
                )
                key_parts = [
                    "researchreader", self._source_name(match.group("source")), date,
                    path.name,
                ]
                key = _sha256_text("|".join(key_parts))
                packages.append(ExportPackage(
                    source=self._source_name(match.group("source")), date=date, job_id="",
                    package_path=path, index_path=daily, docx_path=None,
                    article_count=None, package_key=key, origin="researchreader",
                    html_files=tuple(html_files),
                    extra_files=tuple(variant_files),
                ))
        unmatched_books: list[ExportPackage] = []
        for path in sorted(self.books_root.glob("*")) if self.books_root.exists() else []:
            if not path.is_file() or path.suffix.lower() not in {".epub", ".pdf"}:
                continue
            match = re.search(r"(?P<source>[a-z][a-z0-9-]*)\s+(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})", path.stem, re.IGNORECASE)
            if not match:
                continue
            source = self._source_name(match.group("source"))
            date = f"{match.group('year')}-{match.group('month')}-{match.group('day')}"
            key = _sha256_text(f"researchreader|{source}|{date}|{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}")
            book_package = ExportPackage(
                source=source, date=date, job_id="", package_path=path.parent,
                index_path=path, docx_path=None, article_count=None, package_key=key,
                origin="researchreader", extra_files=(path,),
            )
            matched = next((item for item in packages if item.source == source and item.date == date), None)
            if matched:
                packages[packages.index(matched)] = replace(
                    matched, extra_files=matched.extra_files + (path,)
                )
            else:
                unmatched_books.append(book_package)
        packages.extend(unmatched_books)
        return packages


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"packages": {}, "date_pages": {}, "source_pages": {}, "run_pages": {}, "variant_pages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotionSyncError(f"无法读取状态文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise NotionSyncError(f"状态文件不是 JSON 对象：{path}")
    data.setdefault("packages", {})
    data.setdefault("date_pages", {})
    data.setdefault("source_pages", {})
    data.setdefault("run_pages", {})
    data.setdefault("variant_pages", {})
    return data


@contextmanager
def _state_lock(path: Path, *, timeout: float = 30.0):
    """Hold a cross-process lock for the entire Notion sync lifecycle."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + timeout
    locked = False
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise NotionSyncError(f"Notion 同步状态文件正在被占用：{lock_path}") from exc
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".notion-sync-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(3):
            try:
                os.replace(temp_name, path)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_notion_config(
    *,
    token: Optional[str] = None,
    root_page_id: Optional[str] = None,
    export_root: str | Path = DEFAULT_EXPORT_ROOT,
    state_path: str | Path = DEFAULT_STATE_PATH,
    researchreader_output: str | Path | None = None,
    researchreader_books: str | Path | None = None,
) -> dict[str, Any]:
    load_dotenv()
    return {
        "token": (token or os.environ.get("NOTION_TOKEN", "")).strip(),
        "root_page_id": (root_page_id or os.environ.get("NOTION_ROOT_PAGE_ID", "")).strip(),
        "export_root": Path(os.environ.get("NOTION_EXPORT_ROOT", str(export_root))),
        "state_path": Path(os.environ.get("NOTION_SYNC_STATE", str(state_path))),
        "max_upload_bytes": notion_max_upload_bytes(),
        "researchreader_output": Path(os.environ["RESEARCHREADER_OUTPUT_ROOT"]) if os.environ.get("RESEARCHREADER_OUTPUT_ROOT") else (Path(researchreader_output) if researchreader_output else None),
        "researchreader_books": Path(os.environ["RESEARCHREADER_BOOKS_ROOT"]) if os.environ.get("RESEARCHREADER_BOOKS_ROOT") else (Path(researchreader_books) if researchreader_books else None),
    }


class NotionClient:
    """最小 Notion API client；不引入第三方 Notion SDK。"""

    def __init__(self, token: str, *, timeout: float = 60.0, client: Optional[httpx.Client] = None):
        if not token:
            raise NotionSyncError("缺少 NOTION_TOKEN")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}))
        url = path if path.startswith(("http://", "https://")) else NOTION_API + path
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise NotionSyncError(f"Notion 网络错误：{exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            raise NotionSyncError(f"Notion API HTTP {response.status_code}：{detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise NotionSyncError("Notion API 返回了非 JSON 响应") from exc

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def child_pages(self, parent_id: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        cursor: Optional[str] = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            payload = self._request("GET", f"/blocks/{parent_id}/children", params=params)
            for block in payload.get("results", []):
                if block.get("type") == "child_page":
                    result.append({"id": block["id"], "title": block.get("child_page", {}).get("title", "")})
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return result

    def find_or_create_child_page(
        self, parent_id: str, title: str, *, position: Optional[dict[str, Any]] = None
    ) -> str:
        for child in self.child_pages(parent_id):
            if child["title"].strip() == title:
                return child["id"]
        payload = {
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        }
        if position:
            position = dict(position)
            # Compatibility guard for callers from older revisions.  The
            # current Notion API names these positions page_start/page_end.
            position["type"] = {
                "start": "page_start",
                "end": "page_end",
            }.get(position.get("type"), position.get("type"))
            if position.get("type") not in {"after_block", "page_start", "page_end"}:
                raise NotionSyncError(f"不支持的 Notion 页面位置：{position.get('type')}")
            payload["position"] = position
        return self._request("POST", "/pages", json=payload)["id"]

    def date_page_position(self, parent_id: str, date: str) -> dict[str, Any]:
        """计算新 Date Page 的位置，按真实日期从新到旧插入。"""
        try:
            target = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return {"type": "page_start"}
        dated: list[tuple[Any, str]] = []
        for child in self.child_pages(parent_id):
            try:
                child_date = datetime.strptime(child["title"].strip(), "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            dated.append((child_date, child["id"]))
        newer = [(child_date, child_id) for child_date, child_id in dated if child_date > target]
        if not newer:
            return {"type": "page_start"}
        _, nearest_newer_id = min(newer, key=lambda item: item[0])
        return {"type": "after_block", "after_block": {"id": nearest_newer_id}}

    def run_page_position(self, parent_id: str, run_id: str) -> Optional[dict[str, Any]]:
        """计算新 Run Page 的位置，按运行时间从新到旧插入。"""
        target = parse_run_id(run_id) if run_id else None
        if target is None:
            return None
        dated: list[tuple[datetime, str]] = []
        for child in self.child_pages(parent_id):
            match = re.match(r"^(\d{2}:\d{2}:\d{2})\s*·", child["title"].strip())
            if not match:
                continue
            try:
                child_time = datetime.strptime(match.group(1), "%H:%M:%S").time()
            except ValueError:
                continue
            dated.append((datetime.combine(target.date(), child_time), child["id"]))
        newer = [(child_time, child_id) for child_time, child_id in dated if child_time > target]
        if not newer:
            return {"type": "page_start"}
        _, nearest_newer_id = min(newer, key=lambda item: item[0])
        return {"type": "after_block", "after_block": {"id": nearest_newer_id}}

    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        for start in range(0, len(blocks), 100):
            self._request("PATCH", f"/blocks/{page_id}/children", json={"children": blocks[start : start + 100]})

    def upload_file(self, path: Path, *, content_type: Optional[str] = None) -> str:
        content_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        # 上传大小必须与 artifact 准备阶段使用同一个正式配置；不要使用
        # 未定义的旧版 _ZIP_LIMIT / _PART_SIZE 常量。
        upload_limit = notion_max_upload_bytes()
        if size <= upload_limit:
            mode = "single_part"
            number_of_parts = None
        else:
            mode = "multi_part"
            number_of_parts = (size + upload_limit - 1) // upload_limit
        body: dict[str, Any] = {"mode": mode, "filename": path.name, "content_type": content_type}
        if number_of_parts:
            body["number_of_parts"] = number_of_parts
        upload = self._request("POST", "/file_uploads", json=body)
        upload_id = upload["id"]
        send_url = upload.get("upload_url") or f"{NOTION_API}/file_uploads/{upload_id}/send"
        complete_url = upload.get("complete_url") or f"{NOTION_API}/file_uploads/{upload_id}/complete"
        with path.open("rb") as handle:
            part = 1
            while True:
                chunk = handle.read(upload_limit if number_of_parts else -1)
                if not chunk:
                    break
                data = {"part_number": str(part)} if number_of_parts else None
                try:
                    response = self.client.post(
                        send_url,
                        # httpx builds multipart/form-data and its boundary when
                        # `files` is supplied; do not override Content-Type here.
                        headers={k: v for k, v in self.headers.items() if k != "Content-Type"},
                        files={"file": (path.name, chunk, content_type)},
                        data=data,
                        timeout=self.client.timeout,
                    )
                except httpx.HTTPError as exc:
                    raise NotionSyncError(f"上传文件网络错误：{exc}") from exc
                if response.status_code >= 400:
                    if response.status_code == 403 and any(
                        marker in response.text.lower()
                        for marker in ("cloudflare", "blocked", "unable to access notion.com")
                    ):
                        raise NotionSyncError(
                            "Notion/Cloudflare 拒绝了文件上传请求（HTTP 403）；"
                            "请检查网络环境，非 Token 配置错误"
                        )
                    raise NotionSyncError(f"Notion 文件上传 HTTP {response.status_code}：{response.text}")
                try:
                    upload = response.json()
                except ValueError:
                    upload = {"status": "uploaded"}
                send_url = upload.get("upload_url") or send_url
                complete_url = upload.get("complete_url") or complete_url
                part += 1
                if not number_of_parts:
                    break
        if number_of_parts:
            upload = self._request("POST", complete_url)
        if upload.get("status") not in (None, "uploaded"):
            raise NotionSyncError(f"Notion 文件上传未完成：{upload.get('status')}")
        return upload_id


def _text_block(text: str, *, bold: bool = False) -> dict[str, Any]:
    annotation = {"bold": True} if bold else {}
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}, "annotations": annotation}]}}


def _file_block(label: str, upload_id: str) -> list[dict[str, Any]]:
    return [
        _text_block(label, bold=True),
        {"object": "block", "type": "file", "file": {"type": "file_upload", "file_upload": {"id": upload_id}}},
    ]


def _write_zip(files: list[Path], package_dir: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, path.relative_to(package_dir).as_posix())


def _ordered_html_files(package: ExportPackage) -> list[Path]:
    files = list(package.html_files) if package.html_files else [
        path for path in package.package_path.rglob("*")
        if path.is_file() and path.suffix.lower() != ".docx"
    ]
    priority = {"index.html": 0, "open-reader.bat": 1, "server.py": 2}
    return sorted(files, key=lambda path: (priority.get(path.name.lower(), 3), path.relative_to(package.package_path).as_posix()))


def _split_file(path: Path, target_dir: Path, max_bytes: int, *, stem: str, key_prefix: str, kind: str) -> list[UploadArtifact]:
    """将单个无法装入 ZIP/上传上限的文件拆成原始二进制分片并生成 manifest。"""
    parts: list[UploadArtifact] = []
    size = path.stat().st_size
    with path.open("rb") as handle:
        part = 1
        while True:
            chunk = handle.read(max_bytes)
            if not chunk:
                break
            part_path = target_dir / f"{stem}.part{part:03d}"
            part_path.write_bytes(chunk)
            if part_path.stat().st_size > max_bytes:
                raise NotionSyncError(f"生成分片超过 Notion 上限：{part_path.name}")
            parts.append(UploadArtifact(
                key=f"{key_prefix}:part:{part:03d}", path=part_path,
                label=part_path.name, kind=kind, content_type="application/octet-stream",
                fingerprint=_file_fingerprint(part_path),
            ))
            part += 1
    manifest = target_dir / f"{stem}.manifest.txt"
    manifest.write_text(
        "Laxinwen Notion Sync recovery manifest\n"
        f"original: {path.name}\noriginal_size: {size}\nparts: {len(parts)}\n"
        "merge_order: lexical part number order; concatenate bytes\n"
        + "\n".join(f"{index + 1:03d}: {item.path.name}" for index, item in enumerate(parts))
        + "\n",
        encoding="utf-8",
    )
    if manifest.stat().st_size > max_bytes:
        raise NotionSyncError(f"生成分片 manifest 超过 Notion 上限：{manifest.name}")
    parts.append(UploadArtifact(
        key=f"{key_prefix}:manifest", path=manifest, label=manifest.name,
        kind=f"{kind}_manifest", content_type="text/plain",
        fingerprint=_file_fingerprint(manifest),
    ))
    return parts


def _build_html_artifacts(package: ExportPackage, target_dir: Path, max_bytes: int) -> list[UploadArtifact]:
    if package.origin == "researchreader":
        return []
    target_dir.mkdir(parents=True, exist_ok=True)
    files = _ordered_html_files(package)
    groups: list[list[Path]] = []
    oversized: list[Path] = []
    current: list[Path] = []
    probe = target_dir / ".probe.zip"
    for path in files:
        _write_zip(current + [path], package.package_path, probe)
        if probe.stat().st_size <= max_bytes:
            current.append(path)
            continue
        if current:
            groups.append(current)
            current = []
        _write_zip([path], package.package_path, probe)
        if probe.stat().st_size <= max_bytes:
            current = [path]
        else:
            oversized.append(path)
    if current:
        groups.append(current)
    probe.unlink(missing_ok=True)

    artifacts: list[UploadArtifact] = []
    multiple = len(groups) != 1 or bool(oversized)
    for index, group in enumerate(groups, 1):
        name = f"{package.package_path.name}-HTML.zip" if not multiple else f"{package.package_path.name}-HTML-part{index:02d}.zip"
        archive = target_dir / name
        _write_zip(group, package.package_path, archive)
        if archive.stat().st_size > max_bytes:
            raise NotionSyncError(f"生成 HTML ZIP 超过 Notion 上限：{archive.name}")
        artifacts.append(UploadArtifact(
            key=f"html:zip:{index:03d}", path=archive,
            label=archive.name, kind="html_zip", content_type="application/zip",
            fingerprint=_file_fingerprint(archive),
        ))
    for index, path in enumerate(oversized, 1):
        stem = f"{package.package_path.name}-HTML-{path.name}"
        artifacts.extend(_split_file(path, target_dir, max_bytes, stem=stem, key_prefix=f"html:file:{index:03d}", kind="html_split"))
    return artifacts


def _build_mobile_html_artifacts(
    package: ExportPackage, target_dir: Path, max_bytes: int
) -> list[UploadArtifact]:
    """Build a standalone, text-first HTML artifact for Laxinwen packages."""
    if package.origin == "researchreader" or not package.index_path.is_file():
        return []
    target_dir.mkdir(parents=True, exist_ok=True)
    mobile_path = target_dir / f"{package.package_path.name}-mobile.html"
    mobile_path.write_bytes(package.index_path.read_bytes())
    if mobile_path.stat().st_size <= max_bytes:
        return [UploadArtifact(
            key="html:mobile:original",
            path=mobile_path,
            label=mobile_path.name,
            kind="html_mobile",
            content_type="text/html",
            fingerprint=_file_fingerprint(mobile_path),
        )]
    return _split_html_file(
        mobile_path, target_dir, max_bytes, variant="original",
        artifact_kind="html_mobile_split",
        key_prefix="html:mobile",
    )


def _build_word_artifacts(package: ExportPackage, target_dir: Path, max_bytes: int) -> list[UploadArtifact]:
    target_dir.mkdir(parents=True, exist_ok=True)
    if package.docx_path is None:
        return []
    if package.docx_path.stat().st_size <= max_bytes:
        return [UploadArtifact(
            key="word:docx", path=package.docx_path, label=package.docx_path.name,
            kind="word", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            fingerprint=_file_fingerprint(package.docx_path),
        )]
    return _split_file(
        package.docx_path, target_dir, max_bytes,
        stem=package.docx_path.name, key_prefix="word:split", kind="word_split",
    )


def _html_variant(path: Path) -> Optional[str]:
    if _TRANSLATED_HTML_RE.match(path.name):
        return "zh-CN-translation"
    if _DUAL_HTML_RE.match(path.name):
        return "zh-CN-dual"
    return None


def _split_html_file(
    path: Path, target_dir: Path, max_bytes: int, *, variant: str,
    artifact_kind: str = "html_split", key_prefix: str | None = None,
) -> list[UploadArtifact]:
    """按 article/section 边界生成可独立打开的完整 HTML parts。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    tree = HTMLParser(text)
    nodes = tree.css("article") or tree.css("section")
    if not nodes:
        raise NotionSyncError(
            f"无法按 article/section 边界切分 HTML：{path.name}"
        )
    head = tree.head.html if tree.head else "<head><meta charset=\"utf-8\"></head>"
    prefix = "<!doctype html><html>"
    suffix = "</body></html>"

    def render(group: list[str]) -> bytes:
        return (prefix + head + "<body>" + "".join(group) + suffix).encode("utf-8")

    groups: list[list[str]] = []
    current: list[str] = []
    for node in nodes:
        html = node.html
        candidate = render(current + [html])
        if len(candidate) <= max_bytes:
            current.append(html)
            continue
        if not current:
            raise NotionSyncError(
                f"单个 HTML article/section 已超过 Notion 上限：{path.name}"
            )
        groups.append(current)
        current = [html]
    if current:
        groups.append(current)

    target_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[UploadArtifact] = []
    for index, group in enumerate(groups, 1):
        part_path = target_dir / f"{path.name}.part{index:03d}.html"
        part_path.write_bytes(render(group))
        if part_path.stat().st_size > max_bytes:
            raise NotionSyncError(f"生成 HTML part 超过 Notion 上限：{part_path.name}")
        artifacts.append(UploadArtifact(
            key=(key_prefix or f"html:{variant}:{path.name}") + f":part:{index:03d}",
            path=part_path,
            label=part_path.name,
            kind=artifact_kind,
            content_type="text/html",
            fingerprint=_file_fingerprint(part_path),
            artifact_variant=variant,
        ))
    return artifacts


def _build_extra_artifacts(package: ExportPackage, target_dir: Path, max_bytes: int) -> list[UploadArtifact]:
    artifacts: list[UploadArtifact] = []
    if (
        package.origin == "researchreader"
        and package.index_path.is_file()
        and package.index_path.suffix.lower() == ".html"
    ):
        path = package.index_path
        if path.stat().st_size <= max_bytes:
            artifacts.append(UploadArtifact(
                key="html:original:single", path=path, label=path.name,
                kind="html", content_type="text/html",
                fingerprint=_file_fingerprint(path), artifact_variant="original",
            ))
        else:
            artifacts.extend(_split_html_file(path, target_dir, max_bytes, variant="original"))
    if package.origin == "researchreader":
        candidates = list(package.extra_files)
    else:
        # Other sources may also emit translation/dual HTML in the same
        # reading-package directory.  Only recognized variants are promoted
        # to direct HTML artifacts; server/resources remain ZIP-only.
        candidates = [
            path for path in package.html_files
            if path.suffix.lower() == ".html" and _html_variant(path)
        ]
    for index, path in enumerate(candidates, 1):
        variant = _html_variant(path) if path.suffix.lower() == ".html" else None
        if variant:
            if path.stat().st_size <= max_bytes:
                artifacts.append(UploadArtifact(
                    key=f"html:{variant}:{path.name}", path=path, label=path.name,
                    kind="html", content_type="text/html",
                    fingerprint=_file_fingerprint(path), artifact_variant=variant,
                ))
            else:
                artifacts.extend(_split_html_file(path, target_dir, max_bytes, variant=variant))
            continue
        kind = path.suffix.lower().lstrip(".") or "file"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.stat().st_size <= max_bytes:
            artifacts.append(UploadArtifact(
                key=f"{kind}:original:part:001", path=path, label=path.name,
                kind=kind, content_type=content_type, fingerprint=_file_fingerprint(path),
            ))
        else:
            artifacts.extend(_split_file(
                path, target_dir, max_bytes, stem=path.name,
                key_prefix=f"{kind}:original:{index:03d}", kind=f"{kind}_split",
            ))
    return artifacts


def _artifact_blocks(artifacts: list[UploadArtifact], ids: dict[str, str], *, title: str, split_title: str) -> list[dict[str, Any]]:
    if not artifacts:
        return []
    if len(artifacts) == 1 and artifacts[0].kind in {"html_zip", "html", "html_mobile", "word"}:
        return _file_block(title, ids[artifacts[0].key])
    blocks = [_text_block(split_title, bold=True)]
    parts = [item for item in artifacts if not item.kind.endswith("_manifest")]
    total = len(parts)
    for index, item in enumerate(parts, 1):
        blocks.extend(_file_block(f"Part {index} / {total} · {item.label}", ids[item.key]))
    for item in artifacts:
        if item.kind.endswith("_manifest"):
            blocks.extend(_file_block("恢复说明", ids[item.key]))
    return blocks


def _artifact_identity(package: ExportPackage, artifact: UploadArtifact) -> str:
    artifact_type = artifact.kind.split("_", 1)[0]
    if artifact.kind.startswith("html_mobile"):
        artifact_type = "html_mobile"
    if artifact.kind.endswith("_manifest"):
        artifact_type = artifact.kind.removesuffix("_manifest")
    if artifact.artifact_variant == "original" and package.origin != "researchreader":
        # Preserve the identity shape used by existing notion-sync.json files.
        part = artifact.key.rsplit(":", 1)[-1]
    else:
        part = ":".join(artifact.key.split(":")[1:])
    run = package.run_id or f"legacy:{package.package_key}"
    return "|".join([
        package.origin, package.source, package.date, run,
        artifact_type, artifact.artifact_variant, part,
    ])


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


def _package_state(
    state: dict[str, Any], package: ExportPackage
) -> tuple[str, dict[str, Any]]:
    """按当前 key 查找，并兼容旧 ResearchReader package key。"""
    packages = state.get("packages", {})
    if package.package_key in packages:
        return package.package_key, packages[package.package_key]
    if package.origin != "researchreader":
        return package.package_key, {}
    current_path = _normalized_path(package.package_path)
    for key, record in packages.items():
        if (
            record.get("origin") == "researchreader"
            and str(record.get("source", "")).lower() == package.source.lower()
            and record.get("date") == package.date
            and record.get("package_path")
            and _normalized_path(record["package_path"]) == current_path
        ):
            return key, record
    return package.package_key, {}


def _legacy_artifact_state(
    record: dict[str, Any], package: ExportPackage, artifact: UploadArtifact
) -> Optional[dict[str, Any]]:
    """按 fingerprint/name 找旧 identity，避免 package key 迁移导致重传。"""
    artifacts = record.get("artifacts", {})
    identity = _artifact_identity(package, artifact)
    candidate = artifacts.get(identity) or artifacts.get(artifact.key)
    if candidate:
        return candidate
    for item in artifacts.values():
        if (
            item.get("kind") == artifact.kind
            and item.get("name") == artifact.label
            and item.get("fingerprint") == artifact.fingerprint
            and item.get("artifact_variant", "original") == artifact.artifact_variant
            and item.get("upload_id")
        ):
            return item
    if not artifacts and record.get("synced") and artifact.artifact_variant == "original":
        legacy_upload_id = None
        if artifact.kind == "html_zip":
            legacy_upload_id = record.get("html_upload_id") or f"legacy-synced:{artifact.key}"
        elif artifact.kind == "word":
            legacy_upload_id = record.get("word_upload_id")
        elif package.origin != "researchreader":
            legacy_upload_id = f"legacy-synced:{artifact.key}"
        if legacy_upload_id:
            return {
                "upload_id": legacy_upload_id,
                "fingerprint": artifact.fingerprint,
                "block_appended": True,
            }
    return None


class NotionSync:
    """把扫描到的阅读包归档到 Root → Source → Date 页面结构。"""

    def __init__(self, client: Optional[NotionClient], root_page_id: str, *, state_path: str | Path = DEFAULT_STATE_PATH, max_upload_bytes: Optional[int] = None):
        self.client = client
        self.root_page_id = root_page_id
        self.state_path = Path(state_path)
        self.max_upload_bytes = max_upload_bytes or notion_max_upload_bytes()
        self.state = _read_json(self.state_path)

    def _save(self) -> None:
        _write_json(self.state_path, self.state)

    def sync(self, packages: Iterable[ExportPackage], *, dry_run: bool = False) -> list[str]:
        # Reload under the lock so a second process cannot sync from a stale
        # state snapshot after the first process has completed.
        with _state_lock(self.state_path):
            self.state = _read_json(self.state_path)
            return self._sync_unlocked(packages, dry_run=dry_run)

    def _sync_unlocked(self, packages: Iterable[ExportPackage], *, dry_run: bool = False) -> list[str]:
        if not dry_run:
            self.client.retrieve_page(self.root_page_id)
        messages: list[str] = []
        for package in packages:
            try:
                _, saved = _package_state(self.state, package)
                record = dict(saved)
                with tempfile.TemporaryDirectory(prefix="laxinwen-notion-") as temp:
                    target_dir = Path(temp)
                    html_artifacts = _build_html_artifacts(package, target_dir, self.max_upload_bytes)
                    mobile_html_artifacts = _build_mobile_html_artifacts(
                        package, target_dir, self.max_upload_bytes
                    )
                    word_artifacts = _build_word_artifacts(package, target_dir, self.max_upload_bytes)
                    extra_artifacts = _build_extra_artifacts(package, target_dir, self.max_upload_bytes)
                    artifacts = (
                        mobile_html_artifacts + html_artifacts
                        + word_artifacts + extra_artifacts
                    )
                    artifact_state = dict(record.get("artifacts", {}))
                    resolved: dict[str, dict[str, Any]] = {}
                    pending: list[UploadArtifact] = []
                    for artifact in artifacts:
                        identity = _artifact_identity(package, artifact)
                        prior = _legacy_artifact_state(record, package, artifact)
                        if prior and prior.get("fingerprint") == artifact.fingerprint and prior.get("upload_id"):
                            current = dict(prior)
                            current.update({
                                "identity": identity,
                                "kind": artifact.kind,
                                "name": artifact.label,
                                "fingerprint": artifact.fingerprint,
                                "artifact_variant": artifact.artifact_variant,
                            })
                            if "block_appended" not in current:
                                current["block_appended"] = bool(record.get("blocks_appended"))
                            artifact_state[identity] = current
                            resolved[identity] = current
                        else:
                            pending.append(artifact)

                    if dry_run:
                        if not pending:
                            messages.append(f"SYNC SKIP · {package.source.upper()} · {package.date} · already synced")
                        else:
                            labels = ", ".join(
                                f"{item.artifact_variant}:{item.label}" for item in pending
                            )
                            messages.append(
                                f"SYNC PLAN · {package.source.upper()} · {package.date} · "
                                f"{package.package_path.name} · upload {len(pending)} file(s) · {labels}"
                            )
                        continue

                    block_pending = [
                        artifact for artifact in artifacts
                        if not resolved.get(_artifact_identity(package, artifact), {}).get("block_appended")
                    ]
                    if not pending and not block_pending:
                        messages.append(f"SYNC SKIP · {package.source.upper()} · {package.date} · already synced")
                        continue
                    source_key = package.source.lower()
                    date_key = f"{source_key}:{package.date}"
                    source_id = self.state["source_pages"].get(source_key)
                    if not source_id:
                        source_id = self.client.find_or_create_child_page(self.root_page_id, package.source.upper())
                        self.state["source_pages"][source_key] = source_id
                        self._save()
                    date_id = self.state["date_pages"].get(date_key)
                    if not date_id:
                        date_position = (
                            self.client.date_page_position(source_id, package.date)
                            if hasattr(self.client, "date_page_position") else None
                        )
                        try:
                            date_id = self.client.find_or_create_child_page(
                                source_id, package.date, position=date_position
                            )
                        except TypeError as exc:
                            if "position" not in str(exc):
                                raise
                            date_id = self.client.find_or_create_child_page(source_id, package.date)
                        self.state["date_pages"][date_key] = date_id
                        self._save()
                    run_key = "|".join([
                        package.origin, package.source, package.date,
                        package.run_id or f"legacy:{package.package_key}",
                    ])
                    is_researchreader = package.origin == "researchreader"
                    if is_researchreader:
                        # ResearchReader is file/version-oriented: its
                        # artifacts are siblings directly under Date, with no
                        # synthetic ResearchReader or Legacy Run page.
                        run_id = date_id
                    else:
                        run_id = self.state["run_pages"].get(run_key)
                        if not run_id:
                            parsed = parse_run_id(package.run_id) if package.run_id else None
                            run_label = (
                                f"{parsed.strftime('%H:%M:%S')} · {package.job_id or '手动运行'}"
                                if parsed else f"Legacy · {package.job_id or '历史运行'}"
                            )
                            run_position = (
                                self.client.run_page_position(date_id, package.run_id)
                                if hasattr(self.client, "run_page_position") else None
                            )
                            try:
                                run_id = self.client.find_or_create_child_page(
                                    date_id, run_label, position=run_position
                                )
                            except TypeError as exc:
                                if "position" not in str(exc):
                                    raise
                                run_id = self.client.find_or_create_child_page(date_id, run_label)
                            self.state["run_pages"][run_key] = run_id
                            self._save()
                    record.update({
                        "origin": package.origin, "source": source_key, "date": package.date,
                        "run_id": package.run_id, "job_id": package.job_id,
                        "artifact_variant": package.artifact_variant,
                        "package_path": str(package.package_path),
                        "date_page_id": date_id, "notion_page_id": run_id,
                    })
                    record["synced"] = False
                    record["artifacts"] = artifact_state
                    upload_ids: dict[str, str] = {}
                    for identity, item in resolved.items():
                        if item.get("upload_id"):
                            upload_ids[
                                next(item_.key for item_ in artifacts if _artifact_identity(package, item_) == identity)
                            ] = item["upload_id"]
                    for artifact in pending:
                        identity = _artifact_identity(package, artifact)
                        if artifact.path.stat().st_size > self.max_upload_bytes:
                            raise NotionSyncError(f"文件超过 Notion 安全上限：{artifact.path.name}")
                        assert self.client is not None
                        upload_id = self.client.upload_file(artifact.path, content_type=artifact.content_type)
                        artifact_state[identity] = {
                            "identity": identity,
                            "kind": artifact.kind,
                            "name": artifact.label,
                            "artifact_variant": artifact.artifact_variant,
                            "fingerprint": artifact.fingerprint,
                            "size": artifact.path.stat().st_size,
                            "upload_id": upload_id,
                            "block_appended": False,
                            "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        }
                        upload_ids[artifact.key] = upload_id
                        resolved[identity] = artifact_state[identity]
                        self.state["packages"][package.package_key] = record
                        self._save()
                    parsed = parse_run_id(package.run_id) if package.run_id else None
                    label = (
                        f"{parsed.strftime('%H:%M:%S')} · {package.job_id or '手动运行'}"
                        if parsed else f"Legacy · {package.job_id or '历史运行'}"
                    )
                    run_artifacts = list(artifacts)
                    run_block_pending = [item for item in block_pending if item in run_artifacts]
                    blocks = []
                    if not record.get("blocks_appended") and not is_researchreader:
                        blocks.append(_text_block(label, bold=True))
                        if package.article_count is not None:
                            blocks.append(_text_block(f"新闻数量：{package.article_count}"))
                    selected = [item for item in run_block_pending if item in mobile_html_artifacts]
                    blocks.extend(_artifact_blocks(
                        selected, upload_ids,
                        title="手机 HTML 阅读 · 原文",
                        split_title="手机 HTML 阅读 · 原文（文件较大，已自动分片）",
                    ))
                    selected = [item for item in run_block_pending if item in html_artifacts]
                    blocks.extend(_artifact_blocks(
                        selected, upload_ids,
                        title="HTML 阅读包 · 原文（完整）",
                        split_title="完整 HTML 阅读包（文件较大，已自动分包）",
                    ))
                    blocks.extend(_artifact_blocks(
                        [item for item in run_block_pending if item in word_artifacts], upload_ids,
                        title="Word 阅读包",
                        split_title="Word 阅读包（文件较大，已自动分片）",
                    ))
                    for kind in ("epub", "pdf"):
                        selected = [item for item in run_block_pending if item.kind == kind]
                        selected.extend(item for item in run_block_pending if item.kind == f"{kind}_split")
                        selected.extend(item for item in run_block_pending if item.kind == f"{kind}_split_manifest")
                        blocks.extend(_artifact_blocks(
                            selected, upload_ids,
                            title=f"原始 {kind.upper()}",
                            split_title=f"原始 {kind.upper()}（文件较大，已自动分片）",
                        ))
                    for variant, page_title in (
                        ("original", "HTML 阅读包 · 原文"),
                        ("zh-CN-translation", "HTML 阅读包 · 中文"),
                        ("zh-CN-dual", "HTML 阅读包 · 中英双语"),
                    ):
                        selected = [
                            item for item in run_block_pending
                            if item.artifact_variant == variant
                            and item.kind in {"html", "html_split"}
                        ]
                        # Laxinwen's original HTML is represented by its
                        # mobile artifact; ResearchReader uses this branch for
                        # daily.html and translated/dual variants.
                        if not is_researchreader and variant == "original":
                            selected = []
                        blocks.extend(_artifact_blocks(
                            selected, upload_ids,
                            title=page_title,
                            split_title=f"{page_title}（文件较大，已自动分片）",
                        ))
                    if blocks:
                        assert self.client is not None
                        self.client.append_blocks(run_id, blocks)
                        for artifact in run_block_pending:
                            artifact_state[_artifact_identity(package, artifact)]["block_appended"] = True
                    record["blocks_appended"] = all(
                        artifact_state.get(_artifact_identity(package, artifact), {}).get("block_appended")
                        for artifact in artifacts
                    )
                    record["synced"] = record["blocks_appended"] and all(
                        artifact_state.get(_artifact_identity(package, artifact), {}).get("fingerprint") == artifact.fingerprint
                        and artifact_state.get(_artifact_identity(package, artifact), {}).get("upload_id")
                        for artifact in artifacts
                    )
                    record["synced_at"] = datetime.now(timezone.utc).isoformat()
                    self.state["packages"][package.package_key] = record
                    self._save()
                    messages.append(f"SYNC SUCCESS · {package.source.upper()} · {package.date}")
            except Exception as exc:
                messages.append(f"SYNC FAILED · {package.source.upper()} · {package.date} · {exc}")
                logger.error("Notion 同步失败 %s: %s", package.package_path, exc)
        return messages


def run_sync(
    *,
    token: Optional[str] = None,
    root_page_id: Optional[str] = None,
    export_root: str | Path = DEFAULT_EXPORT_ROOT,
    state_path: str | Path = DEFAULT_STATE_PATH,
    timeout: float = 60.0,
    dry_run: bool = False,
    researchreader_output: str | Path | None = None,
    researchreader_books: str | Path | None = None,
) -> list[str]:
    config = load_notion_config(
        token=token, root_page_id=root_page_id, export_root=export_root,
        state_path=state_path, researchreader_output=researchreader_output,
        researchreader_books=researchreader_books,
    )
    packages = ExportPackageScanner(config["export_root"]).scan()
    if config["researchreader_output"] and config["researchreader_books"]:
        packages.extend(ResearchReaderScanner(
            config["researchreader_output"], config["researchreader_books"]
        ).scan())
    if dry_run:
        return NotionSync(
            None, config["root_page_id"] or "dry-run",
            state_path=config["state_path"], max_upload_bytes=config["max_upload_bytes"],
        ).sync(packages, dry_run=True)
    if not config["root_page_id"]:
        raise NotionSyncError("缺少 NOTION_ROOT_PAGE_ID")
    client = NotionClient(config["token"], timeout=timeout)
    try:
        return NotionSync(
            client, config["root_page_id"], state_path=config["state_path"],
            max_upload_bytes=config["max_upload_bytes"],
        ).sync(packages)
    finally:
        client.close()
