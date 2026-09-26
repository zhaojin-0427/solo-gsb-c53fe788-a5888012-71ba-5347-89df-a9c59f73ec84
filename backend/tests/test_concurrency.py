"""「并发调度安全分析」模块的回归测试。

覆盖：
1. fork/join 汇合：分支部分序交错全部枚举、按线程与汇合令牌指纹合并、
   汇合后续跑线程执行 join 活动、无问题时穷尽到 succeeded；
2. 数据竞态：向量时钟识别不同线程上无 HB 关系的写-写/读-写访问，
   同锁保护与 fork/join HB 关系下不误报，读读不冲突，见证最短且可重放；
3. 锁顺序死锁：相反加锁顺序形成等待环，给出阻塞环、涉及锁与最短见证；
4. 无法汇合的分支：分支在到达 join 前正常结束时在该转移上封存终态并给出
   「无法汇合」见证（任意步数边界均可检出、最短可重放、分叉复现同一判定）；
   步数不足导致汇合组暂未闭合只标记边界截断，不得误报为无法汇合；
5. 并发语义下补偿/崩溃恢复/不变量：分支失败释放全部分支锁并全局 LIFO 补偿，
   自定义不变量在并发状态上求值，重复生效违反仍可检出；
6. 调度回放与从见证节点分叉：事件序列可逐步重放，改选另一分支线程后
   仍能重新发现问题；稳定分支 tid 不随调度顺序变化；
7. 静态校验：fork 出边不足、join 不可达、区域嵌套/成环、未知资源/锁等；
8. 兼容性：无并发声明的模型拒绝并发分析但既有检验/风险 API 不受影响。
"""
from __future__ import annotations

import os
import unittest

from app import concurrency as CC
from app import workflow as W


def _spec(activities, *, edges=None, resources=(), locks=(), groups=(),
          variables=None, invariants=None, start=None):
    return {
        "start": start if start is not None else activities[0]["id"],
        "variables": variables or [],
        "activities": activities,
        "edges": edges if edges is not None else [],
        "invariants": invariants if invariants is not None else {
            "at_most_once": True, "undo_on_failure": True, "custom": []},
        "concurrency": {
            "resources": [{"name": r} for r in resources],
            "locks": [{"name": lk} for lk in locks],
            "groups": [{"id": gid, "fork": fk, "join": jn}
                       for gid, fk, jn in groups],
        },
    }


def _safe_act(aid, **over):
    a = {"id": aid, "name": aid, "can_fail": False, "retryable": False,
         "max_attempts": 1, "comp_can_fail": False}
    a.update(over)
    return a


def _fork_join_spec(**branch_kw):
    """f fork -> a/b -> j(join)。branch_kw 给 a/b 加并发注解。"""
    acts = [
        _safe_act("f"),
        _safe_act("a", effect=branch_kw.pop("a_effect", None)),
        _safe_act("b", effect=branch_kw.pop("b_effect", None)),
        _safe_act("j"),
        _safe_act("end"),
    ]
    for a in acts:
        if a.get("effect") is None:
            a.pop("effect", None)
    a_kw = branch_kw.get("a", {})
    b_kw = branch_kw.get("b", {})
    acts[1].update(a_kw)
    acts[2].update(b_kw)
    edges = [{"src": "f", "dst": "a"}, {"src": "f", "dst": "b"},
             {"src": "a", "dst": "j"}, {"src": "b", "dst": "j"},
             {"src": "j", "dst": "end"}]
    return _spec(
        acts, edges=edges, start="f",
        resources=branch_kw.get("resources", ()),
        locks=branch_kw.get("locks", ()),
        groups=[("g1", "f", "j")],
        invariants=branch_kw.get("invariants"))


def _events(result) -> list[dict]:
    return [s["event"] for s in result["counterexample"]["steps"] if s["event"]]


def _event_types(result) -> list[str]:
    return [e["type"] for e in _events(result)]


