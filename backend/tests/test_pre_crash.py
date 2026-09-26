"""「确认前崩溃（pre_crash）」状态机语义的回归测试。

被修复的缺陷：启用 pre_crash 后，写检查点前崩溃只产生一个与崩溃前 ACTIVE
状态业务字段完全相同的后继，状态指纹一致，导致该分支被有界 BFS 当作重复
状态丢弃——崩溃/恢复既不可区分、也无法参与去重、最短反例、单步回放与分叉。

修复后的语义：
  ACTIVE --crash_before_ack--> RECOVERING --recover_pre_ack--> ACTIVE（同一活动）

* RECOVERING 是显式的「恢复中」状态，进入状态指纹（不会与父状态合并）；
* recover_pre_ack 是确定性的显式恢复事件：不增加尝试次数、不产生外部效果；
* 恢复后重新枚举同一活动的全部交错：成功 / 失败 / 再次崩溃；
* 崩溃与恢复参与 BFS 指纹去重（崩溃循环有界收敛）、最短反例、单步回放与分叉。
"""
from __future__ import annotations

import unittest

from app import checker as C
from app import workflow as W


def _spec(activity_a: dict, *, custom: list[dict] | None = None) -> dict:
    return {
        "start": "a",
        "variables": [],
        "activities": [
            activity_a,
            {"id": "b", "name": "活动B", "effect": "effB",
             "can_fail": False, "retryable": False, "max_attempts": 1,
             "comp_can_fail": False},
        ],
        "edges": [{"src": "a", "dst": "b"}],
        "invariants": {
            "at_most_once": True,
            "undo_on_failure": True,
            "custom": custom or [],
        },
    }


def _crash_activity(**over) -> dict:
    a = {"id": "a", "name": "活动A", "effect": "effA", "pre_crash": True,
         "can_fail": False, "retryable": False, "max_attempts": 1,
         "comp_can_fail": False}
    a.update(over)
    return a


def _step(wf, state, sig):
    """按事件签名在当前状态取后继（透明穿过 ε）。"""
    return C._apply_event(wf, state, {"sig": sig})


def _walk(wf, sigs):
    state = W.initial_state(wf)
    for sig in sigs:
        state = _step(wf, state, sig)
    return state


def _event_types(result) -> list[str]:
    return [s["event"]["type"] for s in result["counterexample"]["steps"] if s["event"]]


class PreCrashStateSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.wf = W.compile_workflow(_spec(_crash_activity()))
        self.s0 = W.initial_state(self.wf)
        self.fp0 = W.fingerprint(self.s0)

    # -- 1. 可区分的恢复中状态 ------------------------------------------------

    def test_crash_enters_distinct_recovering_state(self):
        nxt, ev = self._successor(self.s0, W.EV_PRE_CRASH)
        self.assertEqual(ev["type"], W.EV_PRE_CRASH)
        self.assertEqual(nxt["status"]["a"], W.RECOVERING)
        # 关键回归：不能因与父状态业务字段相同而被指纹去重丢弃
        self.assertNotEqual(W.fingerprint(nxt), self.fp0)

    def test_recover_event_returns_to_same_activity_active(self):
        crashed, _ = self._successor(self.s0, W.EV_PRE_CRASH)
        recovered, ev = self._successor(crashed, W.EV_RECOVER)
        self.assertEqual(ev["type"], W.EV_RECOVER)
        self.assertEqual(recovered["current"], "a")
        self.assertEqual(recovered["status"]["a"], W.ACTIVE)

    def test_recover_neither_consumes_attempt_nor_produces_effect(self):
        crashed, _ = self._successor(self.s0, W.EV_PRE_CRASH)
        self.assertEqual(crashed["tries"]["a"], 0)
        self.assertEqual(crashed["effects"], [])
        recovered, _ = self._successor(crashed, W.EV_RECOVER)
        self.assertEqual(recovered["tries"]["a"], 0)          # 尝试次数不增加
        self.assertEqual(recovered["effects"], [])            # 无外部效果
        self.assertIsNone(recovered["ambiguous"])
        self.assertEqual(recovered["variables"], self.s0["variables"])

    def test_recovering_state_fingerprint_covers_recovery(self):
        crashed, _ = self._successor(self.s0, W.EV_PRE_CRASH)
        crashed_fp = W.fingerprint(crashed)
        # 恢复后回到与崩溃前等价的 ACTIVE 状态（指纹相同，正常去重），
        # 而崩溃本身是不同的指纹
        recovered, _ = self._successor(crashed, W.EV_RECOVER)
        self.assertEqual(W.fingerprint(recovered), self.fp0)
        self.assertNotEqual(crashed_fp, self.fp0)

    def test_recovery_reinvokes_same_interleavings(self):
        crashed, _ = self._successor(self.s0, W.EV_PRE_CRASH)
        recovered, _ = self._successor(crashed, W.EV_RECOVER)
        kinds = {ev["type"] for _, ev in W.successors(self.wf, recovered)}
        # 同一活动被重新调用：成功与再次崩溃都必须仍可枚举
        self.assertIn(W.EV_SUCCESS, kinds)
        self.assertIn(W.EV_PRE_CRASH, kinds)

    # -- 2. 成功 / 失败 / 再次崩溃交错 ----------------------------------------

    def test_success_after_recovery(self):
        s = _walk(self.wf, [["crash_before_ack", "a", 1],
                            ["recover_pre_ack", "a", 1],
                            ["success", "a", 1]])
        self.assertEqual(s["status"]["a"], W.DONE)
        self.assertEqual(s["tries"]["a"], 1)
        self.assertEqual(len(s["effects"]), 1)
        self.assertFalse(s["effects"][0]["undone"])

    def test_fatal_failure_after_recovery_leaves_no_phantom_effect(self):
        wf = W.compile_workflow(_spec(_crash_activity(
            can_fail=True, retryable=False, max_attempts=1)))
        s = _walk(wf, [["crash_before_ack", "a", 1],
                       ["recover_pre_ack", "a", 1],
                       ["fatal_error", "a", 1]])
        self.assertEqual(s["status"]["a"], W.FAILED)
        self.assertEqual(s["tries"]["a"], 1)
        # 崩溃前无持久痕迹：不得为补偿登记幽灵效果；无效果即干净补偿完成
        self.assertEqual(s["effects"], [])
        self.assertEqual(s["outcome"], "compensated")

    def test_repeated_crash_recovery_is_bounded_and_stateless(self):
        s = self.s0
        for _ in range(5):
            s = _step(self.wf, s, ["crash_before_ack", "a", 1])
            self.assertEqual(s["status"]["a"], W.RECOVERING)
            self.assertEqual(s["tries"]["a"], 0)
            self.assertEqual(s["effects"], [])
            s = _step(self.wf, s, ["recover_pre_ack", "a", 1])
            self.assertEqual(s["status"]["a"], W.ACTIVE)
            self.assertEqual(s["tries"]["a"], 0)
            self.assertEqual(s["effects"], [])
        # 恢复后的状态始终与初始等价
        self.assertEqual(W.fingerprint(s), self.fp0)

    def test_crash_interleaved_with_retryable_keeps_attempt_count(self):
        wf = W.compile_workflow(_spec(_crash_activity(
            can_fail=True, retryable=True, max_attempts=2)))
        # 第 1 次尝试可重试失败 -> 确定性重试 -> 第 2 次尝试前崩溃 -> 恢复
        s = _walk(wf, [["retryable_error", "a", 1],
                       ["retry", "a", 1],
                       ["crash_before_ack", "a", 2],
                       ["recover_pre_ack", "a", 2]])
        self.assertEqual(s["status"]["a"], W.ACTIVE)
        self.assertEqual(s["tries"]["a"], 1)          # 崩溃不额外消耗尝试
        alts = C._alternatives(wf, s)
        self.assertTrue(alts)
        self.assertTrue(all(e["attempt"] == 2 for e in alts))
        # 第 2 次仍可再次崩溃，且不能借此获得第 3 次尝试
        s = _step(wf, s, ["crash_before_ack", "a", 2])
        s = _step(wf, s, ["recover_pre_ack", "a", 2])
        self.assertEqual(s["tries"]["a"], 1)
        kinds = {e["type"] for e in C._alternatives(wf, s)}
        self.assertIn(W.EV_EXHAUSTED, kinds)
        self.assertNotIn(W.EV_RETRYABLE, kinds)

    def test_recover_sig_is_stable_for_replay_matching(self):
        crashed, _ = self._successor(self.s0, W.EV_PRE_CRASH)
        recovered, ev = self._successor(crashed, W.EV_RECOVER)
        # 事件签名包含类型/活动/尝试，重放按 sig 必须能精确匹配
        again = C._apply_event(self.wf, crashed, {"sig": ev["sig"]})
        self.assertEqual(W.fingerprint(again), W.fingerprint(recovered))

    # -- 3. BFS 指纹去重：崩溃恢复参与且有界收敛 ------------------------------

    def test_bfs_does_not_discard_recovering_state(self):
        result = C.run_check(self.wf, bound=8)
        # 修复前崩溃分支被去重丢弃，代表轨迹只会是 success；
        # 修复后按 sig 最小（crash < success）的代表路径必含崩溃与恢复
        types = _event_types(result)
        self.assertIn(W.EV_PRE_CRASH, types)
        self.assertIn(W.EV_RECOVER, types)
        self.assertGreaterEqual(result["stats"]["states_visited"], 2)

    def test_bfs_repeated_crash_loop_terminates_within_bound(self):
        result = C.run_check(self.wf, bound=12)
        # crash->recover 是指纹回到 ACTIVE 的自环，去重后必须在小边界内穷尽
        self.assertEqual(result["verdict"], "exhausted")
        self.assertFalse(result["stats"]["truncated"])
        # 反复崩溃不应膨胀状态数：active / recovering / a-done / b-active / 终态量级
        self.assertLessEqual(result["stats"]["states_visited"], 6)

    # -- 4. 最短反例 ----------------------------------------------------------

    def test_shortest_counterexample_does_not_include_crash_detour(self):
        # 自定义不变量：a 成功后才允许 b 生效；b 本身恒满足，
        # 这里用一个在「a 已成功推进到 b」时被破坏的不可满足不变量
        # succ("a") = 0 在 a 成功之后为假：最短反例是直接 success（1 步），
        # 任何 crash/recover 绕路都更长，BFS 必须给出长度 1 的反例。
        wf = W.compile_workflow(_spec(
            _crash_activity(can_fail=False),
            custom=[{"name": "a尚未成功", "expr": 'succ("a") = 0'}]))
        result = C.run_check(wf, bound=8)
        self.assertEqual(result["verdict"], "violated")
        types = _event_types(result)
        self.assertEqual(result["counterexample"]["length"], 1)
        self.assertEqual(types, [W.EV_SUCCESS])

    def test_violation_after_crash_recovery_is_reachable_as_counterexample(self):
        # 不变量在「a 已成功、b 尚未成功」的推进中间态为假。直接成功 1 步即违反，
        # 这里验证：分叉进崩溃后，BFS 仍以 RECOVERING 为根继续展开，
        # 恢复事件不会因根/父状态指纹问题丢失（崩溃态本身也违反该全局不变量，
        # 因此分叉反例的第一跳必须是 crash，且检验器正常给出反例而非丢弃分支）。
        wf = W.compile_workflow(_spec(
            _crash_activity(can_fail=False),
            custom=[{"name": "b必须已成功", "expr": 'succ("b") > 0'}]))
        result = C.run_check(wf, bound=8)
        self.assertEqual(result["verdict"], "violated")
        crash_sig = next(a["sig"] for a in result["counterexample"]["steps"][0]["alternatives"]
                         if a["type"] == W.EV_PRE_CRASH)
        forked = C.run_fork(wf, prefix=[], chosen_sig=crash_sig, bound=8)
        # 分叉根是 RECOVERING（succ("b")=0 同样违反），第一跳即崩溃事件，
        # 说明崩溃分支被检验器接纳为一等状态，而非静默丢弃。
        self.assertEqual(forked["verdict"], "violated")
        self.assertEqual(_event_types(forked), [W.EV_PRE_CRASH])
        # 根状态快照显式标注“恢复中”，其唯一备选是恢复事件
        root_snapshot = forked["counterexample"]["steps"][-1]["snapshot"]
        self.assertEqual(next(a for a in root_snapshot["activities"]
                              if a["id"] == "a")["status"], W.RECOVERING)
        self.assertEqual([a["type"] for a in forked["counterexample"]["steps"][-1]["alternatives"]],
                         [W.EV_RECOVER])

    # -- 5. 单步回放 ----------------------------------------------------------

    def test_replay_steps_expose_recovering_snapshot(self):
        result = C.run_check(self.wf, bound=8)
        steps = result["counterexample"]["steps"]
        crash_idx = next(i for i, s in enumerate(steps)
                         if s["event"] and s["event"]["type"] == W.EV_PRE_CRASH)
        snap = steps[crash_idx]["snapshot"]
        self.assertEqual(snap["current"], "a")
        act_a = next(x for x in snap["activities"] if x["id"] == "a")
        self.assertEqual(act_a["status"], W.RECOVERING)
        self.assertEqual(act_a["tries"], 0)
        # 下一步是确定性恢复事件
        self.assertEqual(steps[crash_idx + 1]["event"]["type"], W.EV_RECOVER)

    def test_replay_from_crash_node_offers_recovery_alternative(self):
        result = C.run_check(self.wf, bound=8)
        steps = result["counterexample"]["steps"]
        crash_idx = next(i for i, s in enumerate(steps)
                         if s["event"] and s["event"]["type"] == W.EV_PRE_CRASH)
        alts = steps[crash_idx]["alternatives"]
        self.assertEqual([a["type"] for a in alts], [W.EV_RECOVER])

    # -- 6. 从节点分叉 --------------------------------------------------------

    def test_fork_from_active_node_into_crash_then_recover_then_success(self):
        # 选一条直接成功的干净运行作为参照，再从初始节点分叉进崩溃路径
        wf = W.compile_workflow(_spec(_crash_activity(can_fail=False)))
        result = C.run_check(wf, bound=8)
        crash_sig = next(a["sig"] for a in result["counterexample"]["steps"][0]["alternatives"]
                         if a["type"] == W.EV_PRE_CRASH)
        forked = C.run_fork(wf, prefix=[], chosen_sig=crash_sig, bound=8)
        forked_steps = forked["counterexample"]["steps"]
        # 分叉点之后：crash -> recover -> success(a) -> success(b)
        types = [s["event"]["type"] for s in forked_steps if s["event"]]
        self.assertEqual(types,
                         [W.EV_PRE_CRASH, W.EV_RECOVER, W.EV_SUCCESS, W.EV_SUCCESS])
        self.assertEqual(forked_steps[-1]["snapshot"]["outcome"], "succeeded")
        # 共享前缀标记：第 0 步为前缀
        self.assertTrue(forked_steps[0]["shared_prefix"])

    def test_fork_after_crash_prefix_can_choose_success_on_reinvocation(self):
        wf = W.compile_workflow(_spec(_crash_activity(can_fail=False)))
        result = C.run_check(wf, bound=8)
        prefix = [s["event"] for s in result["counterexample"]["steps"][1:3]]  # crash, recover
        self.assertEqual([e["type"] for e in prefix],
                         [W.EV_PRE_CRASH, W.EV_RECOVER])
        forked = C.run_fork(wf, prefix=prefix, chosen_sig=[W.EV_SUCCESS, "a", 1], bound=8)
        types = [s["event"]["type"] for s in forked["counterexample"]["steps"]
                 if s["event"]]
        self.assertEqual(types,
                         [W.EV_PRE_CRASH, W.EV_RECOVER, W.EV_SUCCESS, W.EV_SUCCESS])

    # -- 7. 不启用 pre_crash 时无恢复状态/事件（回归保护） ---------------------

    def test_no_recovering_when_pre_crash_disabled(self):
        wf = W.compile_workflow(_spec(_crash_activity(pre_crash=False)))
        s0 = W.initial_state(wf)
        kinds = [ev["type"] for _, ev in W.successors(wf, s0)]
        self.assertNotIn(W.EV_PRE_CRASH, kinds)
        self.assertNotIn(W.EV_RECOVER, kinds)
        statuses = {s["status"]["a"] for s in (W.initial_state(wf),)}
        self.assertNotIn(W.RECOVERING, statuses)

    # -- helpers --------------------------------------------------------------

    def _successor(self, state, kind):
        for nxt, ev in W.successors(self.wf, state):
            if ev is not None and ev["type"] == kind:
                return nxt, ev
        raise AssertionError(f"successor {kind} not found")


if __name__ == "__main__":
    unittest.main()
