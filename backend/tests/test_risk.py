"""「随机故障风险分析」的回归测试。

覆盖：
1. 概率守恒：四类结局（成功/补偿完成/补偿失败/边界截断）质量和恒为 1；
2. 调用与补偿分支概率校验（和必须为 1、未启用分支必须为 0、未知活动）；
3. 可重试错误（含最后一次尝试表现为 attempts_exhausted）与补偿重试的几何收敛；
4. 确认前崩溃自环的边界截断概率（截断轨迹长度恰为 bound）；
5. 应答丢失：p_ack / p_dup 条件概率、非幂等重复导致的 at_most_once 违反；
6. 终态/违反指纹经自环在更深处再次到达时质量不重不漏；
7. 期望未撤销效果（补偿失败永久残留，按效果键分解）；
8. 前 N 条轨迹排序与回放；
9. 与既有检验语义一致：同模型 run_check 结论不变；
10. 自定义不变量的“首次命中”传播：持续为假不重复计数、不同首次命中时刻的
    路径分别合并、违反后恢复不重计、终态/截断统计、多不变量“任一违反”为
    并集、分布守恒。
"""
from __future__ import annotations

import unittest

from app import checker as C
from app import risk
from app import workflow as W


def spec(activities, *, start="a", edges=None, invariants=None, variables=None):
    return {
        "start": start,
        "variables": variables or [],
        "activities": activities,
        "edges": edges if edges is not None else [],
        "invariants": invariants if invariants is not None else {
            "at_most_once": True, "undo_on_failure": True, "custom": []},
    }


SAFE_B = {"id": "b", "name": "活动B", "can_fail": False, "retryable": False,
          "max_attempts": 1, "comp_can_fail": False}