def _threads_at(steps, index) -> dict:
    return {t["tid"]: t for t in steps[index]["snapshot"]["threads"]}


class ForkJoinEnumerationTests(unittest.TestCase):
    def setUp(self):
        spec = _fork_join_spec()
        self.wf = W.compile_workflow(spec)

    def test_clean_fork_join_exhausts_to_succeeded(self):
        r = CC.run_check(self.wf, bound=30)
        self.assertEqual(r["verdict"], "exhausted")
        self.assertEqual(r["findings"], {})
        self.assertFalse(r["stats"]["truncated"])
        # 代表轨迹以 succeeded 结束
        self.assertEqual(r["counterexample"]["steps"][-1]["snapshot"]["outcome"],
                         "succeeded")

    def test_branch_interleavings_are_enumerated(self):
        # 两条调度顺序（先 a 后 b / 先 b 后 a）都必须可枚举：
        # 从 fork 后状态取备选，两个 thread_schedule 同时存在
        r = CC.run_check(self.wf, bound=30)
        after_fork = r["counterexample"]["steps"][1]
        tids = {a["thread"] for a in after_fork["alternatives"]
                if a["type"] == CC.EV_SCHEDULE}
        self.assertEqual(tids, {"b_g1_00", "b_g1_01"})
        # 状态空间 > 单一线程序列：交错确实枚举了
        self.assertGreater(r["stats"]["states_visited"], 8)

    def test_join_tokens_fire_once_then_continue_runs_join(self):
        events = _events(CC.run_check(self.wf, bound=30))
        types = [e["type"] for e in events]
        # fork 的成功事件同时带有 fork_branches 标注
        self.assertTrue(any(e.get("fork_group") == "g1" for e in events))
        self.assertIn(CC.EV_JOIN_ARRIVE, types)
        self.assertIn(CC.EV_JOIN_FIRE, types)
        # join 只执行一次：续跑线程 t0001 的一次 success
        join_success = [e for e in events
                        if e["type"] == W.EV_SUCCESS and e["activity"] == "j"]
        self.assertEqual(len(join_success), 1)
        # j 之后还有 end
        end_success = [e for e in events
                       if e["type"] == W.EV_SUCCESS and e["activity"] == "end"]
        self.assertEqual(len(end_success), 1)

    def test_branch_tids_are_stable_across_schedule_orders(self):
        r = CC.run_check(self.wf, bound=30)
        after_fork = r["counterexample"]["steps"][1]
        # 分叉选择 b_g1_01（先调度第二分支），到达状态里第一分支仍叫 b_g1_00
        sig = next(a["sig"] for a in after_fork["alternatives"]
                   if a["thread"] == "b_g1_01")
        fk = CC.run_fork(self.wf, [after_fork["event"]], sig, bound=30)
        snap = fk["counterexample"]["steps"][-1]["snapshot"]
        tids = {t["tid"] for t in snap["threads"]}
        self.assertIn("b_g1_00", tids)
        self.assertIn("b_g1_01", tids)

    def test_fingerprint_merges_same_thread_token_state(self):
        # 无锁、无故障：大量调度交错但指纹合并后状态数有界（远小于全部排列）
        r = CC.run_check(self.wf, bound=30)
        self.assertLess(r["stats"]["states_visited"], 40)

    def test_snapshot_exposes_threads_locks_and_join_tokens(self):
        r = CC.run_check(self.wf, bound=30)
        snap0 = r["initial_snapshot"]
        self.assertTrue(snap0["concurrent"])
        self.assertEqual([t["tid"] for t in snap0["threads"]], ["t0000"])
        fork_step = next(s for s in r["counterexample"]["steps"]
                         if s["event"] and s["event"].get("fork_group") == "g1")
        self.assertIn("g1", [g["group"] for g in fork_step["snapshot"]["joins"]])


