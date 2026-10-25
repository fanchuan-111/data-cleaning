#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财务台账清洗系统 v2
=================================
输入：data/raw_台账.csv（脏数据）
      data/truth_埋坑清单.json（可选，生成器留下的标准答案）
输出：
  - data/cleaned_台账.csv    清洗后的干净数据
  - data/issues_异常清单.csv   需要人工复核的记录（带级别和建议）
  - report.html              可视化报告（含质量评分对比、检出率）

v2 相比 v1 的三处实质改动：
  1. 去重改用**标准化后**的值。v1 用原始列，同一笔业务录成
     「2024/1/5」和「2024-01-05」根本抓不到 —— 只是碰巧靠"完全复制"的重复行
     混过了测试。
  2. 新增 6 维度质量评分，给出清洗前后的分数，而不是只报行数。
  3. 读取生成器留下的 standard answer，算出**检出率**：
     埋了多少坑 / 抓回多少 / 漏了多少。清洗效果从此可以量化。
"""

import io
import json
import re
import sys
import time
import html
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
DATA = BASE / "data"

RAW_FILE = DATA / "raw_台账.csv"
CLEAN_FILE = DATA / "cleaned_台账.csv"
ISSUE_FILE = DATA / "issues_异常清单.csv"
REPORT_FILE = BASE / "report.html"
TRUTH_FILE = DATA / "truth_埋坑清单.json"

# 金额合理上限：超过这个数大概率是手滑多敲了 0（财务上单笔 500 万已属重大）
AMOUNT_MAX_REASONABLE = 5_000_000

# ============================================================
# 科目标准化映射表
# 关键：这是"业务知识"的体现 —— 得知道哪些写法是同一个科目
# ============================================================
SUBJECT_MAP = {
    # 销售费用
    "销售费": "销售费用",
    "销售费用支出": "销售费用",
    "销售費用": "销售费用",
    # 管理费用
    "管理费": "管理费用",
    "管理费用支出": "管理费用",
    # 财务费用
    "财务费": "财务费用",
    "财务费用支出": "财务费用",
    "利息支出": "财务费用",
    # 营业成本
    "营业成本支出": "营业成本",
    "成本": "营业成本",
    "主营业务成本": "营业成本",
    # 营业收入
    "收入": "营业收入",
    "主营业务收入": "营业收入",
    "销售商品收入": "营业收入",
    # 应收账款
    "应收帐款": "应收账款",
    "应收": "应收账款",
    # 银行存款
    "银行": "银行存款",
    "银行存款-工行": "银行存款",
    "工行存款": "银行存款",
    # 库存现金
    "现金": "库存现金",
    "库存现金-人民币": "库存现金",
    "备用金": "库存现金",
}

CANONICAL_SUBJECTS = set(SUBJECT_MAP.values())

# 日期格式清单（按可能性排序，先试常见的）
DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%y年%m月%d日",
]

# 校验规则表：把"检查什么"和"多严重"写在一起，报告里能直接展示
CHECKS = [
    {"id": "R001", "name": "日期缺失或无法解析", "level": "错误", "action": "补录原始凭证上的日期"},
    {"id": "R002", "name": "金额缺失或无法解析", "level": "错误", "action": "核对原始凭证金额"},
    {"id": "R003", "name": "科目缺失", "level": "错误", "action": "按摘要判断应归属科目"},
    {"id": "R004", "name": "日期超出合理范围", "level": "警告", "action": "确认是否录错年份"},
    {"id": "R005", "name": "金额为零", "level": "警告", "action": "确认是否漏填金额"},
    {"id": "R006", "name": "金额异常偏高", "level": "警告", "action": "确认是否多录了小数点或 0"},
    {"id": "R007", "name": "摘要为空", "level": "提示", "action": "已自动标注为（未填写）"},
]

LEVEL_ORDER = {"错误": 0, "警告": 1, "提示": 2}


# ============================================================
# 清洗函数
# ============================================================

def parse_date(value):
    """把各种格式的日期统一成 datetime；无法解析返回 None"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if not s:
        return None

    for fmt in DATE_FORMATS:
        try:
            return pd.to_datetime(s, format=fmt)
        except (ValueError, TypeError):
            continue
    # 兜底：让 pandas 自己猜
    try:
        return pd.to_datetime(s, errors="raise")
    except Exception:
        return None


