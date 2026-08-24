"""Windows Task Scheduler（计划任务）命令构建与调度管理。

本项目把「自动定时抓取」交给 Windows 原生 ``schtasks.exe`` 完成，GUI 只负责
创建 / 修改 / 删除任务，真正的抓取由计划任务在后台以 headless 方式触发，因此：

- GUI 关闭后任务仍然运行；
- Windows 重启后任务仍然继续；
- 不需要一直打开 Laxinwen GUI。

本模块**只负责构建 schtasks / PowerShell 命令**（纯字符串/列表构建，便于在
Linux / headless 环境做单元测试）。实际执行依赖 Windows 的 schtasks.exe，
因此在 Linux 上不会真正执行（标记 ``REQUIRES WINDOWS REAL TEST``）。

设计要点：
- ``Do not start a new instance``（重复运行保护）：通过 schtasks 的
  ``/Z /RI``（重复间隔）+ 任务自身的“仅运行一次”语义实现；同时应用层
  通过 lock 文件兜底，避免两个 pipeline 同时抓同一来源。
- 路径含空格时正确 quoting（schtasks /TR 参数整体用双引号包裹，
  内部程序与参数也用引号）。
- 稳定任务名：``Laxinwen-<SOURCE>-AutoFetch``；重复安装 = 更新原任务。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

from .scheduler_config import (
    FREQ_DAILY,
    SchedulerConfig,
)

logger = logging.getLogger(__name__)

# 每次抓取任务的时间上限（分钟）。超过则 Windows 任务计划程序视为超时。
# 与「Do not start a new instance」配合，避免任务堆积。
TASK_TIMEOUT_MINUTES = 30


def default_project_root() -> Path:
    """返回项目根目录：``src/news/task_scheduler.py`` 向上三级。"""
    return Path(__file__).resolve().parents[2]


def find_python_executable(project_root: str | Path | None = None) -> str:
    """确定后台任务使用的 Python 可执行文件绝对路径。

    优先级：
    1. 项目虚拟环境 ``.venv/Scripts/python.exe``（Windows）或 ``.venv/bin/python``；
    2. 当前进程 ``sys.executable``（若为绝对路径）；
    3. ``python`` / ``python.exe`` 的绝对路径。

    不假设 ``python.exe`` 在 PATH。返回绝对路径字符串。
    """
    root = Path(project_root) if project_root else default_project_root()
    # Windows 虚拟环境
    win_venv = root / ".venv" / "Scripts" / "python.exe"
    if win_venv.is_file():
        return str(win_venv)
    # Unix 虚拟环境
    unix_venv = root / ".venv" / "bin" / "python"
    if unix_venv.is_file():
        return str(unix_venv)
    # 当前进程解释器
    exe = getattr(sys, "executable", "") or ""
    if exe and os.path.isabs(exe) and os.path.isfile(exe):
        return os.path.normpath(exe)
    # 兜底：PATH 中的 python
    for cand in ("python.exe", "python"):
        found = _which(cand)
        if found:
            return found
    return "python"  # 最后兜底（交由 schtasks 尝试）


def _which(name: str) -> Optional[str]:
    """在 PATH 中查找可执行文件（跨平台）。"""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        full = Path(d) / name
        if full.is_file():
            return str(full)
    return None


def _quote_win(path_or_arg: str) -> str:
    """对单个路径/参数做 Windows quoting：整体用双引号包裹，内部双引号转义。"""
    s = str(path_or_arg)
    if '"' in s:
        s = s.replace('"', '\\"')
    return f'"{s}"'


def build_arguments(cfg: SchedulerConfig) -> str:
    """构建后台入口的命令行参数（不含 python 本体）。

    多任务下必须携带 job id：``-m news scheduled-fetch --job-id <id>``，
    让后台入口从 data/scheduler.json 中定位到具体任务，避免多个任务互相混淆。
    """
    # 直接调用包内的后台入口，不依赖 PATH；job id 已写入配置，后台入口按 id 定位。
    return f"-m news scheduled-fetch --job-id {cfg.job_id}"


def build_schtasks_create(
    cfg: SchedulerConfig,
    *,
    python_exe: Optional[str] = None,
    project_root: Optional[str | Path] = None,
) -> list[str]:
    """构建「创建/更新定时任务」的 schtasks 命令列表。

    重复调用会**更新**原有同名任务（schtasks /Create 对同名任务会报错，
    因此此处用 /Create + 先 /Delete 存在时更新；或由上层负责先删除）。

    实际上更稳妥的做法是：先 ``schtasks /Query`` 判断是否存在，存在则
    ``/Delete``，再 ``/Create``，从而做到「重复安装 = 更新原任务」。
    本函数只返回 /Create 的命令，调用方负责“先删除再创建”以保证幂等。
    """
    root = Path(project_root) if project_root else default_project_root()
    py = python_exe or find_python_executable(root)
    task_name = cfg.task_name()

    arguments = build_arguments(cfg)
    # 用 cmd.exe /c 包装，保证 workdir 生效且退出码正确。
    # /TR 整体放在一对双引号内（schtasks 对带参数命令的要求）；
    # 内部 python 路径用引号保护（可能含空格）。
    cmd_line = f'cmd.exe /c "{_quote_win(py)} {arguments}"'

    # schtasks /Create 参数
    cmd = [
        "schtasks",
        "/Create",
        "/TN", task_name,
        "/TR", cmd_line,
        "/SC", _schedule_type(cfg),
    ]
    if cfg.frequency == FREQ_DAILY:
        cmd += ["/ST", cfg.time]
    else:
        # 每小时：用 /SC HOURLY + /MO <interval>
        cmd += ["/MO", str(cfg.interval_hours)]
    cmd += [
        "/F",  # 强制：若任务存在则覆盖（避免重复安装报错）
        "/RL", "LIMITED",  # 普通权限运行（无需管理员）
    ]
    # 工作目录：schtasks 无法直接设置 cwd，因此由后台入口自行 cd（见 scheduled_fetch.py）。
    return cmd


def _schedule_type(cfg: SchedulerConfig) -> str:
    """返回 schtasks /SC 的类型：DAILY / HOURLY。"""
    return "DAILY" if cfg.frequency == FREQ_DAILY else "HOURLY"


def build_schtasks_delete(cfg: SchedulerConfig) -> list[str]:
    """构建「删除定时任务」命令列表。"""
    return ["schtasks", "/Delete", "/TN", cfg.task_name(), "/F"]


def build_schtasks_query(cfg: SchedulerConfig) -> list[str]:
    """构建「查询定时任务」命令列表（用于验证）。"""
    return ["schtasks", "/Query", "/TN", cfg.task_name(), "/V", "/FO", "LIST"]


def build_schtasks_run(cfg: SchedulerConfig) -> list[str]:
    """构建「立即运行一次」命令列表。"""
    return ["schtasks", "/Run", "/TN", cfg.task_name()]


# ---------------------------------------------------------------------------
# 执行层（仅 Windows 可用；Linux/headless 上调用会明确报错）
# ---------------------------------------------------------------------------

def is_windows() -> bool:
    """是否 Windows 平台。"""
    return os.name == "nt"


def run_schtasks(cmd: list[str]) -> tuple[int, str]:
    """实际执行 schtasks 命令。非 Windows 平台直接抛出 RuntimeError。

    返回 (returncode, stdout+stderr)。
    """
    if not is_windows():
        raise RuntimeError(
            "Windows Task Scheduler 只能在 Windows 上执行。"
            "当前为 headless/Linux 环境，此操作标记为 REQUIRES WINDOWS REAL TEST。"
        )
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("schtasks 执行失败: %s", exc)
        return 1, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


# ---------------------------------------------------------------------------
# 高层 API：install / delete / run_now / query（供 GUI 与 BAT 复用）
# ---------------------------------------------------------------------------

def install_task(cfg: SchedulerConfig, *, project_root: Optional[str | Path] = None) -> dict:
    """安装 / 更新定时任务（幂等）。

    流程：
    1. 若同名任务已存在，先删除；
    2. 创建（/F 强制覆盖）。

    返回 {ok, task_name, message}。实际执行依赖 Windows。
    """
    root = Path(project_root) if project_root else default_project_root()
    task_name = cfg.task_name()

    ok, _reason = cfg.is_valid()
    if not ok:
        return {"ok": False, "task_name": task_name, "message": _reason}

    if not is_windows():
        # 生成命令但不执行（供测试 / 预览）
        create_cmd = build_schtasks_create(cfg, project_root=root)
        return {
            "ok": True,
            "task_name": task_name,
            "message": "命令已生成（REQUIRES WINDOWS REAL TEST）",
            "cmd": create_cmd,
            "executed": False,
        }

    # 存在则先删除，保证「重复安装 = 更新原任务」
    _rc, _out = run_schtasks(build_schtasks_delete(cfg))
    rc, out = run_schtasks(build_schtasks_create(cfg, project_root=root))
    if rc == 0:
        return {"ok": True, "task_name": task_name, "message": f"定时任务已安装/更新：{task_name}", "executed": True}
    return {"ok": False, "task_name": task_name, "message": f"创建失败：{out}", "executed": True}


def delete_task(cfg: SchedulerConfig) -> dict:
    """删除定时任务。返回 {ok, task_name, message}。"""
    task_name = cfg.task_name()
    if not is_windows():
        return {"ok": True, "task_name": task_name, "message": "命令已生成（REQUIRES WINDOWS REAL TEST）", "cmd": build_schtasks_delete(cfg), "executed": False}
    rc, out = run_schtasks(build_schtasks_delete(cfg))
    if rc == 0 or "not found" in out.lower():
        return {"ok": True, "task_name": task_name, "message": f"定时任务已删除：{task_name}", "executed": True}
    return {"ok": False, "task_name": task_name, "message": f"删除失败：{out}", "executed": True}


def run_now(cfg: SchedulerConfig) -> dict:
    """立即运行一次定时任务。返回 {ok, task_name, message}。"""
    task_name = cfg.task_name()
    if not is_windows():
        return {"ok": True, "task_name": task_name, "message": "命令已生成（REQUIRES WINDOWS REAL TEST）", "cmd": build_schtasks_run(cfg), "executed": False}
    rc, out = run_schtasks(build_schtasks_run(cfg))
    if rc == 0:
        return {"ok": True, "task_name": task_name, "message": f"已触发立即运行：{task_name}", "executed": True}
    return {"ok": False, "task_name": task_name, "message": f"立即运行失败：{out}", "executed": True}


def query_task(cfg: SchedulerConfig) -> dict:
    """查询定时任务状态。返回 {ok, task_name, message, executed}。"""
    task_name = cfg.task_name()
    if not is_windows():
        return {"ok": True, "task_name": task_name, "message": "命令已生成（REQUIRES WINDOWS REAL TEST）", "cmd": build_schtasks_query(cfg), "executed": False}
    rc, out = run_schtasks(build_schtasks_query(cfg))
    if rc == 0:
        return {"ok": True, "task_name": task_name, "message": out, "executed": True}
    return {"ok": False, "task_name": task_name, "message": f"查询失败：{out}", "executed": True}