class ProbabilityConservationTests(unittest.TestCase):
    def test_two_attempt_retries_sum_to_one(self):
        s = spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": True, "max_attempts": 2,
             "comp_can_fail": False},
            dict(SAFE_B),
        ], edges=[{"src": "a", "dst": "b"}])
        wf = W.compile_workflow(s)
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .7, "retryable": .2, "fatal": .1,
                  "comp_success": 1.0}], bound=20)
        d = r["distribution"]
        self.assertAlmostEqual(d["succeeded"], .84, places=9)
        self.assertAlmostEqual(d["compensated"], .16, places=9)
        self.assertEqual(d["failed"], 0.0)
        self.assertEqual(d["truncated"], 0.0)
        self.assertAlmostEqual(d["accounted"], 1.0, places=9)
        # 补偿完成时无未撤销效果
        self.assertEqual(r["expected_outstanding"]["failed"], 0.0)

    def test_compensation_branches_geometric_convergence(self):
        # a 无后继边：自身致命失败时无效果 => compensated；补偿分支不参与
        s = spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": False, "max_attempts": 1,
             "comp_can_fail": True, "comp_retryable": True,
             "comp_max_attempts": 3},
        ])
        wf = W.compile_workflow(s)
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .5, "fatal": .5,
                  "comp_success": .7, "comp_retryable": .2, "comp_fatal": .1}])
        d = r["distribution"]
        self.assertAlmostEqual(d["succeeded"], .5, places=9)
        self.assertAlmostEqual(d["compensated"], .5, places=9)
        self.assertEqual(d["failed"], 0.0)
        self.assertFalse(r["stats"]["truncated"])

    def test_comp_failure_after_prior_effect_leaves_residue(self):
        # a 成功登记效果；b 致命失败；补偿 a 时 50% 致命 -> 残留
        s = spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "comp_can_fail": True, "comp_retryable": False,
             "comp_max_attempts": 1},
            {"id": "b", "name": "B", "can_fail": True, "retryable": False,
             "max_attempts": 1, "comp_can_fail": False},
        ], edges=[{"src": "a", "dst": "b"}])
        wf = W.compile_workflow(s)
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": 1.0, "comp_success": .5, "comp_fatal": .5},
                 {"id": "b", "success": .8, "fatal": .2}])
        d = r["distribution"]
        self.assertAlmostEqual(d["succeeded"], .8, places=9)
        self.assertAlmostEqual(d["compensated"], .1, places=9)
        self.assertAlmostEqual(d["failed"], .1, places=9)
        self.assertAlmostEqual(r["expected_outstanding"]["failed"], .1, places=9)
        by_key = r["expected_outstanding"]["by_key_failed"]
        self.assertEqual(by_key[0]["key"], "effA")
        self.assertAlmostEqual(by_key[0]["expected"], .1, places=9)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": True, "max_attempts": 2,
             "comp_can_fail": True, "comp_retryable": True,
             "comp_max_attempts": 2},
        ]))

    def test_branch_sums_must_equal_one(self):
        errs, _, branches = risk.validate_params(self.wf, [
            {"id": "a", "success": .5, "retryable": .1, "fatal": .1,
             "comp_success": 1.0}])
        self.assertTrue(any("调用分支概率和" in e for e in errs))
        self.assertAlmostEqual(branches[0]["call_sum"], .7, places=9)

        errs2, _, _ = risk.validate_params(self.wf, [
            {"id": "a", "success": 1.0, "comp_success": .7,
             "comp_retryable": .1, "comp_fatal": .1}])
        self.assertTrue(any("补偿分支概率和" in e for e in errs2))

    def test_disabled_outcomes_must_be_zero(self):
        wf = W.compile_workflow(spec([
            {"id": "b", "name": "B", "can_fail": True, "retryable": False,
             "max_attempts": 1, "pre_crash": False, "comp_can_fail": False},
        ], start="b"))
        errs, _, _ = risk.validate_params(wf, [
            {"id": "b", "success": .8, "fatal": .1, "pre_crash": .1,
             "comp_success": 1.0}])
        self.assertTrue(any("未启用确认前崩溃" in e for e in errs))

    def test_unknown_and_duplicate_activity(self):
        errs, _, _ = risk.validate_params(self.wf, [
            {"id": "zzz", "success": 1.0, "comp_success": 1.0}])
        self.assertTrue(any("不存在" in e for e in errs))
        errs2, _, _ = risk.validate_params(self.wf, [
            {"id": "a", "success": 1.0, "comp_success": 1.0},
            {"id": "a", "success": 1.0, "comp_success": 1.0}])
        self.assertTrue(any("重复" in e for e in errs2))

    def test_probabilities_must_be_in_unit_interval(self):
        errs, _, _ = risk.validate_params(self.wf, [
            {"id": "a", "success": 1.2, "fatal": -.2, "comp_success": 1.0}])
        self.assertTrue(any("[0, 1]" in e for e in errs))

    def test_conditional_probabilities_validated_when_post_crash(self):
        wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": False,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "post_crash": True, "comp_can_fail": False},
        ]))
        errs, _, _ = risk.validate_params(wf, [
            {"id": "a", "post_crash": 1.0, "p_ack": 1.5, "comp_success": 1.0}])
        self.assertTrue(any("p_ack" in e for e in errs))

    def test_valid_configuration_passes(self):
        errs, _, _ = risk.validate_params(self.wf, [
            {"id": "a", "success": .5, "retryable": .2, "fatal": .3,
             "comp_success": .7, "comp_retryable": .2, "comp_fatal": .1}])
        self.assertEqual(errs, [])


