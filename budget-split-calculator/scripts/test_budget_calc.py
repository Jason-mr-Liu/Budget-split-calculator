# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""budget_calc.py 的自测。运行：uv run scripts/test_budget_calc.py"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget_calc as bc  # noqa: E402


def run_allocate(raw: dict) -> dict:
    parsed = bc.parse_allocate_input(json.loads(json.dumps(raw), parse_float=Decimal))
    return bc.allocate(parsed)


def amounts(res: dict) -> dict:
    return {r["name"]: r["amount"] for r in res["rows"]}


BASE = {
    "currency": "CNY",
    "total": 12000,
    "fixed": [{"name": "房租", "amount": 3500}, {"name": "手机费", "amount": 100}, {"name": "还款", "amount": 1500}],
    "categories": [
        {"name": "储蓄", "priority": "high"},
        {"name": "餐饮", "priority": "medium"},
        {"name": "娱乐", "priority": "low"},
        {"name": "学习", "amount": 800},
    ],
}


class AllocateTests(unittest.TestCase):
    def test_basic_split_and_sum(self):
        res = run_allocate(BASE)
        self.assertEqual(res["status"], "ok")
        a = amounts(res)
        # 可支配 6900；缓冲 5% = 345；学习 800；剩余 5755 按 3:2:1 分
        self.assertEqual(a["应急缓冲"], Decimal(345))
        self.assertEqual(a["学习"], Decimal(800))
        self.assertEqual(a["储蓄"], Decimal(2877))
        self.assertEqual(a["餐饮"], Decimal(1918))
        self.assertEqual(a["娱乐"], Decimal(959))
        self.assertEqual(a["余数"], Decimal(1))
        self.assertTrue(res["check_ok"])
        self.assertEqual(res["row_sum"], Decimal(12000))

    def test_never_exceeds_total(self):
        for total in (997, 1001, 12345, 99999):
            raw = dict(BASE, total=total, fixed=[])
            res = run_allocate(raw)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["row_sum"], Decimal(total))
            self.assertGreaterEqual(amounts(res)["余数"], 0)

    def test_fixed_exceeds_total_is_shortfall(self):
        res = run_allocate(dict(BASE, total=4000))
        self.assertEqual(res["status"], "shortfall")
        self.assertEqual(res["gap"], Decimal(1100))
        self.assertEqual(res["rows"], [])

    def test_specified_plus_buffer_exceeds_disposable(self):
        raw = dict(BASE, categories=[{"name": "学习", "amount": 7000}, {"name": "储蓄", "priority": "high"}])
        res = run_allocate(raw)
        self.assertEqual(res["status"], "shortfall")
        # 可支配 6900，缓冲 345，指定 7000 → 需要 7345，缺口 445
        self.assertEqual(res["gap"], Decimal(445))

    def test_max_cap_redistributes(self):
        raw = {"total": 1000, "buffer_rate": 0, "categories": [
            {"name": "A", "priority": "medium", "max": 100},
            {"name": "B", "priority": "medium"}]}
        res = run_allocate(raw)
        a = amounts(res)
        self.assertEqual((a["A"], a["B"]), (Decimal(100), Decimal(900)))

    def test_min_floor_applies(self):
        raw = {"total": 1000, "buffer_rate": 0, "categories": [
            {"name": "A", "priority": "low", "min": 500},
            {"name": "B", "priority": "high"}]}
        a = amounts(run_allocate(raw))
        self.assertEqual((a["A"], a["B"]), (Decimal(500), Decimal(500)))

    def test_all_capped_leaves_unallocated_in_remainder_with_warning(self):
        raw = {"total": 1000, "buffer_rate": 0, "categories": [
            {"name": "A", "priority": "high", "max": 100}]}
        res = run_allocate(raw)
        self.assertEqual(amounts(res)["余数"], Decimal(900))
        self.assertTrue(res["warnings"])
        self.assertTrue(res["check_ok"])

    def test_percent_specified(self):
        raw = {"total": 10000, "buffer_rate": 0, "categories": [
            {"name": "储蓄", "percent": 20}, {"name": "其他", "priority": "medium"}]}
        a = amounts(run_allocate(raw))
        self.assertEqual(a["储蓄"], Decimal(2000))
        self.assertEqual(a["其他"], Decimal(8000))

    def test_specified_conflicts_with_own_max(self):
        raw = {"total": 1000, "categories": [{"name": "A", "amount": 500, "max": 300}]}
        res = run_allocate(raw)
        self.assertEqual(res["status"], "shortfall")
        self.assertTrue(res["reasons"])

    def test_cents_step(self):
        raw = {"total": "100.00", "step": "0.01", "buffer_rate": 0, "categories": [
            {"name": "A", "priority": "medium"}, {"name": "B", "priority": "medium"},
            {"name": "C", "priority": "medium"}]}
        res = run_allocate(raw)
        self.assertEqual(res["row_sum"], Decimal("100.00"))
        self.assertEqual(amounts(res)["A"], Decimal("33.33"))
        self.assertEqual(amounts(res)["余数"], Decimal("0.01"))

    def test_missing_priority_warns(self):
        raw = {"total": 1000, "categories": [{"name": "A"}]}
        self.assertTrue(run_allocate(raw)["warnings"])

    def test_bad_inputs_raise(self):
        bad = [
            {"total": -1, "categories": [{"name": "A"}]},
            {"total": 1000, "categories": []},
            {"total": 1000, "categories": [{"name": "A"}, {"name": "A"}]},
            {"total": 1000, "categories": [{"name": "A", "amount": 1, "percent": 1}]},
            {"total": 1000, "categories": [{"name": "A", "priority": "超高"}]},
            {"total": 1000, "buffer_rate": 1.5, "categories": [{"name": "A"}]},
            {"total": 1000.5, "categories": [{"name": "A"}]},
            {"total": "abc", "categories": [{"name": "A"}]},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(bc.InputError):
                run_allocate(raw)


class SplitTests(unittest.TestCase):
    def test_even_split_sums_exactly(self):
        res = bc.split_amount(Decimal(1000), ["a", "b", "c"], [Decimal(1)] * 3, Decimal(1))
        self.assertEqual([r["amount"] for r in res["rows"]], [Decimal(334), Decimal(333), Decimal(333)])
        self.assertTrue(res["check_ok"])

    def test_weighted_split_sums_exactly(self):
        res = bc.split_amount(Decimal(1000), ["a", "b", "c"], [Decimal(3), Decimal(2), Decimal(1)], Decimal(1))
        self.assertEqual(res["row_sum"], Decimal(1000))
        self.assertEqual([r["amount"] for r in res["rows"]], [Decimal(500), Decimal(333), Decimal(167)])

    def test_label_weight_mismatch(self):
        with self.assertRaises(bc.InputError):
            bc.split_amount(Decimal(100), ["a", "b"], [Decimal(1)], Decimal(1))


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bc.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes_and_csv(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(BASE, fh, ensure_ascii=False)
            out_csv = os.path.join(d, "out.csv")

            code, out, _ = self.run_cli(["allocate", src, "--csv", out_csv])
            self.assertEqual(code, 0)
            self.assertIn("校验通过", out)
            self.assertTrue(os.path.exists(out_csv))

            code, _, err = self.run_cli(["allocate", src, "--csv", out_csv])
            self.assertEqual(code, 2)  # 不覆盖已有文件
            self.assertIn("--overwrite", err)

            code, _, _ = self.run_cli(["allocate", src, "--csv", out_csv, "--overwrite"])
            self.assertEqual(code, 0)

            code, _, _ = self.run_cli(["allocate", os.path.join(d, "missing.json")])
            self.assertEqual(code, 2)

    def test_shortfall_exit_code_1(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(dict(BASE, total=4000), fh, ensure_ascii=False)
            code, out, _ = self.run_cli(["allocate", src])
            self.assertEqual(code, 1)
            self.assertIn("缺口", out)

    def test_json_format(self):
        code, out, _ = self.run_cli(["split", "--amount", "1000", "--labels", "a,b,c", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["row_sum"], "1000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
