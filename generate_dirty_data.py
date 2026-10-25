#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成模拟的「脏」财务台账数据
=================================
这些脏数据不是随便瞎编的，而是照着代账行业真实遇到的坑设计的：

  1. 日期格式混乱   —— 「2024/1/5」「2024-01-05」「2024.1.5」「24年1月5日」
  2. 金额带杂质     —— 「¥1,234.56」「1234.56元」「1 234.56」「(1,234.56)」表示负数
  3. 科目名不统一   —— 「销售费用」「销售费」「销售费用支出」「销售 费用」
  4. 重复录入       —— 分为两种：
                       a) 完全复制（字面一模一样）
                       b) 同义重复（同一笔业务换了日期/金额/科目写法，字面不同）
                       后者才是真正考验去重逻辑的 —— 必须基于标准化后的值判断
  5. 缺失值         —— 金额空、日期空、摘要空、科目空
  6. 金额越界       —— 少量金额离谱（模拟手滑多敲几个 0）

另外输出 data/truth_埋坑清单.json：这次到底埋了多少坑。
clean.py 拿它当标准答案，算出「检出率」，证明清洗真的有效，而不是嘴上说有效。

输出：data/raw_台账.csv + data/truth_埋坑清单.json
"""

import csv
import io
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)   # 固定种子，每次生成的数据一样，方便复现

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)

# ============================================================
# 基础词典（真实会计科目和常见摘要）
# ============================================================
SUBJECTS = {
    "销售费用": ["销售费", "销售费用支出", "销售 费用", "销售費用", "销售费 "],
    "管理费用": ["管理费", "管理费用支出", "管理 费用", "管理费"],
    "财务费用": ["财务费", "财务费用支出", "财务 费用", "利息支出"],
    "营业成本": ["营业成本支出", "成本", "营业 成本", "主营业务成本"],
    "营业收入": ["收入", "营业 收入", "主营业务收入", "销售商品收入"],
    "应收账款": ["应收帐款", "应收", "应收帐款"],
    "银行存款": ["银行", "银行存款-工行", "工行存款"],
    "库存现金": ["现金", "库存现金-人民币", "备用金"],
}

SUMMARIES = [
    "报销差旅费", "支付货款", "收到货款", "发放工资", "缴纳社保",
    "支付房租", "办公用品采购", "业务招待费", "广告投放", "物流运费",
    "银行手续费", "收到投资款", "支付水电费", "购买设备", "计提折旧",
    "客户退款", "预付货款", "收到押金", "支付咨询费", "开票收入",
]

NAMES = ["张伟", "王芳", "李强", "刘洋", "陈静", "杨帆", "赵敏", "周涛"]

DATE_FORMATS = [
    "%Y/%m/%d",     # 2024/1/5
    "%Y-%m-%d",     # 2024-01-05
    "%Y.%m.%d",     # 2024.1.5
    "%y年%m月%d日",  # 24年1月5日
    "%Y%m%d",       # 20240105
]

NORMAL_ROWS = 1800
DUP_EXACT = 60        # 完全复制的重复
DUP_SEMANTIC = 40     # 同义重复（改了写法，实质相同）
MISSING_ROWS = 60
OUTLIER_ROWS = 30     # 金额离谱

# 埋坑计数器（最后写成 ground truth）
truth = {
    "generator": "generate_dirty_data.py",
    "seed": 42,
    "expected": {
        "total_rows": 0,
        "duplicates_exact": 0,
        "duplicates_semantic": 0,
        "duplicates_total": 0,
        "missing_date": 0,
        "missing_amount": 0,
        "missing_subject": 0,
        "missing_summary": 0,
        "missing_total": 0,
        "amount_outlier": 0,
        "date_format_variants": len(DATE_FORMATS),
        "amount_style_variants": 6,
    },
}


def dirty_date(dt: datetime) -> str:
    """随机挑一种格式输出日期（故意制造混乱）"""
    return dt.strftime(random.choice(DATE_FORMATS))


def dirty_amount(value: float) -> str:
    """把金额弄脏：随机加 ¥ / 元 / 千分位 / 空格 / 括号负数"""
    neg = value < 0
    v = abs(value)
    # 括号样式只能在**负数**时用：会计里 (1,234.56) 表示 -1234.56，
    # 正数套上括号语义就反了（这里曾经真出过 bug —— 同一笔业务
    # 两种写法解析出 +50832 和 -50832，去重时被判成两条不同业务）。
    styles = ["plain", "yuan_sign", "comma", "yuan_word", "space"]
    if neg:
        styles.append("paren_neg")
    style = random.choice(styles)

    if style == "plain":
        s = f"{v:.2f}"
    elif style == "yuan_sign":
        s = f"¥{v:.2f}"
    elif style == "comma":
        s = f"{v:,.2f}"
    elif style == "yuan_word":
        s = f"{v:.2f}元"
    elif style == "space":
        s = f"{v:,.2f}".replace(",", " ")
    else:  # paren_neg
        s = f"({v:,.2f})"

    return f"-{s}" if neg and style != "paren_neg" else s


def dirty_subject(canonical: str) -> str:
    """随机挑一个科目变体"""
    variants = SUBJECTS[canonical]
    if random.random() < 0.55:
        return canonical          # 55% 是标准写法
    return random.choice(variants)


def make_business(i):
    """造一笔业务的基础信息（内部用真实值，写 CSV 时才弄脏）"""
    day = datetime(2024, 1, 1) + timedelta(days=random.randint(0, 364))
    canonical = random.choice(list(SUBJECTS.keys()))
    amount = round(random.uniform(-50000, 80000), 2)
    return {
        "voucher": f"记-{i:04d}",
        "day": day,
        "canonical": canonical,
        "amount": amount,
        "summary": random.choice(SUMMARIES),
        "person": random.choice(NAMES),
    }


def render(biz):
    """把一笔业务渲染成一行脏数据"""
    return {
        "凭证号": biz["voucher"],
        "日期": dirty_date(biz["day"]),
        "科目": dirty_subject(biz["canonical"]),
        "摘要": biz["summary"],
        "金额": dirty_amount(biz["amount"]),
        "制单人": biz["person"],
    }


def main():
    rows = []      # 写进 CSV 的脏数据
    bizs = []      # 与 rows 一一对应的业务真值（用于造同义重复）

    # ---- 1. 正常业务行 ----
    for i in range(NORMAL_ROWS):
        biz = make_business(i + 1)
        rows.append(render(biz))
        bizs.append(biz)

    # ---- 2. 完全复制的重复（字面一模一样）----
    for _ in range(DUP_EXACT):
        idx = random.randrange(NORMAL_ROWS)
        rows.append(dict(rows[idx]))
        bizs.append(bizs[idx])
    truth["expected"]["duplicates_exact"] = DUP_EXACT

    # ---- 3. 同义重复（同一笔业务，日期/金额/科目都换了写法）----
    #        字面不同、实质相同 —— 只有先把值标准化再去重才能抓到
    for _ in range(DUP_SEMANTIC):
        idx = random.randrange(NORMAL_ROWS)
        biz = bizs[idx]
        dup = render(biz)                 # 同一笔业务，重新随机一种写法
        # 保险起见：如果重新渲染后恰好字面完全相同，强行换个日期格式
        if dup["日期"] == rows[idx]["日期"]:
            dup["日期"] = (biz["day"]).strftime("%Y.%m.%d")
        rows.append(dup)
        bizs.append(biz)
    truth["expected"]["duplicates_semantic"] = DUP_SEMANTIC

    # ---- 4. 缺失值 ----
    missing_counter = {"日期": 0, "金额": 0, "摘要": 0, "科目": 0}
    for _ in range(MISSING_ROWS):
        i = NORMAL_ROWS + 1000 + len(rows)          # 凭证号继续往后排
        biz = make_business(i)
        r = render(biz)
        empty_field = random.choice(["日期", "金额", "摘要", "科目"])
        r[empty_field] = ""
        missing_counter[empty_field] += 1
        rows.append(r)
        bizs.append(biz)
    truth["expected"]["missing_date"] = missing_counter["日期"]
    truth["expected"]["missing_amount"] = missing_counter["金额"]
    truth["expected"]["missing_subject"] = missing_counter["科目"]
    truth["expected"]["missing_summary"] = missing_counter["摘要"]

    # ---- 5. 金额离谱（手滑多敲几个 0）----
    for _ in range(OUTLIER_ROWS):
        i = NORMAL_ROWS + 2000 + len(rows)
        biz = make_business(i)
        biz["amount"] = round(random.uniform(5_000_000, 90_000_000), 2)
        rows.append(render(biz))
        bizs.append(biz)
    truth["expected"]["amount_outlier"] = OUTLIER_ROWS

    # ---- 打乱顺序（模拟真实台账的乱序）----
    random.shuffle(rows)

    truth["expected"]["total_rows"] = len(rows)
    truth["expected"]["missing_total"] = sum(missing_counter.values())
    truth["expected"]["duplicates_total"] = DUP_EXACT + DUP_SEMANTIC

    # ---- 写出 ----
    out = OUT_DIR / "raw_台账.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["凭证号", "日期", "科目", "摘要", "金额", "制单人"])
        writer.writeheader()
        writer.writerows(rows)

    truth_file = OUT_DIR / "truth_埋坑清单.json"
    truth_file.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")

    e = truth["expected"]
    print(f"✅ 已生成脏数据: {out}")
    print(f"   共 {len(rows)} 行")
    print()
    print("   故意埋的坑（这就是 clean.py 的标准答案）：")
    print(f"     · 日期格式    {e['date_format_variants']} 种混用")
    print(f"     · 金额写法    {e['amount_style_variants']} 种混用（¥ / 元 / 千分位 / 空格 / 括号负数）")
    print(f"     · 科目变体    每个标准科目 3-5 个写法")
    print(f"     · 完全重复    {DUP_EXACT} 行（字面一模一样）")
    print(f"     · 同义重复    {DUP_SEMANTIC} 行（换了写法，实质相同）← 只能靠标准化后去重抓到")
    print(f"     · 缺失值      {e['missing_total']} 行"
          f"（日期 {e['missing_date']} / 金额 {e['missing_amount']}"
          f" / 科目 {e['missing_subject']} / 摘要 {e['missing_summary']}）")
    print(f"     · 金额离谱    {e['amount_outlier']} 行（手滑多敲几个 0）")
    print()
    print(f"   标准答案已写入: {truth_file.name}")
    print()
    print("   前 8 行预览：")
    for r in rows[:8]:
        print(f"     {r['凭证号']:<10} {r['日期']:<14} {r['科目']:<12} "
              f"{r['摘要']:<10} {r['金额']:<14} {r['制单人']}")


if __name__ == "__main__":
    # 只在直接运行时改 stdout（被 import 时不能碰，否则会关掉调用方的 stdout）
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