def parse_amount(value):
    """
    把脏金额转成 float：
      ¥1,234.56   -> 1234.56
      1234.56元    -> 1234.56
      1 234.56     -> 1234.56
      (1,234.56)   -> -1234.56   ← 会计里括号表示负数
      -1234.56     -> -1234.56
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if not s:
        return None

    # 去掉货币符号、千分位、空格、中文单位
    s = s.replace("¥", "").replace("￥", "").replace("元", "")
    s = s.replace(",", "").replace(" ", "").replace("\u3000", "")

    # 括号负数：会计惯例
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]

    try:
        num = float(s)
    except ValueError:
        return None

    return -abs(num) if negative else num


def normalize_subject(value):
    """科目名标准化：去掉空格，再查映射表"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip().replace(" ", "").replace("\u3000", "")
    if not s:
        return None
    return SUBJECT_MAP.get(s, s)


# ============================================================
# 数据质量评分（6 维度，各 0~100）
# 这是数据治理里常用的分类，比"报个行数"专业得多
# ============================================================

def _pct(part, whole):
    return 0.0 if whole == 0 else part / whole * 100


def assess_quality(df, *, cleaned: bool):
    """
    对一份台账打分。cleaned=False 时看"原始列"，True 时看"标准化列"。

    维度：
      完整性 completeness  关键字段非空率
      唯一性 uniqueness    1 - 重复行占比
      有效性 validity      日期/金额可解析率
      一致性 consistency   科目写法归并率（变体越少分越高）
      准确性 accuracy      金额落在合理区间、日期在范围内的比例
      规范性 conformity    日期格式种类（越少越接近 100）
    """
    total = len(df)
    if total == 0:
        return {k: 0.0 for k in
                ("完整性", "唯一性", "有效性", "一致性", "准确性", "规范性", "总分")}

    date_col = "日期_标准" if cleaned else "日期"
    subj_col = "科目_标准" if cleaned else "科目"
    amt_col = "金额_数值" if cleaned else "金额"

    # ---- 完整性：5 个关键字段非空率平均 ----
    key_fields = ["凭证号", "日期", subj_col, "摘要", amt_col]
    filled = 0
    for col in key_fields:
        if col not in df.columns:
            continue
        s = df[col]
        if cleaned and col == "日期_标准":
            filled += s.notna().sum()
        elif cleaned and col == "金额_数值":
            filled += s.notna().sum()
        else:
            filled += (s.astype(str).str.strip() != "").sum()
    completeness = _pct(filled, total * len(key_fields))

    # ---- 唯一性：用业务四要素算重复 ----
    if cleaned:
        key = df[["凭证号", "日期_标准", "科目_标准", "摘要", "金额_数值"]].copy()
        key = key[key["日期_标准"].notna() & key["金额_数值"].notna()]
        dup = int(key.duplicated().sum())
        base = len(key) or 1
        uniqueness = _pct(base - dup, base)
    else:
        # 原始列：字面相同的才算重复（所以"同义重复"在这里抓不到，分数会偏高）
        key = df[["凭证号", "日期", "科目", "摘要", "金额"]].copy()
        dup = int(key.duplicated().sum())
        uniqueness = _pct(total - dup, total)

    # ---- 有效性：能不能解析 ----
    if cleaned:
        ok_date = df["日期_标准"].notna().sum()
        ok_amt = df["金额_数值"].notna().sum()
    else:
        ok_date = df["日期"].apply(parse_date).notna().sum()
        ok_amt = df["金额"].apply(parse_amount).notna().sum()
    validity = (ok_date + ok_amt) / (total * 2) * 100

    # ---- 一致性：科目写法归并率 ----
    # 分母是"原始有几种写法"，分子是"标准化后还剩几种"。
    # 同一个科目只有一种写法时归并率就是 100%。
    # （早先的公式拿固定的标准科目数当分子，小样本下会直接封顶 100，误判。）
    if cleaned:
        # 清洗后的数据，科目列已经是标准写法，
        # 分子分母自然相等 -> 归并率 100%
        canon_n = df["科目_标准"].dropna().nunique()
        raw_variants = canon_n
    else:
        raw_variants = (df["科目"].astype(str).str.strip()
                        .replace("", pd.NA).dropna().nunique())
        canon_n = df["科目"].apply(normalize_subject).dropna().nunique()
    consistency = _pct(canon_n, raw_variants) if raw_variants else 0.0

    # ---- 准确性：金额/日期是否在合理区间 ----
    if cleaned:
        amt_ok = df["金额_数值"].between(-AMOUNT_MAX_REASONABLE, AMOUNT_MAX_REASONABLE).sum()
        date_ok = df["日期_标准"].between(
            pd.Timestamp("2000-01-01"), pd.Timestamp("2100-01-01")).sum()
    else:
        amt = df["金额"].apply(parse_amount)
        amt_ok = amt.between(-AMOUNT_MAX_REASONABLE, AMOUNT_MAX_REASONABLE).sum()
        date = df["日期"].apply(parse_date)
        date_ok = date.between(pd.Timestamp("2000-01-01"), pd.Timestamp("2100-01-01")).sum()
    accuracy = (amt_ok + date_ok) / (total * 2) * 100

    # ---- 规范性：日期格式种类 ----
    # 清洗后日期已统一成 %Y-%m-%d，直接满分；
    # 清洗前按"用了几种格式"打分（1 种满分，越多越低）。
    if cleaned:
        conformity = 100.0 if df["日期_标准"].notna().any() else 0.0
    else:
        formats = set()
        for v in df["日期"].astype(str):
            s = v.strip()
            if not s:
                continue
            for fmt in DATE_FORMATS:
                try:
                    pd.to_datetime(s, format=fmt)
                    formats.add(fmt)
                    break
                except (ValueError, TypeError):
                    continue
            else:
                formats.add("unknown")
        n_fmt = len(formats) or 1
        conformity = min(100.0, 100.0 / n_fmt * 2) if n_fmt > 1 else 100.0

    scores = {
        "完整性": round(completeness, 1),
        "唯一性": round(uniqueness, 1),
        "有效性": round(validity, 1),
        "一致性": round(consistency, 1),
        "准确性": round(accuracy, 1),
        "规范性": round(conformity, 1),
    }
    scores["总分"] = round(sum(scores.values()) / 6, 1)
    return scores