class DataRaceTests(unittest.TestCase):
    def test_write_write_race_is_detected_with_shortest_witness(self):
        wf = W.compile_workflow(_fork_join_spec(
            resources=("x",),
            a={"writes": ["x"]}, b={"writes": ["x"]}))
        r = CC.run_check(wf, bound=30)
        self.assertEqual(r["primary_kind"], CC.FIND_RACE)
        w = r["findings"][CC.FIND_RACE]
        self.assertEqual(w["resource"], "x")
        tids = {h["tid"] for h in w["accesses"]}
        self.assertEqual(tids, {"b_g1_00", "b_g1_01"})
        # 最短调度：fork(1) + 调度a(1) + a成功(1) + 调度b(1) + b成功竞态(1)
        self.assertEqual(w["length"], 5)
        # 见证最后一个事件带竞态标注
        self.assertEqual(_events(r)[-1].get("race", {}).get("resource"), "x")

    def test_read_write_race_is_detected(self):
        wf = W.compile_workflow(_fork_join_spec(
            resources=("x",),
            a={"writes": ["x"]}, b={"reads": ["x"]}))
        r = CC.run_check(wf, bound=30)
        self.assertIn(CC.FIND_RACE, r["findings"])

    def test_read_read_is_not_a_race(self):
        wf = W.compile_workflow(_fork_join_spec(
            resources=("x",),
            a={"reads": ["x"]}, b={"reads": ["x"]}))
        r = CC.run_check(wf, bound=30)
        self.assertEqual(r["verdict"], "exhausted")
        self.assertNotIn(CC.FIND_RACE, r["findings"])

    def test_common_lock_suppresses_race(self):
        wf = W.compile_workflow(_fork_join_spec(
            resources=("x",), locks=("L",),
            a={"writes": ["x"], "locks": ["L"]},
            b={"writes": ["x"], "locks": ["L"]}))
        r = CC.run_check(wf, bound=40)
        self.assertNotIn(CC.FIND_RACE, r["findings"])
        self.assertEqual(r["verdict"], "exhausted")

    def test_join_happens_before_makes_post_join_access_safe(self):
        # 分支写 x 后汇合；join 之后的 end 再读 x：join_fire 已合并时钟（HB），
        # 不应报竞态。
        wf = W.compile_workflow(_fork_join_spec(
            resources=("x",),
            a={"writes": ["x"]}, b={"reads": ["x"]}))  # 分支间确实有竞态
        r = CC.run_check(wf, bound=30)
        w = r["findings"][CC.FIND_RACE]
        # 竞态只涉及两个分支线程，不涉及汇合后的 t0001
        self.assertTrue(all(h["tid"].startswith("b_g1") for h in w["accesses"]))

    def test_race_witness_replays_and_forks(self):
        wf = W.compile_workflow(_fork_join_spec(
            resources=("x",),
            a={"writes": ["x"]}, b={"writes": ["x"]}))
        r = CC.run_check(wf, bound=30)
        evs = _events(r)
        rep = CC.replay(wf, evs)
        self.assertEqual(rep["counterexample"]["length"], len(evs))
        self.assertEqual(rep["counterexample"]["steps"][-1]["event"]
                         ["race"]["resource"], "x")
        # 从 fork 后改选先调度 b：仍能发现竞态
        after_fork = r["counterexample"]["steps"][1]
        sig = next(a["sig"] for a in after_fork["alternatives"]
                   if a["thread"] == "b_g1_01")
        fk = CC.run_fork(wf, [after_fork["event"]], sig, bound=30)
        self.assertEqual(fk["verdict"], "conc_unsafe")
        self.assertIn(CC.FIND_RACE, fk["findings"])


