"""北京时间（Asia/Shanghai）展示测试。"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.beijing import fmt_date, fmt_dt, now_beijing, to_beijing  # noqa: E402


class TestBeijing:
    def test_utc_to_beijing_plus8(self):
        dt = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
        bj = to_beijing(dt)
        # UTC+8
        assert bj.hour == 20
        assert fmt_dt(dt) == "2026-08-08 20:00"

    def test_iso_string_parsing(self):
        assert fmt_dt("2026-08-08T12:00:00+00:00") == "2026-08-08 20:00"
        assert fmt_dt("2026-08-08T12:00:00Z") == "2026-08-08 20:00"
        # 无时区按 UTC 处理
        assert fmt_dt("2026-08-08T12:00:00") == "2026-08-08 20:00"

    def test_naive_returns_dash(self):
        assert fmt_dt(None) == "—"
        assert fmt_dt("") == "—"
        assert fmt_date(None) == "—"

    def test_24h_format(self):
        # 凌晨（北京 00:xx 应显示 00 而非 12）
        dt = datetime(2026, 8, 8, 16, 5, 0, tzinfo=timezone.utc)  # UTC+8 -> 8/9 00:05
        assert fmt_dt(dt) == "2026-08-09 00:05"

    def test_fmt_date_beijing(self):
        # UTC 8/8 23:00 -> 北京 8/9 07:00，日期跨天
        dt = datetime(2026, 8, 8, 23, 0, 0, tzinfo=timezone.utc)
        assert fmt_date(dt) == "2026-08-09"

    def test_now_beijing(self):
        now = now_beijing()
        assert now.tzinfo is not None
        assert now.utcoffset().total_seconds() == 8 * 3600