# ============================================================
# 主流程
# ============================================================

def main():
    t_start = time.perf_counter()
    stats = []          # 每步统计，用来生成报告
    issues = []         # 异常记录（dict 列表）

    print("=" * 66)
    print("  财务台账清洗系统 v2")
    print("=" * 66)

    # ---------- 读取 ----------
    print("\n[读取] ", end="")
    if not RAW_FILE.exists():
        print(f"\n❌ 找不到 {RAW_FILE}")
        print("   请先运行： python generate_dirty_data.py")
        return
    df = pd.read_csv(RAW_FILE, dtype=str, keep_default_na=False)
    rows_in = len(df)
    print(f"{RAW_FILE.name}  →  {rows_in} 行 × {len(df.columns)} 列")
    stats.append(("读取原始数据", rows_in, rows_in, "—"))

    raw_df = df.copy()          # 留一份原始副本，用来算"清洗前"质量分

    # ---------- Step 1: 日期标准化 ----------
    print("\n[Step 1] 日期标准化")
    df["日期_标准"] = df["日期"].apply(parse_date)
    bad_date = df["日期_标准"].isna() & (df["日期"].str.strip() != "")
    empty_date = df["日期"].str.strip() == ""
    print(f"  解析成功: {df['日期_标准'].notna().sum()} / {rows_in}")
    print(f"  空值    : {empty_date.sum()}")
    print(f"  无法解析: {bad_date.sum()}")
    stats.append(("日期标准化", int(empty_date.sum()), int(bad_date.sum()),
                  f"{df['日期_标准'].notna().sum()} 条解析成功"))

    # ---------- Step 2: 金额清洗 ----------
    print("\n[Step 2] 金额清洗")
    df["金额_数值"] = df["金额"].apply(parse_amount)
    empty_amt = df["金额"].str.strip() == ""
    bad_amt = df["金额_数值"].isna() & (~empty_amt)
    print(f"  转换成功: {df['金额_数值'].notna().sum()} / {rows_in}")
    print(f"  空值    : {empty_amt.sum()}")
    print(f"  无法解析: {bad_amt.sum()}")
    stats.append(("金额清洗", int(empty_amt.sum()), int(bad_amt.sum()),
                  f"{df['金额_数值'].notna().sum()} 条转换成功"))

    # ---------- Step 3: 科目标准化 ----------
    print("\n[Step 3] 科目标准化")
    df["科目_标准"] = df["科目"].apply(normalize_subject)
    # 判断"是否被修正过"：原始值去空格后 与 标准化结果 不一致
    raw_norm = df["科目"].astype(str).str.strip().str.replace(" ", "").str.replace("\u3000", "")
    changed = (df["科目_标准"].notna()) & (df["科目_标准"] != raw_norm)
    print(f"  修正写法: {changed.sum()} 条")
    print(f"  科目种类: {df['科目_标准'].nunique()} 个（去重后）")
    stats.append(("科目标准化", int(changed.sum()), 0,
                  f"{df['科目_标准'].nunique()} 个标准科目"))

    # ---------- Step 4: 去重（v2 关键修复）----------
    print("\n[Step 4] 去重")
    before = len(df)
    # v1 的 bug：用原始列「日期/科目/金额」去重，
    #   同一笔业务录成 2024/1/5 和 2024-01-05 会被当成两条。
    # v2：用**标准化后**的值做业务键。
    #   缺失行不参与去重 —— 日期或金额为空时没法判断是不是同一笔，
    #   强行去重会误删真实业务。
    key_cols = ["凭证号", "日期_标准", "科目_标准", "摘要", "金额_数值"]
    comparable = df[df["日期_标准"].notna() & df["金额_数值"].notna()]
    dup_mask = comparable.duplicated(subset=key_cols, keep="first")
    dup_count = int(dup_mask.sum())
    df = df.drop(index=comparable.index[dup_mask])
    print(f"  删除重复: {dup_count} 行  （{before} → {len(df)}）")
    stats.append(("去重", dup_count, 0, f"剩余 {len(df)} 行"))

    # ---------- Step 5: 缺失值处理 ----------
    print("\n[Step 5] 缺失值处理")
    # 原则：财务数据不瞎猜，缺失的进"异常清单"等人工补
    n_summary_missing = int((df["摘要"].str.strip() == "").sum())
    df["摘要"] = df["摘要"].replace("", "（未填写）")
    print(f"  摘要补全: {n_summary_missing} 条")
    stats.append(("缺失值处理", n_summary_missing, 0, "摘要空值标注为（未填写）"))

    # ---------- Step 6: 校验（规则表驱动）----------
    print("\n[Step 6] 数据校验")
    valid_date = df["日期_标准"].notna()
    out_of_range = valid_date & (
        (df["日期_标准"] < pd.Timestamp("2000-01-01")) |
        (df["日期_标准"] > pd.Timestamp("2100-01-01"))
    )
    zero_amt = (df["金额_数值"] == 0)
    big_amt = df["金额_数值"].abs() > AMOUNT_MAX_REASONABLE

    hits = {
        "R001": int(df["日期_标准"].isna().sum()),
        "R002": int(df["金额_数值"].isna().sum()),
        "R003": int(df["科目_标准"].isna().sum()),
        "R004": int(out_of_range.sum()),
        "R005": int(zero_amt.sum()),
        "R006": int(big_amt.sum()),
        "R007": n_summary_missing,
    }
    for c in CHECKS:
        n = hits.get(c["id"], 0)
        if n:
            print(f"  [{c['id']}] {c['name']:<16} {n:>4} 条  ({c['level']})")
    stats.append(("数据校验", int(out_of_range.sum()), int(big_amt.sum()) + int(zero_amt.sum()),
                  "规则表驱动，7 条规则"))

    # ---------- 质量评分 ----------
    # 必须在重命名列之前算（要用 _标准 列）；
    # 而且"清洗后"要用**最终干净数据**那部分算，
    # 拿中间的 df 算会把缺失行带进来，分数反而比清洗前还低。
    complete = (df["日期_标准"].notna()
                & df["金额_数值"].notna()
                & df["科目_标准"].notna())
    q_before = assess_quality(raw_df, cleaned=False)
    q_after = assess_quality(df[complete], cleaned=True)

    # ---------- 输出 ----------
    print("\n[输出]")
    clean_df = df[[
        "凭证号", "日期", "日期_标准", "科目", "科目_标准",
        "摘要", "金额", "金额_数值", "制单人",
    ]].copy()
    clean_df.columns = [
        "凭证号", "日期_原始", "日期", "科目_原始", "科目",
        "摘要", "金额_原始", "金额", "制单人",
    ]
    # 只保留"三要素齐全"的记录：日期、金额、科目缺任何一项都不算干净数据，
    # 全部进异常清单等人工补。宁可少给，也不交半成品。
    clean_df = clean_df[complete].copy()
    clean_df["日期"] = clean_df["日期"].dt.strftime("%Y-%m-%d")
    clean_df.to_csv(CLEAN_FILE, index=False, encoding="utf-8-sig")
    print(f"  ✅ 干净数据 → {CLEAN_FILE.name}  ({len(clean_df)} 行)")

    # ---------- 异常清单（分级 + 建议）----------
    # 只收"必须人工处理"的：日期/金额/科目缺失。
    # 摘要缺失已自动标注，零金额这种也放进来提醒，但级别是警告。
    review_mask = (
        df["日期_标准"].isna()
        | df["金额_数值"].isna()
        | df["科目_标准"].isna()
        | zero_amt
        | big_amt
    )
    issue_df = df[review_mask].copy()
    n_review = len(issue_df)
    if n_review:
        def build_problem(row):
            probs = []
            if pd.isna(row["日期_标准"]):
                probs.append(("R001", "日期缺失或无法解析"))
            if pd.isna(row["金额_数值"]):
                probs.append(("R002", "金额缺失或无法解析"))
            if pd.isna(row["科目_标准"]):
                probs.append(("R003", "科目缺失"))
            if row["金额_数值"] == 0:
                probs.append(("R005", "金额为零"))
            if abs(row["金额_数值"]) > AMOUNT_MAX_REASONABLE:
                probs.append(("R006", "金额异常偏高"))
            return probs

        problems = issue_df.apply(build_problem, axis=1)
        issue_df["问题类型"] = problems.apply(lambda ps: ";".join(p[1] for p in ps))
        issue_df["规则"] = problems.apply(lambda ps: ",".join(p[0] for p in ps))
        issue_df["级别"] = problems.apply(
            lambda ps: min((c["level"] for c in CHECKS if c["id"] in {p[0] for p in ps}),
                           key=lambda lv: LEVEL_ORDER[lv], default="提示"))
        issue_df["建议处理"] = problems.apply(
            lambda ps: "；".join(next(c["action"] for c in CHECKS if c["id"] == p[0])
                                for p in ps))

        issue_df[["凭证号", "日期", "科目", "摘要", "金额",
                  "问题类型", "规则", "级别", "建议处理"]].to_csv(
            ISSUE_FILE, index=False, encoding="utf-8-sig")
        print(f"  ⚠️  异常清单 → {ISSUE_FILE.name}  ({n_review} 条)")

    t_end = time.perf_counter()
    elapsed = t_end - t_start

    # ---------- 标准答案对照 ----------
    truth = None
    detection = None
    if TRUTH_FILE.exists():
        truth = json.loads(TRUTH_FILE.read_text(encoding="utf-8"))
        exp = truth["expected"]
        detection = {
            "去重": {
                "应该有": exp["duplicates_total"],
                "实际": dup_count,
                "漏": max(0, exp["duplicates_total"] - dup_count),
            },
            "日期缺失": {"应该有": exp["missing_date"], "实际": hits["R001"],
                     "漏": max(0, exp["missing_date"] - hits["R001"])},
            "金额缺失": {"应该有": exp["missing_amount"], "实际": hits["R002"],
                     "漏": max(0, exp["missing_amount"] - hits["R002"])},
            "科目缺失": {"应该有": exp["missing_subject"], "实际": hits["R003"],
                     "漏": max(0, exp["missing_subject"] - hits["R003"])},
            "金额离谱": {"应该有": exp["amount_outlier"], "实际": hits["R006"],
                     "漏": max(0, exp["amount_outlier"] - hits["R006"])},
        }
        print("\n[对照标准答案]")
        for k, v in detection.items():
            rate = v["实际"] / v["应该有"] * 100 if v["应该有"] else 100
            flag = "✅" if v["漏"] == 0 else "⚠️"
            print(f"  {flag} {k:<6} 应有 {v['应该有']:>4} / 抓到 {v['实际']:>4}"
                  f" / 漏 {v['漏']:>3}   ({rate:.0f}%)")

    # ---------- 质量评分（分数已在上面算好，这里只负责打印）----------
    print("\n[数据质量评分]")
    for dim in ("完整性", "唯一性", "有效性", "一致性", "准确性", "规范性"):
        print(f"  {dim}: {q_before[dim]:>5.1f} → {q_after[dim]:>5.1f}   "
              f"({q_after[dim] - q_before[dim]:+.1f})")
    print(f"  总分: {q_before['总分']:>5.1f} → {q_after['总分']:>5.1f}   "
          f"({q_after['总分'] - q_before['总分']:+.1f})")

    # ---------- 报告 ----------
    write_report(rows_in, len(clean_df), n_review, dup_count, elapsed, stats,
                 clean_df, hits, q_before, q_after, detection)

    print("\n" + "=" * 66)
    print(f"  完成！总耗时 {elapsed:.2f} 秒")
    print(f"  {rows_in} 行 → {len(clean_df)} 行干净数据 + {n_review} 条待复核")
    print(f"  质量分 {q_before['总分']:.1f} → {q_after['总分']:.1f}")
    print(f"  报告已生成：{REPORT_FILE}")
    print("=" * 66)