class DeadlockTests(unittest.TestCase):
    def _wf(self):
        return W.compile_workflow(_fork_join_spec(
            locks=("L1", "L2"),
            a={"locks": ["L1", "L2"]}, b={"locks": ["L2", "L1"]}))

    def test_lock_cycle_deadlock_is_detected(self):
        r = CC.run_check(self._wf(), bound=40)
        self.assertIn(CC.FIND_DEADLOCK, r["findings"])
        w = r["findings"][CC.FIND_DEADLOCK]
        cycle_tids = {x["tid"] for x in w["cycle"]}
        self.assertEqual(cycle_tids, {"b_g1_00", "b_g1_01"})
        self.assertEqual(set(w["locks"]), {"L1", "L2"})
        # 阻塞原因逐线程给出
        reasons = {b["tid"]: b["reason"] for b in w["blocking"]}
        self.assertIn("等待锁", reasons["b_g1_00"])
        self.assertIn("等待锁", reasons["b_g1_01"])

    def test_deadlock_witness_ends_in_deadlocked_state(self):
        r = CC.run_check(self._wf(), bound=40)
        w = r["findings"][CC.FIND_DEADLOCK]
        self.assertEqual(w["steps"][-1]["snapshot"]["outcome"], "deadlocked")
        types = [s["event"]["type"] for s in w["steps"] if s["event"]]
        self.assertEqual(types[-1], CC.EV_LOCK_WAIT)
        self.assertEqual(types.count(CC.EV_LOCK_WAIT), 2)
        self.assertEqual(types.count(CC.EV_LOCK_ACQUIRED), 2)

    def test_deadlock_witness_is_replayable(self):
        wf = self._wf()
        r = CC.run_check(wf, bound=40)
        evs = _events(r)
        rep = CC.replay(wf, evs)
        self.assertEqual(rep["counterexample"]["steps"][-1]["snapshot"]["outcome"],
                         "deadlocked")

    def test_same_lock_order_does_not_deadlock(self):
        wf = W.compile_workflow(_fork_join_spec(
            locks=("L1", "L2"),
            a={"locks": ["L1", "L2"]}, b={"locks": ["L1", "L2"]}))
        r = CC.run_check(wf, bound=40)
        self.assertNotIn(CC.FIND_DEADLOCK, r["findings"])


