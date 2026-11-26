#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""预算分配计算器（budget-split-calculator 的计算与校验脚本）。

只用标准库，金额全程用 Decimal，不会出现浮点误差。运行方式：

    uv run scripts/budget_calc.py allocate input.json [--format md|json] [--csv out.csv]
    uv run scripts/budget_calc.py split --amount 1200 --labels 第1周,第2周,第3周,第4周

退出码：0 = 正常；1 = 预算不够或条件冲突（仍会输出缺口报告）；2 = 输入有误。

allocate 的输入 JSON：
{
  "currency": "CNY",            // 可选，仅用于显示
  "total": 12000,               // 总预算，必填
  "step": 1,                    // 可选，金额最小单位，默认 1；有分的币种用 0.01
  "buffer_rate": 0.05,          // 可选，应急缓冲占可支配金额的比例，默认 0.05
  "weights": {"high": 3, "medium": 2, "low": 1},   // 可选，优先级权重
  "fixed": [{"name": "房租", "amount": 3500}],
  "categories": [
    {"name": "储蓄", "priority": "high"},             // 优先级：high/medium/low 或 高/中/低
    {"name": "学习", "amount": 800},                  // 指定金额
    {"name": "娱乐", "percent": 10, "max": 1500},     // 指定比例（10 表示总预算的 10%）
    {"name": "餐饮", "priority": "medium", "min": 1500, "max": 3000}
  ]
}

规则：
1. 可支配金额 = 总预算 - 固定支出合计。
2. 应急缓冲 = 可支配金额 x buffer_rate，向下取整到 step，不参与分配。
3. 指定了 amount 或 percent 的类别先按指定值满足，不会被缩减。
4. 剩余金额按优先级权重分给其他类别，遵守各自的 min / max。
5. 所有金额向下取整到 step，取整差额与因上限未分完的金额放进「余数」行。
6. 预算不够或条件冲突时，只报告缺口，不自行缩减任何一项。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from decimal import ROUND_DOWN, Decimal, InvalidOperation

PRIORITY_ALIASES = {
    "high": "high", "h": "high", "高": "high",
    "medium": "medium", "mid": "medium", "m": "medium", "中": "medium",
    "low": "low", "l": "low", "低": "low",
}
PRIORITY_LABEL = {"high": "高", "medium": "中", "low": "低"}
DEFAULT_WEIGHTS = {"high": Decimal(3), "medium": Decimal(2), "low": Decimal(1)}
SHORTFALL_OPTIONS = [
    "降低某个类别的金额或比例",
    "减少应急缓冲",
    "增加总预算",
    "调整或削减固定支出",
]


class InputError(Exception):
    """输入数据有误。"""


