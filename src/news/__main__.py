"""``python -m news`` 入口 —— 与 console script ``news`` 等价。

供 Windows Task Scheduler / BAT 通过 ``.venv\\Scripts\\python.exe -m news ...``
headless 调用（``news scheduled-fetch``、``news scheduler install`` 等），
保证后台任务不依赖 PATH 中的 ``news`` 命令即可执行。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