class UnjoinableTests(unittest.TestCase):
    @staticmethod
    def _early_end_spec():
        """f fork -> a -> j(join) / b -> c（c 在区域内无出边）；j -> end。

        b 分支在到达 join 前正常结束（c 无出边），汇合组 g1 结构上永不闭合；
        join 对另一分支 a 仍可达，因此静态校验放行，问题在运行期暴露。
        """
        acts = [_safe_act("f"), _safe_act("a"), _safe_act("b"),
                _safe_act("c"), _safe_act("j"), _safe_act("end")]
        edges = [{"src": "f", "dst": "a"}, {"src": "f", "dst": "b"},
                 {"src": "a", "dst": "j"}, {"src": "b", "dst": "c"},
                 {"src": "j", "dst": "end"}]
        return W.compile_workflow(_spec(acts, edges=edges, start="f",
                                       groups=[("g1", "f", "j")]))

    def test_branch_ending_before_join_is_unjoinable_at_any_bound(self):
        # 漏报回归：分支在到达 join 前正常结束，足够大的步数边界下旧实现
        # 把「无存活线程且仍欠汇合令牌」的终态当作无问题终态而报 exhausted。
        wf = self._early_end_spec()
        for bound in (6, 24, 60):
            r = CC.run_check(wf, bound=bound)
            self.assertEqual(r["verdict"], "conc_unsafe", bound)
            self.assertIn(CC.FIND_UNJOINABLE, r["findings"], bound)
            self.assertTrue(r["findings"][CC.FIND_UNJOINABLE]["terminal"], bound)
        r = CC.run_check(wf, bound=60)
        w = r["findings"][CC.FIND_UNJOINABLE]
        # 最短调度：fork 成功 → 调度 b → b 成功 → 调度 c → c 成功（提前结束）
        self.assertTrue(w["terminal"])
        self.assertEqual(w["length"], 5)
        types = [s["event"]["type"] for s in w["steps"] if s["event"]]
        self.assertEqual(types[0], W.EV_SUCCESS)       # f 成功即 fork
        self.assertEqual(types[-1], CC.EV_BRANCH_ENDED)
        last = w["steps"][-1]
        self.assertEqual(last["event"]["thread"], "b_g1_01")
        self.assertEqual(last["event"]["join_group"], "g1")
        self.assertEqual(last["snapshot"]["outcome"], CC.FIND_UNJOINABLE)
        last_threads = {t["tid"]: t for t in last["snapshot"]["threads"]}
        self.assertEqual(last_threads["b_g1_01"]["status"], CC.T_ENDED)
        self.assertEqual(last_threads["b_g1_01"]["status_label"],
                         "汇合前已结束")
        g = w["groups"][0]
        self.assertEqual(g["group"], "g1")
        self.assertEqual(g["needed"], 2)
        stopped = [b for b in g["branches"] if b["tid"] == "b_g1_01"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["activity"], "c")
        self.assertIn("未到达汇合点", w["detail"])
        # 终态统计：终态结局为 unjoinable，不存在被静默吞掉的 stuck 终态
        self.assertGreaterEqual(r["stats"]["terminal_states"], 1)
        self.assertIn(CC.FIND_UNJOINABLE, r["stats"]["terminal_outcomes"])
        self.assertNotIn("stuck", r["stats"]["terminal_outcomes"])

    def test_unjoinable_witness_replays_and_forks_same_verdict(self):
        # 从见证节点分叉必须复现相同判定（提前结束事件是显式、可匹配的转移）
        wf = self._early_end_spec()
        r = CC.run_check(wf, bound=60)
        evs = _events(r)
        rep = CC.replay(wf, evs)
        self.assertEqual(rep["counterexample"]["steps"][-1]["snapshot"]["outcome"],
                         CC.FIND_UNJOINABLE)
        fk = CC.run_fork(wf, evs[:-1], evs[-1]["sig"], bound=60)
        self.assertEqual(fk["verdict"], "conc_unsafe")
        self.assertIn(CC.FIND_UNJOINABLE, fk["findings"])
        self.assertTrue(fk["findings"][CC.FIND_UNJOINABLE]["terminal"])

    def test_pending_join_at_small_bound_is_only_truncation(self):
        # 相反回归（不得误报）：步数不足导致汇合组暂未闭合，只标记边界截断，
        # 不生成无法汇合见证。
        wf = W.compile_workflow(_fork_join_spec(
            b={"can_fail": True, "retryable": True, "max_attempts": 100}))
        r = CC.run_check(wf, bound=5)
        self.assertEqual(r["verdict"], "bound_reached")
        self.assertTrue(r["stats"]["truncated"])
        self.assertNotIn(CC.FIND_UNJOINABLE, r["findings"])
        self.assertEqual(r["findings"], {})
        self.assertIn("g1", r["stats"]["pending_groups_at_bound"])
        self.assertIn("截断", r["verdict_text"])

    def test_terminated_execution_is_not_unjoinable(self):
        # 足够大的边界：分支总能成功或失败，不应再报无法汇合
        wf = W.compile_workflow(_fork_join_spec(
            b={"can_fail": True, "retryable": True, "max_attempts": 3}))
        r = CC.run_check(wf, bound=60)
        self.assertNotIn(CC.FIND_UNJOINABLE, r["findings"])


