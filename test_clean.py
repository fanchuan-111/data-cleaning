"""数据清洗核心逻辑的测试。

跑法：
    py -m pytest test_clean.py -v
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clean import (  # noqa: E402
    parse_date, parse_amount, normalize_subject, assess_quality,
)
from generate_dirty_data import dirty_amount, dirty_date  # noqa: E402


class TestParseDate:
    @pytest.mark.parametrize("raw,expected", [
        ("2024-01-05", "2024-01-05"),
        ("2024/01/05", "2024-01-05"),
        ("2024.01.05", "2024-01-05"),
        ("20240105", "2024-01-05"),
        ("24年01月05日", "2024-01-05"),
    ])
    def test_all_formats_normalize_to_same_day(self, raw, expected):
        got = parse_date(raw)
        assert got is not None, f"{raw} 应该能解析"
        assert got.strftime("%Y-%m-%d") == expected

    @pytest.mark.parametrize("raw", ["", "   ", None, "不是日期", "13月45日", "abc"])
    def test_invalid_returns_none(self, raw):
        assert parse_date(raw) is None


class TestParseAmount:
    @pytest.mark.parametrize("raw,expected", [
        ("1234.56", 1234.56),
        ("¥1,234.56", 1234.56),
        ("￥1234.56", 1234.56),
        ("1234.56元", 1234.56),
        ("1 234.56", 1234.56),
        ("(1,234.56)", -1234.56),      # 会计括号负数
        ("-1234.56", -1234.56),
        ("-¥1,234.56", -1234.56),
        ("（1,234.56）", None),         # 全角括号暂不支持，明确返回 None 而不是瞎猜
    ])
    def test_parse(self, raw, expected):
        assert parse_amount(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", "¥", "元"])
    def test_invalid_returns_none(self, raw):
        assert parse_amount(raw) is None


class TestDirtyAmountRoundTrip:
    """生成器造的脏金额，必须能被解析回原值。

    这里有一条回归测试：生成器曾经在**正数**上也随机用括号样式，
    而会计括号表示负数 —— 结果同一笔业务两种写法解析出 +50832 和 -50832，
    去重时被判成两条不同业务，白丢一行干净数据。
    """

    def test_positive_amount_never_becomes_negative(self):
        for _ in range(500):
            raw = dirty_amount(1234.56)
            assert parse_amount(raw) == pytest.approx(1234.56), f"{raw} 解析错了"

    def test_negative_amount_stays_negative(self):
        for _ in range(500):
            raw = dirty_amount(-1234.56)
            assert parse_amount(raw) == pytest.approx(-1234.56), f"{raw} 解析错了"

    def test_dirty_date_round_trip(self):
        for _ in range(200):
            raw = dirty_date(pd.Timestamp("2024-06-18").to_pydatetime())
            assert parse_date(raw) is not None, f"{raw} 解析失败"


class TestNormalizeSubject:
    @pytest.mark.parametrize("raw,expected", [
        ("销售费", "销售费用"),
        ("销售 费用", "销售费用"),
        ("销售費用", "销售费用"),
        ("利息支出", "财务费用"),
        ("主营业务收入", "营业收入"),
        ("备用金", "库存现金"),
        ("销售费用", "销售费用"),        # 本身就是标准写法
        ("某个奇怪的科目", "某个奇怪的科目"),  # 不认识就原样保留，不瞎归并
    ])
    def test_mapping(self, raw, expected):
        assert normalize_subject(raw) == expected

    @pytest.mark.parametrize("raw", ["", "  ", None])
    def test_empty(self, raw):
        assert normalize_subject(raw) is None


class TestAssessQuality:
    def _frame(self, dates, subjects, amounts):
        return pd.DataFrame({
            "凭证号": [f"记-{i}" for i in range(len(dates))],
            "日期": dates,
            "科目": subjects,
            "摘要": ["测试"] * len(dates),
            "金额": amounts,
        })

    def test_dirty_scores_lower_than_clean(self):
        dirty = self._frame(
            ["2024/1/5", "2024-01-05", "20240105"],
            ["销售费", "销售费用", "销售費用"],
            ["¥100.00", "100.00", "100元"],
        )
        scores = assess_quality(dirty, cleaned=False)
        assert 0 <= scores["总分"] <= 100
        assert scores["一致性"] < 100, "科目有三种写法，一致性不该满分"
        assert scores["规范性"] < 100, "日期有三种格式，规范性不该满分"

    def test_all_empty_frame_does_not_crash(self):
        empty = self._frame([], [], [])
        scores = assess_quality(empty, cleaned=False)
        assert scores["总分"] == 0.0


class TestTruthConsistency:
    """生成器声明的坑数，内部必须自洽。"""

    def test_duplicates_add_up(self):
        truth_path = Path(__file__).parent / "data" / "truth_埋坑清单.json"
        if not truth_path.exists():
            pytest.skip("还没跑过 generate_dirty_data.py")
        exp = json.loads(truth_path.read_text(encoding="utf-8"))["expected"]
        assert exp["duplicates_total"] == exp["duplicates_exact"] + exp["duplicates_semantic"]
        assert exp["missing_total"] == (
            exp["missing_date"] + exp["missing_amount"]
            + exp["missing_subject"] + exp["missing_summary"]
        )
        assert exp["total_rows"] > 0