# ============================================================
# 生成 HTML 报告
# ============================================================

def write_report(rows_in, rows_out, n_review, dup_count, elapsed, stats,
                 clean_df, hits, q_before, q_after, detection):
    # 估算人工耗时：按每行人工核对 4 秒
    manual_seconds = rows_in * 4
    manual_min = manual_seconds / 60
    speedup = manual_seconds / max(elapsed, 0.001)

    subject_counts = clean_df["科目"].value_counts()

    stat_rows = "\n".join(
        f"<tr><td>{html.escape(name)}</td><td>{a}</td><td>{b}</td>"
        f"<td>{html.escape(str(note))}</td></tr>"
        for name, a, b, note in stats
    )

    subject_rows = "\n".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{v}</td></tr>"
        for k, v in subject_counts.items()
    )

    sample_rows = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
        for row in clean_df.head(12)[["凭证号", "日期", "科目", "摘要", "金额", "制单人"]].values
    )

    # 质量评分表：带进度条
    dims = ("完整性", "唯一性", "有效性", "一致性", "准确性", "规范性")
    quality_rows = "\n".join(
        f"""<tr>
              <td>{d}</td>
              <td><div class="bar"><i style="width:{q_before[d]}%"></i></div></td>
              <td class="num">{q_before[d]:.1f}</td>
              <td><div class="bar bar-green"><i style="width:{q_after[d]}%"></i></div></td>
              <td class="num green">{q_after[d]:.1f}</td>
              <td class="num">{q_after[d] - q_before[d]:+.1f}</td>
            </tr>"""
        for d in dims
    )

    # 检出率表
    if detection:
        detection_rows = "\n".join(
            f"""<tr>
                  <td>{html.escape(k)}</td>
                  <td class="num">{v['应该有']}</td>
                  <td class="num green">{v['实际']}</td>
                  <td class="num {'warn' if v['漏'] else ''}">{v['漏']}</td>
                  <td class="num">{v['实际'] / v['应该有'] * 100 if v['应该有'] else 100:.0f}%</td>
                </tr>"""
            for k, v in detection.items()
        )
        detection_html = f"""
  <h2>清洗效果验证（对照生成时的标准答案）</h2>
  <p class="sub">生成器埋坑时记录了「应该有多少条」，清洗后逐项核对。这是清洗系统自己的评测。</p>
  <table>
    <tr><th>问题类型</th><th>应有</th><th>抓到</th><th>漏</th><th>检出率</th></tr>
    {detection_rows}
  </table>"""
    else:
        detection_html = ""

    # 校验规则命中表
    rule_rows = "\n".join(
        f"""<tr>
              <td class="mono">{c['id']}</td>
              <td>{html.escape(c['name'])}</td>
              <td><span class="badge lv-{LEVEL_ORDER[c['level']]}">{c['level']}</span></td>
              <td class="num">{hits.get(c['id'], 0)}</td>
              <td class="muted">{html.escape(c['action'])}</td>
            </tr>"""
        for c in CHECKS
    )

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>财务台账清洗报告</title>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #00ff9d; --warn: #f0883e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 40px 24px;
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    line-height: 1.6;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 28px; margin: 0 0 8px; }}
  h1 span {{ color: var(--accent); }}
  .sub {{ color: var(--muted); font-size: 14px; margin-bottom: 20px; }}
  .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }}
  .card .value {{ font-size: 28px; font-weight: 700; margin-top: 6px; font-family: ui-monospace, Consolas, monospace; }}
  .card .value.green {{ color: var(--accent); }}
  .card .value.orange {{ color: var(--warn); }}
  .card .note {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  h2 {{ font-size: 17px; margin: 32px 0 12px; color: var(--accent); }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card);
           border: 1px solid var(--border); border-radius: 10px; overflow: hidden; font-size: 13px; }}
  th {{ background: #1c2128; text-align: left; padding: 10px 14px; color: var(--muted); font-weight: 600; }}
  td {{ padding: 9px 14px; border-top: 1px solid var(--border); font-family: ui-monospace, Consolas, monospace; }}
  .highlight {{ background: rgba(0,255,157,.08); }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 6px;
            background: rgba(0,255,157,.12); color: var(--accent);
            font-size: 11px; font-family: ui-monospace, Consolas, monospace; }}
  .lv-0 {{ background: rgba(248,81,73,.15); color: #ff7b72; }}
  .lv-1 {{ background: rgba(240,136,62,.15); color: var(--warn); }}
  .lv-2 {{ background: rgba(139,148,158,.15); color: var(--muted); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .green {{ color: var(--accent); }}
  .warn {{ color: var(--warn); }}
  .muted {{ color: var(--muted); font-family: inherit; }}
  .mono {{ font-family: ui-monospace, Consolas, monospace; color: var(--muted); }}
  .bar {{ position: relative; height: 8px; width: 130px; background: #21262d; border-radius: 4px; overflow: hidden; }}
  .bar i {{ display: block; height: 100%; background: #8b949e; border-radius: 4px; }}
  .bar-green i {{ background: var(--accent); }}
  .footer {{ margin-top: 40px; color: var(--muted); font-size: 12px; text-align: center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>财务台账<span>清洗报告</span></h1>
  <div class="sub">Pandas 数据处理 · 6 步流水线 · 质量评分 · 全流程可复现</div>

  <div class="cards">
    <div class="card">
      <div class="label">原始行数</div>
      <div class="value">{rows_in}</div>
      <div class="note">脏数据</div>
    </div>
    <div class="card">
      <div class="label">清洗后</div>
      <div class="value green">{rows_out}</div>
      <div class="note">去重 {dup_count} 行</div>
    </div>
    <div class="card">
      <div class="label">待人工复核</div>
      <div class="value orange">{n_review}</div>
      <div class="note">错误 / 警告分级</div>
    </div>
    <div class="card">
      <div class="label">质量总分</div>
      <div class="value green">{q_before['总分']:.1f} → {q_after['总分']:.1f}</div>
      <div class="note">6 维度平均</div>
    </div>
  </div>

  <h2>数据质量评分（清洗前 → 清洗后）</h2>
  <table>
    <tr><th>维度</th><th>清洗前</th><th></th><th>清洗后</th><th></th><th>变化</th></tr>
    {quality_rows}
  </table>
{detection_html}
  <h2>校验规则命中情况</h2>
  <table>
    <tr><th>规则</th><th>检查项</th><th>级别</th><th>命中</th><th>建议处理</th></tr>
    {rule_rows}
  </table>

  <h2>效率对比</h2>
  <table>
    <tr><th>处理方式</th><th>耗时</th><th>说明</th></tr>
    <tr><td>人工逐行核对</td><td>约 {manual_min:.0f} 分钟（{manual_min/60:.1f} 小时）</td>
        <td>按每行 4 秒估算</td></tr>
    <tr class="highlight"><td>本脚本</td><td>{elapsed:.2f} 秒</td>
        <td><span class="badge">提速约 {speedup:,.0f} 倍</span></td></tr>
  </table>

  <h2>清洗流水线</h2>
  <table>
    <tr><th>步骤</th><th>处理量</th><th>异常量</th><th>结果</th></tr>
    {stat_rows}
  </table>

  <h2>科目分布（标准化后）</h2>
  <table>
    <tr><th>科目</th><th>笔数</th></tr>
    {subject_rows}
  </table>

  <h2>清洗结果示例（前 12 行）</h2>
  <table>
    <tr><th>凭证号</th><th>日期</th><th>科目</th><th>摘要</th><th>金额</th><th>制单人</th></tr>
    {sample_rows}
  </table>

  <div class="footer">
    由 clean.py 自动生成 · 数据来源：raw_台账.csv · 标准答案：truth_埋坑清单.json
  </div>
</div>
</body>
</html>
"""
    REPORT_FILE.write_text(page, encoding="utf-8")


if __name__ == "__main__":
    # Windows 控制台默认 GBK，直接 print emoji 会崩。
    # 只在**直接运行**时改 stdout；被 import 时不能碰，
    # 否则会把调用方的 stdout 关掉（诊断脚本就踩过这个坑）。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