class ConcurrencyCompensationTests(unittest.TestCase):
    def test_branch_failure_releases_all_locks_and_compensates(self):
        wf = W.compile_workflow(_fork_join_spec(
            a_effect="effA", locks=("L",),
            a={"locks": ["L"]},
            b={"can_fail": True, "retryable": False}))
        r = CC.run_check(wf, bound=40)
        # 存在一条“a 先成功持锁后释放 → b 致命失败 → 补偿 effA”的轨迹
        # （主见证可能是其他类别，这里直接构造该轨迹）
        st = CC.c_initial_state(wf)
        st = CC.apply_event(wf, st, {"sig": [W.EV_SUCCESS, "t0000", "f", 1]})
        st = CC.apply_event(wf, st, {"sig": [CC.EV_SCHEDULE, "b_g1_00", "a"]})
        st = CC.apply_event(wf, st, {"sig": [CC.EV_LOCK_ACQUIRED, "b_g1_00", "a"]})
        # a 成功且其出边即汇合点 j：成功与“到达 join”是同一个原子事件，
        # 成功登记效果、释放锁后交出汇合令牌
        st = CC.apply_event(wf, st, {"sig": [CC.EV_JOIN_ARRIVE, "b_g1_00", "g1"]})
        self.assertIsNone(st["locks"]["L"])           # a 成功后已释放锁
        self.assertTrue(any(e["key"] == "effA" and not e["undone"]
                            for e in st["effects"]))
        st = CC.apply_event(wf, st, {"sig": [CC.EV_SCHEDULE, "b_g1_01", "b"]})
        st = CC.apply_event(wf, st, {"sig": [W.EV_FATAL, "b_g1_01", "b", 1]})
        self.assertEqual(st["phase"], W.PHASE_COMP)
        # 已到达 join 的分支保持 joined，其余分支随失败撤销，无残留 waiters
        self.assertEqual(st["threads"]["b_g1_01"]["status"], CC.T_CANCELLED)
        self.assertEqual(st["threads"]["t0000"]["status"], CC.T_FORKED)
        self.assertEqual(st["waiters"], {})
        self.assertNotIn("g1", st["joins"])
        st = CC.apply_event(wf, st, {"sig": [W.EV_COMP_SUCCESS, "comp", "a", 1]})
        self.assertEqual(st["outcome"], "compensated")
        self.assertTrue(all(e["undone"] for e in st["effects"]))

    def test_pre_crash_recovery_inside_branch(self):
        wf = W.compile_workflow(_fork_join_spec(
            a={"pre_crash": True}))
        st = CC.c_initial_state(wf)
        st = CC.apply_event(wf, st, {"sig": [W.EV_SUCCESS, "t0000", "f", 1]})
        st = CC.apply_event(wf, st, {"sig": [CC.EV_SCHEDULE, "b_g1_00", "a"]})
        st = CC.apply_event(wf, st, {"sig": [W.EV_PRE_CRASH, "b_g1_00", "a", 1]})
        self.assertEqual(st["threads"]["b_g1_00"]["status"], CC.T_RECOVERING)
        st = CC.apply_event(wf, st, {"sig": [W.EV_RECOVER, "b_g1_00", "a", 1]})
        self.assertEqual(st["threads"]["b_g1_00"]["status"], CC.T_ACTIVE)
        self.assertEqual(st["threads"]["b_g1_00"]["tries"], 0)
        self.assertEqual(st["effects"], [])

    def test_duplicate_effect_in_branch_violates_at_most_once(self):
        wf = W.compile_workflow(_fork_join_spec(
            a_effect="effA",
            a={"idempotent": False, "post_crash": True}))
        r = CC.run_check(wf, bound=40)
        # 超时重调重复生效的不变量违反在并发 BFS 中同样被检出
        self.assertIn(CC.FIND_INVARIANT, r["findings"])

    def test_custom_invariant_evaluated_on_concurrent_state(self):
        inv = {"at_most_once": True, "undo_on_failure": True,
               "custom": [{"name": "j未完成时禁止end",
                           "expr": 'succ("end") = 0 or succ("j") > 0'}]}
        wf = W.compile_workflow(_fork_join_spec(invariants=inv))
        r = CC.run_check(wf, bound=40)
        # 所有合法调度都先 j 后 end，不变量恒成立
        self.assertNotIn(CC.FIND_INVARIANT, r["findings"])