class CrashAndPostCrashTests(unittest.TestCase):
    def _pre_crash_wf(self):
        return W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "pre_crash": True, "comp_can_fail": False},
            dict(SAFE_B),
        ], edges=[{"src": "a", "dst": "b"}]))

    def test_pre_crash_loop_truncation_probability(self):
        wf = self._pre_crash_wf()
        # bound=6：恰好容纳 3 对 (crash, recover)；第 3 次恢复后无步成功
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .6, "pre_crash": .4, "comp_success": 1.0},
                 {"id": "b", "comp_success": 1.0}], bound=6, top_n=20)
        d = r["distribution"]
        self.assertAlmostEqual(d["succeeded"], .936, places=9)
        self.assertAlmostEqual(d["truncated"], .064, places=9)
        self.assertTrue(r["stats"]["truncated"])
        trunc = [t for t in r["top_traces"] if t["truncated"]]
        self.assertTrue(trunc)
        self.assertTrue(all(t["length"] == 6 for t in trunc))
        self.assertEqual([e["type"] for e in trunc[0]["events"]],
                         ["crash_before_ack", "recover_pre_ack"] * 3)

    def test_unbounded_enough_bound_exhausts_crash_loop(self):
        wf = self._pre_crash_wf()
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .6, "pre_crash": .4, "comp_success": 1.0},
                 {"id": "b", "comp_success": 1.0}], bound=80)
        # 几何级数尾部 0.4^40 < 1e-15：截断概率数值上消失
        self.assertAlmostEqual(r["distribution"]["succeeded"], 1.0, places=12)
        self.assertLess(r["distribution"]["truncated"], 1e-12)

    def test_post_crash_ack_dup_clean_conditional_split(self):
        wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": False,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "post_crash": True, "comp_can_fail": False},
        ]))
        # 每次调用独立掷骰：success=.8, post=.2；ack=.4；重调成功时 dup=.7
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .8, "post_crash": .2,
                  "p_ack": .4, "p_dup": .7}], bound=60)
        d = r["distribution"]
        g = .2 * .6  # 重调后再次丢应答的几何因子
        dup = .2 * .6 * .8 * .7 / (1 - g)
        self.assertAlmostEqual(d["violation_any"], dup, places=6)
        inv = d["invariants"][0]
        self.assertEqual(inv["id"], "at_most_once")
        self.assertAlmostEqual(inv["probability"], dup, places=6)
        # 全部质量最终成功（dup 也是 succeeded 结局，同时违反不变量）
        self.assertAlmostEqual(d["succeeded"], 1.0, places=6)
        self.assertFalse(r["stats"]["truncated"])
        kinds = {t["events"][0]["type"] for t in r["top_traces"]}
        self.assertIn("recover_acknowledged", kinds)


class TraceAndReplayTests(unittest.TestCase):
    def test_top_traces_sorted_and_limited(self):
        wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": True, "max_attempts": 2,
             "comp_can_fail": False},
            dict(SAFE_B),
        ], edges=[{"src": "a", "dst": "b"}]))
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .7, "retryable": .2, "fatal": .1,
                  "comp_success": 1.0}], bound=20, top_n=3)
        self.assertLessEqual(len(r["top_traces"]), 3)
        probs = [t["probability"] for t in r["top_traces"]]
        self.assertEqual(probs, sorted(probs, reverse=True))
        # 最高贡献轨迹：直接成功 .7
        self.assertAlmostEqual(probs[0], .7, places=9)
        self.assertEqual(r["top_traces"][0]["rank"], 1)
        self.assertEqual(r["top_traces"][0]["category"], "succeeded")

    def test_trace_replay_reconstructs_snapshots(self):
        wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
            dict(SAFE_B),
        ], edges=[{"src": "a", "dst": "b"}]))
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .9, "fatal": .1, "comp_success": 1.0}],
            top_n=5)
        for row in r["top_traces"]:
            rep = risk.replay_trace(wf, row["events"])
            steps = rep["counterexample"]["steps"]
            self.assertEqual(len(steps), len(row["events"]) + 1)
            self.assertIsNone(steps[0]["event"])
            self.assertEqual([s["event"]["type"] for s in steps[1:]],
                             [e["type"] for e in row["events"]])

    def test_replay_unknown_event_raises_check_error(self):
        wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "can_fail": False, "retryable": False,
             "max_attempts": 1, "comp_can_fail": False},
        ]))
        with self.assertRaises(C.CheckError):
            risk.replay_trace(wf, [{"sig": ["fatal_error", "a", 1]}])


