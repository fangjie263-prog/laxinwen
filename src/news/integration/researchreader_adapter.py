"""调用现有 ResearchReader EPUB -> HTML 流程。

本模块只负责目录扫描、进程边界和输出状态，不复制 ResearchReader 的解析器。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class LocalNewsFile:
    path: Path
    kind: str
    status: str = "待处理"
    output_path: Path | None = None
    error: str | None = None


class ResearchReaderAdapter:
    """通过 subprocess 调用外部 ResearchReader，保持两个项目独立。"""

    def __init__(
        self,
        books_root: str | Path | None = None,
        output_root: str | Path | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[4]
        default_books = project_root / "ResearchReader" / "researchreader" / "books"
        default_output = project_root / "ResearchReader" / "researchreader" / "output"
        self.books_root = Path(
            books_root or os.environ.get("RESEARCHREADER_BOOKS_ROOT") or default_books
        ).expanduser()
        self.output_root = Path(
            output_root or os.environ.get("RESEARCHREADER_OUTPUT_ROOT") or default_output
        ).expanduser()
        self._runner = runner or subprocess.run

        # 让现有 notion_sync.ResearchReaderScanner 能发现本 adapter 的输出。
        os.environ.setdefault("RESEARCHREADER_BOOKS_ROOT", str(self.books_root))
        os.environ.setdefault("RESEARCHREADER_OUTPUT_ROOT", str(self.output_root))

    def scan_files(self) -> list[LocalNewsFile]:
        if not self.books_root.is_dir():
            return []
        files: list[LocalNewsFile] = []
        for path in sorted(self.books_root.iterdir(), key=lambda item: item.name.casefold()):
            if path.is_file() and path.suffix.lower() in {".epub", ".pdf"}:
                kind = "EPUB" if path.suffix.lower() == ".epub" else "PDF"
                files.append(self.get_status(path) if kind == "EPUB" else LocalNewsFile(path=path, kind=kind))
        return files

    def extract_epub_to_html(self, source_path: str | Path) -> LocalNewsFile:
        source = Path(source_path).resolve()
        if source.suffix.lower() != ".epub":
            raise ValueError("当前阶段只支持 EPUB；PDF 入口将在后续阶段启用。")
        if not source.is_file():
            raise FileNotFoundError(f"EPUB 文件不存在：{source}")

        output_dir = self._output_directory(source)
        output_dir.mkdir(parents=True, exist_ok=True)
        python = self._researchreader_python()
        script = (
            "import sys; "
            "from pathlib import Path; "
            "import wsj_reader; "
            "source=Path(sys.argv[1]); output=Path(sys.argv[2]); "
            "wsj_reader.OUTPUT_DIR=output; wsj_reader.IMAGES_DIR=output/'images'; "
            "title, images, articles=wsj_reader.read_epub(str(source)); "
            "wsj_reader.save_output(title, images, articles); "
            "print('RESEARCHREADER_HTML=' + str(output/'daily.html'))"
        )
        result = self._runner(
            [str(python), "-c", script, str(source), str(output_dir)],
            cwd=str(self._reader_root()),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "未知错误").strip()
            raise RuntimeError(f"ResearchReader EPUB → HTML 失败：{detail}")
        html_path = output_dir / "daily.html"
        if not html_path.is_file():
            raise RuntimeError("ResearchReader 未生成 daily.html")
        return LocalNewsFile(path=source, kind="EPUB", status="已完成", output_path=html_path)

    def get_output_path(self, source_path: str | Path) -> Path:
        return self._output_directory(Path(source_path).resolve()) / "daily.html"

    def get_status(self, source_path: str | Path) -> LocalNewsFile:
        source = Path(source_path).resolve()
        output = self.get_output_path(source)
        if output.is_file():
            return LocalNewsFile(source, "EPUB" if source.suffix.lower() == ".epub" else "PDF", "已完成", output)
        return LocalNewsFile(source, source.suffix.upper().lstrip("."), "待处理")

    def _reader_root(self) -> Path:
        return Path(__file__).resolve().parents[4] / "ResearchReader" / "researchreader"

    def _researchreader_python(self) -> Path:
        candidates = (
            self._reader_root() / ".venv" / "Scripts" / "python.exe",
            self._reader_root().parent / ".venv" / "Scripts" / "python.exe",
            self._reader_root().parent / "epub-translator" / ".venv" / "Scripts" / "python.exe",
            Path(sys.executable),
        )
        return next((path for path in candidates if path.is_file()), Path(sys.executable))

    def _output_directory(self, source: Path) -> Path:
        # 生成现有 ResearchReaderScanner 识别的 source_DD-MM-YYYY_Kobo 目录。
        match = re.search(r"(?P<source>[a-z][a-z0-9-]*).*?(?P<year>20\d{2})[-_](?P<month>\d{2})[-_](?P<day>\d{2})", source.stem, re.I)
        if match:
            name = f"{match.group('source')}_{match.group('day')}-{match.group('month')}-{match.group('year')}_Kobo"
        else:
            name = f"{source.stem}_Kobo"
        return self.output_root / name