class ValidationTests(unittest.TestCase):
    def _base(self, **conc_over):
        spec = _fork_join_spec()
        spec["concurrency"].update(conc_over)
        return spec

    def test_fork_needs_two_out_edges(self):
        # 保持图可达（f->a->j->end，b 由独立入边），只把 f 的出边降到 1 条
        spec = self._base()
        spec["edges"] = [{"src": "f", "dst": "a"},
                         {"src": "a", "dst": "j"}, {"src": "b", "dst": "j"},
                         {"src": "j", "dst": "end"}]
        with self.assertRaises(W.WorkflowError) as cm:
            W.compile_workflow(spec)
        self.assertTrue(any("至少需要 2 条出边" in e for e in str(cm.exception).split("；")))

    def test_join_unreachable_rejected(self):
        spec = self._base()
        spec["edges"] = [e for e in spec["edges"]
                         if e["src"] in ("f", "j")]
        # 去掉 a/j 与 b/j 后 j 不可达（f->a, f->b 仍在）
        spec["edges"].extend([])
        with self.assertRaises(W.WorkflowError):
            W.compile_workflow(spec)

    def test_unknown_resource_and_lock_rejected(self):
        spec = _fork_join_spec(a={"writes": ["nope"], "locks": ["ghost"]})
        with self.assertRaises(W.WorkflowError) as cm:
            W.compile_workflow(spec)
        msg = str(cm.exception)
        self.assertIn("共享资源未声明", msg)
        self.assertIn("锁未声明", msg)

    def test_nested_groups_rejected(self):
        # g1 区域内的活动 a 再充当 g2 的 fork（g2 需要两条出边才进入区域检查）
        spec = _fork_join_spec()
        spec["concurrency"]["groups"].append(
            {"id": "g2", "fork": "a", "join": "end"})
        spec["edges"] = [
            {"src": "f", "dst": "a"}, {"src": "f", "dst": "b"},
            {"src": "a", "dst": "j"}, {"src": "a", "dst": "end"},
            {"src": "b", "dst": "j"}, {"src": "j", "dst": "end"},
        ]
        with self.assertRaises(W.WorkflowError) as cm:
            W.compile_workflow(spec)
        self.assertIn("嵌套或重叠", str(cm.exception))

    def test_duplicate_lock_in_one_activity_rejected(self):
        spec = _fork_join_spec(locks=("L",), a={"locks": ["L", "L"]})
        with self.assertRaises(W.WorkflowError) as cm:
            W.compile_workflow(spec)
        self.assertIn("重复加锁", str(cm.exception))


class CompatibilityTests(unittest.TestCase):
    def test_non_concurrent_model_rejects_concurrency_check(self):
        spec = _spec([_safe_act("a"), _safe_act("b")],
                     edges=[{"src": "a", "dst": "b"}])
        wf = W.compile_workflow(spec)
        self.assertFalse(wf.concurrent)
        with self.assertRaises(CC.ConcError):
            CC.run_check(wf, bound=10)

    def test_serial_checker_unchanged_for_new_fields(self):
        # 并发声明不影响既有串行检验（模型只有一条链时根本没有分支）
        spec = _spec(
            [_safe_act("a", writes=["x"]), _safe_act("b", reads=["x"])],
            edges=[{"src": "a", "dst": "b"}],
            resources=("x",))
        wf = W.compile_workflow(spec)
        from app import checker as C
        r = C.run_check(wf, bound=10)
        self.assertIn(r["verdict"], ("exhausted", "bound_reached"))