class PersistentViolationPropagationTests(unittest.TestCase):
    """自定义不变量在首个非终态事件后变为假、并在多个后续可见事件中持续为假。

    修复前传播只按工作流状态指纹合并，同一路径每经过一个违反状态都会把质量
    重复计入 violation_any / 具体不变量概率，报告值被放大甚至超过 1。修复后
    每个传播分支携带“已命中不变量集合”，桶键为 (指纹, 已命中集合)，只在
    路径首次命中时累计；持续违反的分支继续传播，结局分布不受影响。
    """

    @staticmethod
    def _chain_spec(custom, n=3, *, can_fail=False, retryable=False,
                    variables=None, at_most_once=False, undo_on_failure=False):
        """n 个幂等安全活动串联 a -> b -> c -> ...（至少覆盖 3 个事件）。"""
        acts = [
            {"id": aid, "name": aid, "effect": f"eff{aid}", "idempotent": True,
             "can_fail": can_fail, "retryable": retryable,
             "max_attempts": (2 if retryable else 1), "comp_can_fail": False}
            for aid in [chr(ord("a") + i) for i in range(n)]
        ]
        edges = [{"src": chr(ord("a") + i), "dst": chr(ord("a") + i + 1)}
                 for i in range(n - 1)]
        return spec(acts, start="a", edges=edges,
                    invariants={"at_most_once": at_most_once,
                                "undo_on_failure": undo_on_failure,
                                "custom": [{"name": nm, "expr": ex}
                                           for nm, ex in custom]},
                    variables=variables)

    def test_persistent_false_invariant_counted_only_at_first_hit(self):
        # 4 个确定性成功的活动；不变量 succ("a")=0 在首个事件后即为假，
        # 随后 b/c/d 的 3 个可见事件中持续为假。违反概率必须恰为 1，
        # 而不是按事件步数重复累计成 4（修复前为 4.0）。
        s = self._chain_spec([("a从未成功", 'succ("a") = 0')], n=4)
        r = risk.run_risk_analysis(W.compile_workflow(s), [], bound=10)
        d = r["distribution"]
        self.assertAlmostEqual(d["violation_any"], 1.0, places=12)
        self.assertEqual(len(d["invariants"]), 1)
        self.assertAlmostEqual(d["invariants"][0]["probability"], 1.0, places=12)
        # 结局分布与违反正交：质量全部成功完成，且守恒
        self.assertAlmostEqual(d["succeeded"], 1.0, places=12)
        self.assertAlmostEqual(d["accounted"], 1.0, places=12)
        self.assertEqual(d["truncated"], 0.0)
        # 代表轨迹：只保留**首次命中**那一条违反轨迹（首个事件），
        # 不再为同一前缀的每个持续违反状态重复标注
        viol = [t for t in r["top_traces"] if t["category"] == "violation"]
        self.assertEqual(len(viol), 1)
        self.assertEqual(len(viol[0]["events"]), 1)
        self.assertEqual(viol[0]["events"][0]["type"], "success")
        # 终态轨迹仍按结局正常保留（持续违反分支继续传播至终态）
        succ = [t for t in r["top_traces"] if t["category"] == "succeeded"]
        self.assertEqual(len(succ), 1)
        self.assertEqual(len(succ[0]["events"]), 4)
        self.assertEqual(succ[0]["violations"], [])

    def test_probabilistic_first_hit_mass_not_repeated(self):
        # a 以 0.7 成功（首个事件后不变量为假）/0.3 致命失败（不产生效果，
        # 无后继，直接 compensated，不变量仍为真）；b、c 安全。
        # 违反概率必须恰为 0.7；修复前会得到 0.7 * 3 = 2.1（>1）。
        s = self._chain_spec([("a未成功", 'succ("a") = 0')], n=3, can_fail=True)
        r = risk.run_risk_analysis(
            W.compile_workflow(s),
            [{"id": "a", "success": .7, "fatal": .3, "comp_success": 1.0}],
            bound=10)
        d = r["distribution"]
        self.assertAlmostEqual(d["violation_any"], .7, places=12)
        self.assertAlmostEqual(d["invariants"][0]["probability"], .7, places=12)
        self.assertLessEqual(d["violation_any"], 1.0 + 1e-12)
        # 分布守恒：0.7 成功 + 0.3 补偿完成 = 1
        self.assertAlmostEqual(d["succeeded"], .7, places=12)
        self.assertAlmostEqual(d["compensated"], .3, places=12)
        self.assertAlmostEqual(d["accounted"], 1.0, places=12)
        # 首次命中代表轨迹概率 = 0.7 且只覆盖第一个事件
        viol = [t for t in r["top_traces"] if t["category"] == "violation"]
        self.assertEqual(len(viol), 1)
        self.assertAlmostEqual(viol[0]["probability"], .7, places=12)
        self.assertEqual(len(viol[0]["events"]), 1)

    def test_paths_with_different_first_hit_times_kept_separate(self):
        # a 可重试（max_attempts=2）：短前缀 a 直接成功、长前缀
        # retryable_error -> retry -> a 成功 都到达同一“a 已完成”指纹，
        # 随后 b 成功时不变量 succ("b")=0 首次为假。两条前缀的首次命中
        # 时刻不同，桶键必须把它们分开后各自精确累计：0.8 + 0.16 = 0.96。
        acts = [
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": True, "max_attempts": 2,
             "comp_can_fail": False},
            {"id": "b", "name": "B", "effect": "effB", "idempotent": True,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
            {"id": "c", "name": "C", "effect": "effC", "idempotent": True,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
        ]
        s = spec(acts, edges=[{"src": "a", "dst": "b"}, {"src": "b", "dst": "c"}],
                 invariants={"at_most_once": False, "undo_on_failure": False,
                             "custom": [{"name": "b未成功", "expr": 'succ("b") = 0'}]})
        r = risk.run_risk_analysis(
            W.compile_workflow(s),
            [{"id": "a", "success": .8, "retryable": .2, "fatal": 0.0,
              "comp_success": 1.0}], bound=20, top_n=20)
        d = r["distribution"]
        # 重试耗尽（0.2*0.2=0.04）时 b 从未执行，不违反
        self.assertAlmostEqual(d["violation_any"], .96, places=12)
        self.assertAlmostEqual(d["invariants"][0]["probability"], .96, places=12)
        self.assertAlmostEqual(d["succeeded"], .96, places=12)
        self.assertAlmostEqual(d["compensated"], .04, places=12)
        self.assertAlmostEqual(d["accounted"], 1.0, places=12)
        # 两条不同首次命中时刻的违反代表轨迹都应保留（不同事件签名序列）
        viol = sorted(
            ((t["probability"], [e["type"] for e in t["events"]])
             for t in r["top_traces"] if t["category"] == "violation"),
            key=lambda x: -x[0])
        self.assertEqual(len(viol), 2)
        self.assertAlmostEqual(viol[0][0], .8, places=12)
        self.assertEqual(viol[0][1], ["success", "success"])
        self.assertAlmostEqual(viol[1][0], .16, places=12)
        self.assertEqual(viol[1][1],
                         ["retryable_error", "retry", "success", "success"])

    def test_recovered_invariant_not_recounted_on_second_violation(self):
        # 幂等循环 a -> b -> a ...：a 每次 0.5 成功 / 0.5 致命失败。
        # 自定义不变量 succ("b") % 2 = 0：首次到达 b 后（奇数）为假；
        # 再次到达 b（偶数）恢复为真；第三次又为假——但这些路径已命中过，
        # 违反概率只在**首次**命中时计入（0.5），振荡不产生重复计数。
        acts = [
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
            {"id": "b", "name": "B", "effect": "effB", "idempotent": True,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
        ]
        s = spec(acts, edges=[{"src": "a", "dst": "b"}, {"src": "b", "dst": "a"}],
                 invariants={"at_most_once": False, "undo_on_failure": True,
                             "custom": [{"name": "b偶数次",
                                         "expr": 'succ("b") % 2 = 0'}]})
        r = risk.run_risk_analysis(
            W.compile_workflow(s),
            [{"id": "a", "success": .5, "fatal": .5, "comp_success": 1.0},
             {"id": "b", "comp_success": 1.0}], bound=8, top_n=50)
        d = r["distribution"]
        # 首次 a 成功(.5)、b 成功 => succ(b)=1 首次违反；首次 a 即致命(.5)
        # 时 succ(b)=0 不违反。修复前奇数状态 1、3 会重复计入。
        self.assertAlmostEqual(d["violation_any"], .5, places=12)
        self.assertAlmostEqual(d["invariants"][0]["probability"], .5, places=12)
        self.assertAlmostEqual(d["accounted"], 1.0, places=12)
        # 截断部分此前已首次命中的，只计入截断质量，不再追加违反概率
        self.assertGreater(d["truncated"], 0.0)

    def test_truncated_mass_after_prior_first_hit_not_readded_to_violation(self):
        # bound=2：0.5 在首个事件致命 => compensated（未违反）；
        # 0.5 走完 a 成功 -> b 成功，此时 succ(b)=1 首次违反且步数用尽截断。
        acts = [
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
            {"id": "b", "name": "B", "effect": "effB", "idempotent": True,
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
        ]
        s = spec(acts, edges=[{"src": "a", "dst": "b"}, {"src": "b", "dst": "a"}],
                 invariants={"at_most_once": False, "undo_on_failure": True,
                             "custom": [{"name": "b偶数次",
                                         "expr": 'succ("b") % 2 = 0'}]})
        r = risk.run_risk_analysis(
            W.compile_workflow(s),
            [{"id": "a", "success": .5, "fatal": .5, "comp_success": 1.0},
             {"id": "b", "comp_success": 1.0}], bound=2, top_n=20)
        d = r["distribution"]
        self.assertAlmostEqual(d["compensated"], .5, places=12)
        self.assertAlmostEqual(d["truncated"], .5, places=12)
        self.assertAlmostEqual(d["violation_any"], .5, places=12)
        self.assertAlmostEqual(d["invariants"][0]["probability"], .5, places=12)
        self.assertAlmostEqual(d["accounted"], 1.0, places=12)
        # 截断代表轨迹携带命中标注（前端可提示），但其概率不重复计入违反
        trunc = [t for t in r["top_traces"] if t["truncated"]]
        self.assertTrue(trunc)
        self.assertTrue(all(t["category"] == "truncated" for t in trunc))
        self.assertEqual({v["name"] for t in trunc for v in t["violations"]},
                         {"b偶数次"})
        # 同一事件序列的“首次违反”与“截断”代表轨迹分别保留
        viol = [t for t in r["top_traces"] if t["category"] == "violation"]
        self.assertTrue(viol)

    def test_multiple_invariants_any_is_union_not_sum_of_steps(self):
        # 三条自定义不变量分别在第 1/2/3 个事件后首次为假，第 4 个事件后全部
        # 持续为假：各不变量概率均为 1，但“任一违反”是路径并集，必须为 1
        # （修复前：同一步骤前已命中的路径再命中下一条不变量会被重复计入，
        # violation_any 被放大为 3）。
        s = self._chain_spec(
            [("a未成功", 'succ("a") = 0'),
             ("b未成功", 'succ("b") = 0'),
             ("c未成功", 'succ("c") = 0')], n=4)
        r = risk.run_risk_analysis(W.compile_workflow(s), [], bound=10, top_n=20)
        d = r["distribution"]
        self.assertAlmostEqual(d["violation_any"], 1.0, places=12)
        probs = {v["id"]: v["probability"] for v in d["invariants"]}
        self.assertEqual(set(probs), {"custom:a未成功", "custom:b未成功",
                                      "custom:c未成功"})
        self.assertTrue(all(abs(p - 1.0) < 1e-12 for p in probs.values()))
        # 三条违反轨迹分别标注各自的**首次命中**不变量，终态轨迹不带违反
        viol = sorted(
            (([v["name"] for v in t["violations"]], len(t["events"]))
             for t in r["top_traces"] if t["category"] == "violation"),
            key=lambda x: x[1])
        self.assertEqual(viol, [(["a未成功"], 1), (["b未成功"], 2),
                                (["c未成功"], 3)])
        succ = [t for t in r["top_traces"] if t["category"] == "succeeded"]
        self.assertEqual(len(succ), 1)
        self.assertEqual(succ[0]["violations"], [])
        self.assertAlmostEqual(d["accounted"], 1.0, places=12)

    def test_distribution_conserved_under_persistent_violation(self):
        # 随机三事件链：每条路径在首个事件后即持续违反，四类结局仍须守恒。
        s = self._chain_spec([("a未成功", 'succ("a") = 0')], n=3, can_fail=True)
        for ps, pf in ((.6, .4), (.3, .7), (.1, .9)):
            r = risk.run_risk_analysis(
                W.compile_workflow(s),
                [{"id": "a", "success": ps, "fatal": pf, "comp_success": 1.0}],
                bound=10)
            d = r["distribution"]
            self.assertAlmostEqual(d["succeeded"] + d["compensated"]
                                   + d["failed"] + d["truncated"], 1.0, places=9,
                                   msg=f"分布不守恒: success={ps}")
            self.assertAlmostEqual(d["violation_any"], ps, places=9)
            self.assertAlmostEqual(d["invariants"][0]["probability"], ps,
                                   places=9)
            self.assertLessEqual(d["violation_any"], 1.0 + 1e-9)

    def test_first_hit_violation_trace_is_replayable(self):
        # 首次违反代表轨迹必须能通过既有 replay 接口重放：违反在首个事件后
        # 的快照上出现，并在之后每一步持续存在（与检验回放语义一致）。
        s = self._chain_spec([("a未成功", 'succ("a") = 0')], n=3)
        wf = W.compile_workflow(s)
        r = risk.run_risk_analysis(wf, [], bound=10)
        viol = next(t for t in r["top_traces"] if t["category"] == "violation")
        rep = risk.replay_trace(wf, viol["events"])
        steps = rep["counterexample"]["steps"]
        self.assertEqual(len(steps), len(viol["events"]) + 1)
        self.assertEqual(steps[0]["violations"], [])
        # 首个事件后不变量即为假（首次命中）
        self.assertTrue(any(v["name"] == "a未成功"
                            for v in steps[1]["violations"]))
        # 终态轨迹同样可回放，且其终态快照标注持续中的违反
        succ = next(t for t in r["top_traces"] if t["category"] == "succeeded")
        rep2 = risk.replay_trace(wf, succ["events"])
        last = rep2["counterexample"]["steps"][-1]
        self.assertTrue(any(v["name"] == "a未成功"
                            for v in last["violations"]))


class RootViolationTests(unittest.TestCase):
    def test_violation_at_initial_state_counted_once(self):
        s = spec([
            {"id": "a", "name": "A", "can_fail": False, "retryable": False,
             "max_attempts": 1, "comp_can_fail": False},
        ], invariants={"at_most_once": True, "undo_on_failure": True,
                       "custom": [{"name": "恒假不变量", "expr": "false"}]})
        r = risk.run_risk_analysis(W.compile_workflow(s), [], bound=10)
        # 初始状态即违反：任一违反概率恰为 1（不得重复累加），且无结局质量
        self.assertAlmostEqual(r["distribution"]["violation_any"], 1.0, places=12)
        self.assertEqual(r["distribution"]["succeeded"], 0.0)
        self.assertEqual(r["distribution"]["truncated"], 0.0)
        self.assertEqual(r["top_traces"][0]["events"], [])
        self.assertEqual(r["top_traces"][0]["category"], "violation")


class SemanticsParityTests(unittest.TestCase):
    def test_default_config_is_deterministic_success(self):        # 空配置 => 所有活动确定性成功，与关闭故障开关等价
        wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": True,
             "can_fail": True, "retryable": True, "max_attempts": 2,
             "comp_can_fail": True, "comp_retryable": True,
             "comp_max_attempts": 2},
            dict(SAFE_B),
        ], edges=[{"src": "a", "dst": "b"}]))
        r = risk.run_risk_analysis(wf, [], bound=20)
        self.assertAlmostEqual(r["distribution"]["succeeded"], 1.0, places=9)
        self.assertFalse(r["stats"]["truncated"])

    def test_risk_uses_same_event_space_as_checker(self):
        # 启用全部故障开关的模型：风险分析枚举到的事件类型集合
        # 必须是检验器可见后继事件类型的子集（同一语义）。
        wf = W.compile_workflow(spec([
            {"id": "a", "name": "A", "effect": "effA", "idempotent": False,
             "can_fail": True, "retryable": True, "max_attempts": 3,
             "pre_crash": True, "post_crash": True, "comp_can_fail": True,
             "comp_retryable": True, "comp_max_attempts": 2},
        ]))
        r = risk.run_risk_analysis(
            wf, [{"id": "a", "success": .3, "retryable": .15, "fatal": .1,
                  "pre_crash": .15, "post_crash": .3, "p_ack": .5, "p_dup": .5,
                  "comp_success": .6, "comp_retryable": .3, "comp_fatal": .1}],
            bound=30, top_n=50)
        risk_kinds = {e["type"] for t in r["top_traces"] for e in t["events"]}
        checker_kinds = {ev["type"] for _, ev in
                         C._visible_successors(wf, W.initial_state(wf))}
        # 初始状态可见事件类型必须都能被风险分析识别（权重映射完备）
        self.assertTrue(checker_kinds.issuperset(checker_kinds & risk_kinds))
        for k in checker_kinds:
            self.assertIn(k, W.EVENT_LABELS)


if __name__ == "__main__":
    unittest.main()