# ---------------------------------------------------------------- 基础工具
def to_decimal(value, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InputError(f"{field} 必须是数字")
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise InputError(f"{field} 必须是数字，收到：{value!r}") from None
    if not d.is_finite():
        raise InputError(f"{field} 必须是有限数字")
    return d


def floor_to(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(ROUND_DOWN) * step


def places_of(step: Decimal) -> int:
    exp = step.normalize().as_tuple().exponent
    return max(0, -exp)


def fmt(x: Decimal, places: int) -> str:
    return f"{x:.{places}f}"


def pct(amount: Decimal, base: Decimal) -> str:
    if base == 0:
        return "0.0%"
    return f"{(amount / base * 100):.1f}%"


def check_multiple(value: Decimal, step: Decimal, field: str) -> None:
    if value % step != 0:
        raise InputError(f"{field}={value} 不是 step={step} 的整数倍，请调整金额或把 step 改小（如 0.01）")


# ---------------------------------------------------------------- 输入解析
def parse_allocate_input(raw) -> dict:
    if not isinstance(raw, dict):
        raise InputError("输入必须是 JSON 对象")

    total = to_decimal(raw.get("total"), "total")
    if total <= 0:
        raise InputError("total 必须大于 0")
    step = to_decimal(raw.get("step", 1), "step")
    if step <= 0:
        raise InputError("step 必须大于 0")
    check_multiple(total, step, "total")

    rate = to_decimal(raw.get("buffer_rate", "0.05"), "buffer_rate")
    if not (Decimal(0) <= rate < Decimal(1)):
        raise InputError("buffer_rate 必须在 0 到 1 之间（0.05 表示 5%）")

    weights = dict(DEFAULT_WEIGHTS)
    for key, value in (raw.get("weights") or {}).items():
        alias = PRIORITY_ALIASES.get(str(key).strip().lower())
        if alias is None:
            raise InputError(f"weights 里的优先级 {key!r} 无法识别，可用 high/medium/low 或 高/中/低")
        w = to_decimal(value, f"weights.{key}")
        if w <= 0:
            raise InputError(f"weights.{key} 必须大于 0")
        weights[alias] = w

    seen: set[str] = set()

    def unique_name(item, where: str, idx: int) -> str:
        if not isinstance(item, dict):
            raise InputError(f"{where}[{idx}] 必须是对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise InputError(f"{where}[{idx}] 缺少 name")
        if name in seen:
            raise InputError(f"名称重复：{name}")
        seen.add(name)
        return name

    fixed = []
    for i, item in enumerate(raw.get("fixed") or []):
        name = unique_name(item, "fixed", i)
        amount = to_decimal(item.get("amount"), f"fixed[{name}].amount")
        if amount < 0:
            raise InputError(f"fixed[{name}].amount 不能为负数")
        check_multiple(amount, step, f"fixed[{name}].amount")
        fixed.append({"name": name, "amount": amount})

    categories = []
    for i, item in enumerate(raw.get("categories") or []):
        name = unique_name(item, "categories", i)
        has_amount = item.get("amount") is not None
        has_percent = item.get("percent") is not None
        if has_amount and has_percent:
            raise InputError(f"类别「{name}」不能同时指定 amount 和 percent")
        cat = {"name": name, "spec_amount": None, "spec_percent": None,
               "min": None, "max": None, "weight": None, "priority": None, "priority_missing": False}
        if has_amount:
            cat["spec_amount"] = to_decimal(item["amount"], f"categories[{name}].amount")
            if cat["spec_amount"] < 0:
                raise InputError(f"类别「{name}」的 amount 不能为负数")
            check_multiple(cat["spec_amount"], step, f"categories[{name}].amount")
        if has_percent:
            cat["spec_percent"] = to_decimal(item["percent"], f"categories[{name}].percent")
            if not (Decimal(0) <= cat["spec_percent"] <= Decimal(100)):
                raise InputError(f"类别「{name}」的 percent 必须在 0 到 100 之间")
        for bound in ("min", "max"):
            if item.get(bound) is not None:
                cat[bound] = to_decimal(item[bound], f"categories[{name}].{bound}")
                if cat[bound] < 0:
                    raise InputError(f"类别「{name}」的 {bound} 不能为负数")
                check_multiple(cat[bound], step, f"categories[{name}].{bound}")
        if cat["min"] is not None and cat["max"] is not None and cat["min"] > cat["max"]:
            raise InputError(f"类别「{name}」的 min 大于 max")
        if item.get("weight") is not None:
            cat["weight"] = to_decimal(item["weight"], f"categories[{name}].weight")
            if cat["weight"] <= 0:
                raise InputError(f"类别「{name}」的 weight 必须大于 0")
        if item.get("priority") is not None:
            p = PRIORITY_ALIASES.get(str(item["priority"]).strip().lower())
            if p is None:
                raise InputError(f"类别「{name}」的 priority 无法识别，可用 high/medium/low 或 高/中/低")
            cat["priority"] = p
        is_specified = has_amount or has_percent
        if not is_specified and cat["weight"] is None and cat["priority"] is None:
            cat["priority"] = "medium"
            cat["priority_missing"] = True
        if cat["weight"] is None and cat["priority"] is not None:
            cat["weight"] = weights[cat["priority"]]
        categories.append(cat)

    if not categories:
        raise InputError("categories 不能为空，至少要有一个分配类别")

    return {"currency": str(raw.get("currency") or ""), "total": total, "step": step,
            "rate": rate, "fixed": fixed, "categories": categories}


# ---------------------------------------------------------------- 加权分配
def weighted_fill(items: list[dict], pool: Decimal) -> tuple[dict[str, Decimal], dict[str, str], Decimal]:
    """按权重分配 pool，遵守每项的 min/max。返回 (金额, 触及的边界, 未分完的金额)。"""
    alloc: dict[str, Decimal] = {}
    bound_hit: dict[str, str] = {}
    active = list(items)
    left = pool
    while active:
        total_w = sum(i["weight"] for i in active)
        shares = {i["name"]: left * i["weight"] / total_w for i in active}
        low = [i for i in active if i["min"] is not None and shares[i["name"]] < i["min"]]
        high = [i for i in active if i["max"] is not None and shares[i["name"]] > i["max"]]
        if not low and not high:
            alloc.update(shares)
            left = Decimal(0)
            active = []
            break
        net = sum(i["min"] - shares[i["name"]] for i in low) - sum(shares[i["name"]] - i["max"] for i in high)
        fix = low if net > 0 else high if net < 0 else low + high
        for i in fix:
            if i in low:
                alloc[i["name"]], bound_hit[i["name"]] = i["min"], "min"
            else:
                alloc[i["name"]], bound_hit[i["name"]] = i["max"], "max"
            left -= alloc[i["name"]]
            active.remove(i)
    return alloc, bound_hit, left


# ---------------------------------------------------------------- 主计算
def allocate(data: dict) -> dict:
    total, step, rate = data["total"], data["step"], data["rate"]
    places = places_of(step)
    warnings: list[str] = []

    fixed_total = sum((f["amount"] for f in data["fixed"]), Decimal(0))
    disposable = total - fixed_total
    base = {"currency": data["currency"], "total": total, "step": step, "places": places,
            "fixed_total": fixed_total, "disposable": disposable, "buffer_rate": rate}

    if disposable < 0:
        return {**base, "status": "shortfall", "gap": -disposable,
                "reasons": [f"固定支出合计 {fmt(fixed_total, places)} 已超过总预算 {fmt(total, places)}"],
                "options": SHORTFALL_OPTIONS, "rows": [], "warnings": warnings}

    buffer = floor_to(disposable * rate, step)

    specified, flexible = [], []
    conflicts: list[str] = []
    for c in data["categories"]:
        if c["spec_amount"] is not None or c["spec_percent"] is not None:
            if c["spec_amount"] is not None:
                amount, note = c["spec_amount"], "我指定的金额"
            else:
                amount = floor_to(total * c["spec_percent"] / 100, step)
                note = f"我指定的比例 {c['spec_percent'].normalize():f}%"
            if c["min"] is not None and amount < c["min"]:
                conflicts.append(f"类别「{c['name']}」指定值 {fmt(amount, places)} 低于它自己的下限 {fmt(c['min'], places)}")
            if c["max"] is not None and amount > c["max"]:
                conflicts.append(f"类别「{c['name']}」指定值 {fmt(amount, places)} 高于它自己的上限 {fmt(c['max'], places)}")
            specified.append({"name": c["name"], "amount": amount, "note": note})
        else:
            flexible.append(c)
            if c["priority_missing"]:
                warnings.append(f"类别「{c['name']}」没给优先级，已按「中」处理，建议向用户确认")

    spec_sum = sum((s["amount"] for s in specified), Decimal(0))
    min_sum = sum((c["min"] or Decimal(0) for c in flexible), Decimal(0))
    required = spec_sum + buffer + min_sum

    if conflicts or required > disposable:
        reasons = list(conflicts)
        gap = max(required - disposable, Decimal(0))
        if required > disposable:
            parts = [f"指定金额合计 {fmt(spec_sum, places)}", f"应急缓冲 {fmt(buffer, places)}"]
            if min_sum > 0:
                parts.append(f"各类别下限合计 {fmt(min_sum, places)}")
            reasons.append(f"{' + '.join(parts)} = {fmt(required, places)}，超过可支配金额 {fmt(disposable, places)}")
        return {**base, "status": "shortfall", "gap": gap, "buffer": buffer, "reasons": reasons,
                "options": SHORTFALL_OPTIONS, "rows": [], "warnings": warnings}

    pool = disposable - buffer - spec_sum
    raw_alloc, bound_hit, unallocated_by_cap = weighted_fill(flexible, pool) if flexible else ({}, {}, pool)

    rows = [{"kind": "fixed", "name": f["name"], "amount": f["amount"], "note": "固定支出"} for f in data["fixed"]]
    spec_by_name = {s["name"]: s for s in specified}
    for c in data["categories"]:
        if c["name"] in spec_by_name:
            s = spec_by_name[c["name"]]
            rows.append({"kind": "category", "name": s["name"], "amount": s["amount"], "note": s["note"]})
        else:
            amount = floor_to(raw_alloc[c["name"]], step)
            if c["min"] is not None:
                amount = max(amount, c["min"])
            if c["max"] is not None:
                amount = min(amount, c["max"])
            if c["priority"] is not None and c["weight"] is not None:
                note = f"按优先级「{PRIORITY_LABEL[c['priority']]}」分配，权重 {c['weight'].normalize():f}"
            else:
                note = f"按权重 {c['weight'].normalize():f} 分配"
            hit = bound_hit.get(c["name"])
            if hit == "max":
                note += f"；已按上限 {fmt(c['max'], places)} 封顶"
            elif hit == "min":
                note += f"；已按下限 {fmt(c['min'], places)} 保底"
            rows.append({"kind": "category", "name": c["name"], "amount": amount, "note": note})

    rows.append({"kind": "buffer", "name": "应急缓冲", "amount": buffer, "note": f"缓冲 {(rate * 100).normalize():f}%（占可支配金额）"})
    used = sum((r["amount"] for r in rows), Decimal(0))
    remainder = total - used
    if remainder < 0:  # 理论上不会发生，作为兜底
        raise InputError("内部校验失败：分配金额超过总预算，请检查输入")
    rem_note = "取整差额"
    if unallocated_by_cap > 0:
        rem_note = "取整差额；另有金额因类别上限未能分完，请询问用户如何处理"
        warnings.append(f"有 {fmt(unallocated_by_cap, places)} 因类别上限没分完，已计入余数，需要用户决定去向")
    rows.append({"kind": "remainder", "name": "余数", "amount": remainder, "note": rem_note})

    row_sum = sum((r["amount"] for r in rows), Decimal(0))
    return {**base, "status": "ok", "buffer": buffer, "rows": rows, "row_sum": row_sum,
            "check_ok": row_sum == total, "warnings": warnings}


# ---------------------------------------------------------------- 等额或加权拆分（明细用）
def split_amount(amount: Decimal, labels: list[str], weights: list[Decimal], step: Decimal) -> dict:
    if not labels:
        raise InputError("labels 不能为空")
    if len(set(labels)) != len(labels):
        raise InputError("labels 里有重复项")
    if len(weights) != len(labels):
        raise InputError(f"weights 数量（{len(weights)}）与 labels 数量（{len(labels)}）不一致")
    if any(w <= 0 for w in weights):
        raise InputError("weights 必须全部大于 0")
    if amount < 0:
        raise InputError("amount 不能为负数")
    check_multiple(amount, step, "amount")

    units = int(amount / step)
    tw = sum(weights)
    exact = [Decimal(units) * w / tw for w in weights]
    base = [int(e.to_integral_value(ROUND_DOWN)) for e in exact]
    extra = units - sum(base)
    order = sorted(range(len(labels)), key=lambda i: (-(exact[i] - base[i]), i))
    bumped = set(order[:extra])
    rows = []
    for i, label in enumerate(labels):
        n = base[i] + (1 if i in bumped else 0)
        rows.append({"name": label, "amount": Decimal(n) * step,
                     "note": "含取整补差" if i in bumped else ""})
    row_sum = sum((r["amount"] for r in rows), Decimal(0))
    return {"status": "ok", "total": amount, "step": step, "places": places_of(step),
            "rows": rows, "row_sum": row_sum, "check_ok": row_sum == amount, "warnings": []}


# ---------------------------------------------------------------- 输出
def render_allocate_md(res: dict) -> str:
    p = res["places"]
    unit = f" {res['currency']}" if res["currency"] else ""
    out = [f"分配方法：零基预算（先扣固定支出，再按优先级权重分配）",
           f"总预算：{fmt(res['total'], p)}{unit}　固定支出合计：{fmt(res['fixed_total'], p)}　可支配金额：{fmt(res['disposable'], p)}"]
    if res["status"] == "shortfall":
        out.append("")
        out.append(f"**预算不足，缺口 {fmt(res['gap'], p)}{unit}。未做任何分配。**")
        for reason in res["reasons"]:
            out.append(f"- {reason}")
        out.append("可选的取舍方向（由用户决定）：" + "；".join(res["options"]))
    else:
        out.append("")
        out.append("| 类别 | 金额 | 占比 | 备注 |")
        out.append("|---|---:|---:|---|")
        for r in res["rows"]:
            out.append(f"| {r['name']} | {fmt(r['amount'], p)} | {pct(r['amount'], res['total'])} | {r['note']} |")
        verdict = "校验通过：合计 = 总预算" if res["check_ok"] else "校验失败：合计 ≠ 总预算"
        out.append(f"| **合计** | **{fmt(res['row_sum'], p)}** | **{pct(res['row_sum'], res['total'])}** | {verdict} |")
    for w in res["warnings"]:
        out.append(f"\n注意：{w}")
    return "\n".join(out)


def render_split_md(res: dict) -> str:
    p = res["places"]
    out = ["| 项目 | 金额 | 占比 | 备注 |", "|---|---:|---:|---|"]
    for r in res["rows"]:
        out.append(f"| {r['name']} | {fmt(r['amount'], p)} | {pct(r['amount'], res['total'])} | {r['note']} |")
    verdict = "校验通过：合计 = 拆分前金额" if res["check_ok"] else "校验失败：合计 ≠ 拆分前金额"
    out.append(f"| **合计** | **{fmt(res['row_sum'], p)}** | **{pct(res['row_sum'], res['total'])}** | {verdict} |")
    return "\n".join(out)


def jsonable(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def write_csv(path: str, res: dict, first_header: str, overwrite: bool) -> None:
    if os.path.exists(path) and not overwrite:
        raise InputError(f"文件已存在：{path}。如需覆盖，请加 --overwrite")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    p = res["places"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # utf-8-sig 让 Excel 正确显示中文
        w = csv.writer(fh)
        w.writerow([first_header, "金额", "占比", "备注"])
        for r in res["rows"]:
            w.writerow([r["name"], fmt(r["amount"], p), pct(r["amount"], res["total"]), r["note"]])
        w.writerow(["合计", fmt(res["row_sum"], p), pct(res["row_sum"], res["total"]),
                    "校验通过" if res["check_ok"] else "校验失败"])


# ---------------------------------------------------------------- 命令行
def read_json(path: str):
    try:
        if path == "-":
            return json.loads(sys.stdin.read(), parse_float=Decimal)
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh, parse_float=Decimal)
    except FileNotFoundError:
        raise InputError(f"找不到输入文件：{path}") from None
    except json.JSONDecodeError as e:
        raise InputError(f"输入不是有效的 JSON：{e}") from None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="budget_calc.py", description="预算分配计算与校验")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("allocate", help="按输入 JSON 计算总额分配")
    a.add_argument("input", help="输入 JSON 文件路径，- 表示标准输入")

    s = sub.add_parser("split", help="把一个金额拆成若干份（用于明细），保证合计精确")
    s.add_argument("--amount", required=True, help="要拆分的金额")
    s.add_argument("--labels", required=True, help="各份的名称，用逗号分隔，如 第1周,第2周")
    s.add_argument("--weights", help="各份的权重，用逗号分隔；不给则等分")
    s.add_argument("--step", default="1", help="金额最小单位，默认 1")

    for sp in (a, s):
        sp.add_argument("--format", choices=["md", "json"], default="md", help="输出格式，默认 md")
        sp.add_argument("--csv", metavar="PATH", help="同时导出 CSV（UTF-8 带 BOM，Excel 可直接打开）")
        sp.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的 CSV 文件")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "allocate":
            res = allocate(parse_allocate_input(read_json(args.input)))
            text = render_allocate_md(res)
            first_header = "类别"
        else:
            step = to_decimal(args.step, "step")
            if step <= 0:
                raise InputError("step 必须大于 0")
            labels = [x.strip() for x in args.labels.split(",") if x.strip()]
            weights = ([to_decimal(x.strip(), "weights") for x in args.weights.split(",")]
                       if args.weights else [Decimal(1)] * len(labels))
            res = split_amount(to_decimal(args.amount, "amount"), labels, weights, step)
            text = render_split_md(res)
            first_header = "项目"

        print(json.dumps(jsonable(res), ensure_ascii=False, indent=2) if args.format == "json" else text)
        if args.csv:
            if res["status"] != "ok":
                print("预算不足，未导出 CSV。", file=sys.stderr)
            else:
                write_csv(args.csv, res, first_header, args.overwrite)
                print(f"已导出：{args.csv}", file=sys.stderr)
        return 0 if res["status"] == "ok" else 1
    except InputError as e:
        print(f"输入有误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