class ConcurrencyApiTests(unittest.TestCase):
    """HTTP 层：分析、持久化、回放、分叉、历史读取与既有 API 共存。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from fastapi.testclient import TestClient
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        os.environ["CW_DB_PATH"] = cls._tmp.name
        os.environ["CW_FRONTEND_DIR"] = os.path.join(
            os.path.dirname(__file__), "..", "..", "frontend")
        from app import db
        db.init_db(cls._tmp.name)
        from app.main import app
        cls.client = TestClient(app)
        race = next(ex for ex in _api_examples(cls.client)
                    if ex["name"].startswith("并发：双分支"))
        cls.race_spec = race["spec"]

    def setUp(self):
        c = type(self).client
        self.model = c.post("/api/models",
                            json={"name": "conc-api", "spec": self.race_spec}).json()
        self.mid = self.model["id"]

    def test_check_persist_replay_fork_and_history(self):
        c = type(self).client
        r = c.post(f"/api/models/{self.mid}/concurrency",
                   json={"bound": 24, "persist": True}).json()
        self.assertEqual(r["verdict"], "conc_unsafe")
        self.assertIn("data_race", r["findings"])
        self.assertTrue(r["run_id"])

        runs = c.get(f"/api/models/{self.mid}/concurrency/runs").json()
        self.assertTrue(any(x["id"] == r["run_id"] for x in runs))
        full = c.get(f"/api/concurrency/runs/{r['run_id']}").json()
        self.assertEqual(full["result"]["verdict"], "conc_unsafe")

        evs = [s["event"] for s in r["counterexample"]["steps"] if s["event"]]
        rep = c.post(f"/api/models/{self.mid}/concurrency/replay",
                     json={"events": evs}).json()
        self.assertEqual(rep["verdict"], "conc_replay")
        self.assertEqual(rep["counterexample"]["length"], len(evs))

        # 见证轨迹是 fork 成功后的调度序列；在第一个 fork 成功事件之后改选
        # 另一分支线程（steps[1] 即 fork 成功后的状态）
        fork_ok = next(s for s in r["counterexample"]["steps"]
                       if s["event"] and s["event"].get("fork_group"))
        idx = r["counterexample"]["steps"].index(fork_ok)
        alts = fork_ok["alternatives"]
        sig = next(a["sig"] for a in alts if a["thread"].startswith("b_pay_01"))
        prefix = [s["event"] for s in r["counterexample"]["steps"][1:idx + 1]]
        fk = c.post(f"/api/models/{self.mid}/concurrency/fork",
                    json={"prefix": prefix, "sig": sig,
                          "bound": 24}).json()
        self.assertEqual(fk["verdict"], "conc_unsafe")
        self.assertIn("data_race", fk["findings"])
        self.assertEqual(fk["fork_point"], len(prefix))

    def test_non_concurrent_model_returns_400(self):
        c = type(self).client
        safe = next(ex for ex in _api_examples(c) if ex["name"].startswith("两活动"))
        m = c.post("/api/models", json={"name": "plain", "spec": safe["spec"]}).json()
        resp = c.post(f"/api/models/{m['id']}/concurrency", json={"bound": 10})
        self.assertEqual(resp.status_code, 400)

    def test_existing_check_and_risk_apis_still_work(self):
        c = type(self).client
        chk = c.post(f"/api/models/{self.mid}/check",
                     json={"bound": 10, "persist": False}).json()
        self.assertIn(chk["verdict"], ("exhausted", "bound_reached", "violated"))
        v = c.post("/api/risk/validate",
                   json={"spec": self.race_spec, "activities": []}).json()
        self.assertTrue(v["ok"])

    def test_deadlock_example_end_to_end(self):
        c = type(self).client
        dl = next(ex for ex in _api_examples(c) if ex["name"].startswith("并发：相反"))
        m = c.post("/api/models", json={"name": "dl", "spec": dl["spec"]}).json()
        r = c.post(f"/api/models/{m['id']}/concurrency",
                   json={"bound": 40, "persist": False}).json()
        self.assertIn("deadlock", r["findings"])
        w = r["findings"]["deadlock"]
        self.assertEqual(len(w["cycle"]), 2)
        self.assertEqual(set(w["locks"]), {"accountLock", "ledgerLock"})


def _api_examples(client):
    return client.get("/api/examples").json()


if __name__ == "__main__":
    unittest.main()
