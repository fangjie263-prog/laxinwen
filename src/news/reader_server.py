"""轻量级本地 HTTP 静态服务器（News Reader 的"本地 HTTP 阅读模式"）。

解决的问题：浏览器扩展（尤其是 Immersive Translate）无法可靠处理 ``file://`` 本地 HTML。
通过把 ``data/export/`` 作为静态目录以 ``http://127.0.0.1:<port>/`` 提供，
让浏览器扩展像处理普通网页一样处理本地 News Archive / AI Research 页面。

设计约束（对应验收清单）：

- 只使用 Python 标准库 ``http.server``，不增加第三方依赖；
- **只监听 127.0.0.1**，绝不监听 0.0.0.0，避免把本地新闻数据库暴露到局域网；
- 端口被占用时自动选择下一个可用端口（偏好端口列表 → 随机空闲端口）；
- 生命周期跟随 News Reader GUI：``start()`` 启动守护线程，``stop()`` 优雅关闭；
- 不实现翻译引擎、不注入任何翻译相关代码 —— 只是普通静态 HTTP。
"""

from __future__ import annotations

import functools
import logging
import socketserver
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 默认偏好端口（从 8000 起依次尝试；全部被占用则用系统分配的随机端口）
DEFAULT_PREFERRED_PORTS = (8000, 8001, 8002, 8003, 8004)


class _QuietHandler(SimpleHTTPRequestHandler):
    """静默版静态文件 handler：不把每次请求打印到 stderr。"""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 保留到 logger（DEBUG 级），避免刷屏
        logger.debug(fmt, *args)


class _ReaderHTTPServer(ThreadingHTTPServer):
    """只服务 127.0.0.1 的 HTTP server：避免 server_bind 里的 getfqdn。

    ``http.server`` 默认的 ``server_bind`` 会调用 ``socket.getfqdn()``，
    它需要导入 ``encodings.idna``；在某些受限环境/非主线程中这会触发
    解释器崩溃。因为我们只监听 127.0.0.1，直接设置 server_name 即可。
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class ReaderServer:
    """``data/export/`` 的本地静态 HTTP 服务器，仅监听 127.0.0.1。

    用法::

        server = ReaderServer(export_root)   # export_root = data/export
        server.start()                       # 选择端口并启动守护线程
        url = server.url_for("news-html/eco/index.html")
        # -> http://127.0.0.1:<port>/news-html/eco/index.html
        server.stop()
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        preferred_ports: Optional[Sequence[int]] = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError(
                "本地新闻阅读服务器只允许监听 127.0.0.1，禁止监听 0.0.0.0 等地址。"
            )
        self.root_dir = Path(root_dir).resolve()
        self.host = host
        self._port = int(port)
        self._preferred_ports = tuple(preferred_ports or DEFAULT_PREFERRED_PORTS)
        self._httpd: Optional[_ReaderHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> Optional[int]:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._port or None

    def start(self) -> "ReaderServer":
        """启动服务器（幂等）。端口被占用时自动尝试偏好端口/随机端口。"""
        if self._httpd is not None:
            return self
        if not self.root_dir.is_dir():
            raise FileNotFoundError(f"静态目录不存在：{self.root_dir}")

        handler = functools.partial(_QuietHandler, directory=str(self.root_dir))

        # 端口候选：显式端口 > 偏好端口列表 > 0（系统分配随机空闲端口）
        candidates: list[int] = []
        if self._port:
            candidates.append(self._port)
        else:
            candidates.extend(self._preferred_ports)
            candidates.append(0)

        httpd: Optional[_ReaderHTTPServer] = None
        last_err: Optional[OSError] = None
        for p in candidates:
            try:
                httpd = _ReaderHTTPServer((self.host, p), handler)
                break
            except OSError as exc:
                last_err = exc
                continue
        if httpd is None:
            raise OSError(
                f"无法绑定 127.0.0.1 的任何候选端口（{last_err}）"
            )

        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="laxinwen-reader-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("News Reader HTTP 已启动：http://%s:%d/", self.host, self.port)
        return self

    def stop(self) -> None:
        """停止服务器（幂等）。"""
        if self._httpd is None:
            return
        httpd, self._httpd = self._httpd, None
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
        self._thread = None

    def url_for(self, rel_path: str) -> str:
        """把相对路径转换为 ``http://127.0.0.1:<port>/<rel>``。"""
        if self._httpd is None:
            raise RuntimeError("服务器尚未启动，无法生成 URL。")
        rel = str(rel_path).lstrip("/").replace("\\", "/")
        return f"http://{self.host}:{self.port}/{quote(rel, safe='/')}"
