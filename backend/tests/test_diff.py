"""版本行为对比（diff）模块的回归测试。

覆盖：
* 同模型自比 -> identical（已穷尽）；
* 活动开关差异只在一侧可触发 -> 最短轨迹分叉见证（divergence），见证可回放、
  持留步为 hold、分叉侧独有事件可物化；
* 变量赋值差异 -> variables 见证；终态差异 -> terminal 见证；
* 外部效果差异（幂等开关/效果键）-> effects 见证；
* 共同自定义不变量的结论翻转 -> invariant 见证；仅一侧启用的不变量进入配置差异；
* 步数边界截断 -> inconclusive 且 incompleteness 非空（结果未完备标注）；
* 见证步骤的对齐索引连续、两侧长度在分叉步对齐；
* 从差异节点分叉复检（/api/diff/fork）、对比记录持久化、按记录打开（含快照）；
* 同模型对比返回 400；既有检验/风险/并发 API 不受影响。
"""
from __future__ import annotations

import os
import unittest

from app import diff as D
from app import workflow as W


def _spec(*, start="a", variables=None, activities=None, edges=None,
          at_most_once=True, undo_on_failure=True, custom=None):
    return {
        "start": start,
        "variables": variables or [],
        "activities": activities or [],
        "edges": edges if edges is not None else [],
        "invariants": {"at_most_once": at_most_once,
                       "undo_on_failure": undo_on_failure,
                       "custom": custom or []},
    }


def _act(aid, **over):
    a = {"id": aid, "name": aid.upper(), "can_fail": False, "retryable": False,
         "max_attempts": 1, "comp_can_fail": False}
    a.update(over)
    return a


def _chain(effects=("e1", "e2"), **kw):
    """两活动串行链：a -> b。"""
    acts = [
        _act("a", effect=effects[0], **kw),
        _act("b", effect=effects[1]),
    ]
    return _spec(activities=acts, edges=[{"src": "a", "dst": "b"}])


class DiffCoreTests(unittest.TestCase):
    def test_identical_self_comparison_is_exhaustive(self):
        spec = _chain()
        wf = W.compile_workflow(spec)
        r = D.run_diff(wf, W.compile_workflow(spec), bound=12)
        self.assertEqual(r["verdict"], "identical")
        self.assertFalse(r["stats"]["truncated"])
        self.assertEqual(r["witnesses"], {})
        self.assertIsNone(r["divergence"])
        # 至少覆盖 初始->a成功->b成功 三个对齐对
        self.assertGreaterEqual(r["stats"]["pairs_visited"], 3)

    def test_divergence_witness_when_event_only_one_side(self):
        base = _chain()
        cand = _chain()
        cand["activities"][0]["can_fail"] = True   # 候选可致命失败（基线不能）
        wb, wc = W.compile_workflow(base), W.compile_workflow(cand)
        r = D.run_diff(wb, wc, bound=12)
        self.assertEqual(r["verdict"], "diff_found")
        self.assertIsNotNone(r["divergence"])
        d = r["divergence"]
        self.assertEqual(d["kind"], "divergence")
        self.assertEqual(d["side"], "candidate")
        self.assertEqual(d["event"]["activity"], "a")
        self.assertIn(d["event"]["type"], ("fatal_error", "attempts_exhausted"))
        # 分叉前两侧长度一致（初始步 + 对齐长度）；分叉步触发侧执行独有事件，
        # 另一侧在同一对齐索引上 hold，因此两侧步数相同
        self.assertEqual(d["diverge_index"], 1)
        self.assertEqual(len(d["candidate_steps"]), len(d["baseline_steps"]))
        self.assertTrue(d["candidate_steps"][-1]["divergence"])
        self.assertTrue(d["baseline_steps"][-1]["hold"])
        # 对齐索引连续
        self.assertEqual([s["align_index"] for s in d["candidate_steps"]],
                         list(range(len(d["candidate_steps"]))))

    def test_baseline_only_event_is_reported_on_baseline_side(self):
        base = _chain()
        base["activities"][0]["can_fail"] = True
        cand = _chain()
        r = D.run_diff(W.compile_workflow(base), W.compile_workflow(cand), bound=12)
        d = r["divergence"]
        self.assertEqual(d["side"], "baseline")
        self.assertEqual(d["diverge_index"], 1)
        self.assertEqual(len(d["baseline_steps"]), d["diverge_index"] + 1)
        self.assertTrue(d["baseline_steps"][-1]["divergence"])
        self.assertTrue(d["candidate_steps"][-1]["hold"])

    def test_variable_change_witness(self):
        base = _chain()
        base["variables"] = [{"name": "v", "type": "int", "initial": 0}]
        base["activities"][0]["assign"] = [{"var": "v", "expr": "1"}]
        cand = _chain()
        cand["variables"] = [{"name": "v", "type": "int", "initial": 0}]
        cand["activities"][0]["assign"] = [{"var": "v", "expr": "2"}]
        r = D.run_diff(W.compile_workflow(base), W.compile_workflow(cand), bound=12)
        self.assertIn("variables", r["witnesses"])
        w = r["witnesses"]["variables"]
        self.assertEqual(w["changes"][0]["name"], "v")
        self.assertEqual((w["changes"][0]["base"], w["changes"][0]["candidate"]), (1, 2))
        # 最短对齐长度：a 成功后变量即不同 -> 1
        self.assertEqual(w["length"], 1)
        self.assertTrue(w["candidate_steps"][-1]["diff_point"])

    def test_terminal_change_witness(self):
        # a 成功生效（e1）；b 致命失败后补偿 a：基线补偿总是成功 -> compensated；
        # 候选中 a 的补偿动作可致命失败 -> failed（e1 永久残留）。
        base = _spec(
            activities=[
                _act("a", effect="e1", comp_can_fail=False),
                _act("b", can_fail=True, retryable=False),
            ],
            edges=[{"src": "a", "dst": "b"}])
        cand = _spec(
            activities=[
                _act("a", effect="e1",
                     comp_can_fail=True, comp_retryable=False),
                _act("b", can_fail=True, retryable=False),
            ],
            edges=[{"src": "a", "dst": "b"}])
        r = D.run_diff(W.compile_workflow(base), W.compile_workflow(cand), bound=12)
        # 共同对齐到 a 成功、b 失败进入补偿后，补偿事件集合分叉；终态变化作为
        # 该分叉见证的别名类别（terminal）出现。
        self.assertIn("terminal", r["witnesses"])
        w = r["witnesses"]["terminal"]
        self.assertTrue(w.get("via_divergence"))
        ch = w["changes"][0]
        self.assertEqual(ch["candidate_outcome"], "failed")
        # 基线下一步补偿成功即 compensated（在独有致命分叉步上仍是非终态）
        self.assertIn(ch["base_outcome"], (None, "compensated"))
        # 同一分叉上失败终态携带未撤销效果，undo_on_failure 不变量同时翻转
        self.assertIn("invariant", r["witnesses"])

    def test_effects_change_witness(self):
        # 基线 a 非幂等 + 应答丢失可产生重复；候选 a 幂等 -> 重复计数不同。
        base = _chain()
        base["activities"][0].update(idempotent=False, post_crash=True)
        base["activities"][1].update(effect="e2")
        cand = _chain()
        cand["activities"][0].update(idempotent=True, post_crash=True)
        cand["activities"][1].update(effect="e2")
        r = D.run_diff(W.compile_workflow(base), W.compile_workflow(cand), bound=20)
        # 至少存在 effects 或 divergence 之一；此处效果结构键相同、重复路径存在
        self.assertTrue(r["witnesses"].get("effects") or r["divergence"])

    def test_invariant_conclusion_flip_witness(self):
        # 自定义不变量 effects("e1") = 0：基线 a 无效果（恒真），候选 a 有效果（假）。
        inv = [{"name": "无e1", "expr": 'effects("e1") = 0'}]
        base = _spec(
            activities=[_act("a", effect=""), _act("b", effect="e2")],
            edges=[{"src": "a", "dst": "b"}], custom=inv)
        cand = _spec(
            activities=[_act("a", effect="e1"), _act("b", effect="e2")],
            edges=[{"src": "a", "dst": "b"}], custom=inv)
        r = D.run_diff(W.compile_workflow(base), W.compile_workflow(cand), bound=12)
        self.assertIn("invariant", r["witnesses"])
        ch = r["witnesses"]["invariant"]["changes"][0]
        self.assertEqual(ch["id"], "custom:无e1")
        self.assertEqual((ch["base_violated"], ch["candidate_violated"]), (False, True))

    def test_invariant_enabled_on_one_side_is_config_diff_not_witness(self):
        base = _spec(
            activities=[_act("a", effect="e1"), _act("b", effect="e2")],
            edges=[{"src": "a", "dst": "b"}],
            custom=[{"name": "x", "expr": "true"}])
        cand = _spec(
            activities=[_act("a", effect="e1"), _act("b", effect="e2")],
            edges=[{"src": "a", "dst": "b"}], custom=[])
        r = D.run_diff(W.compile_workflow(base), W.compile_workflow(cand), bound=12)
        self.assertEqual(r["verdict"], "identical")
        self.assertIn("自定义不变量「x」", r["invariant_config_diff"]["only_baseline"])

    def test_bound_truncation_marks_inconclusive(self):
        # 无界增长的变量 + 环：步数用尽时仍有未展开的对齐状态
        spec = _spec(
            variables=[{"name": "v", "type": "int", "initial": 0}],
            activities=[
                _act("a", assign=[{"var": "v", "expr": "v + 1"}]),
                _act("b"),
            ],
            edges=[{"src": "a", "dst": "b"}, {"src": "b", "dst": "a"}])
        wf = W.compile_workflow(spec)
        r = D.run_diff(wf, W.compile_workflow(spec), bound=4)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertTrue(r["stats"]["truncated"])
        self.assertTrue(r["incompleteness"])
        self.assertTrue(any("步数边界" in t for t in r["incompleteness"]))

    def test_truncated_but_diff_found_is_diff_found_incomplete(self):
        spec = _spec(
            variables=[{"name": "v", "type": "int", "initial": 0}],
            activities=[
                _act("a", assign=[{"var": "v", "expr": "v + 1"}]),
                _act("b"),
            ],
            edges=[{"src": "a", "dst": "b"}, {"src": "b", "dst": "a"}])
        spec2 = _spec(
            variables=[{"name": "v", "type": "int", "initial": 0}],
            activities=[
                _act("a", assign=[{"var": "v", "expr": "v + 2"}]),
                _act("b"),
            ],
            edges=[{"src": "a", "dst": "b"}, {"src": "b", "dst": "a"}])
        r = D.run_diff(W.compile_workflow(spec), W.compile_workflow(spec2), bound=4)
        self.assertEqual(r["verdict"], "diff_found_incomplete")
        self.assertIn("variables", r["witnesses"])
        self.assertTrue(r["stats"]["truncated"])


class DiffApiTests(unittest.TestCase):
    """HTTP 层：执行、持久化、分叉复检、按记录打开与既有 API 共存。"""

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

    def _make_model(self, name, **patch):
        spec = _chain()
        spec["variables"] = [{"name": "v", "type": "int", "initial": 0}]
        spec["activities"][0].update(**patch)
        spec["activities"][0]["assign"] = [{"var": "v", "expr": "1"}]
        r = type(self).client.post("/api/models", json={"name": name, "spec": spec})
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()

    def test_diff_persist_history_record_replay_and_fork(self):
        c = type(self).client
        base = self._make_model("基线")
        cand = self._make_model("候选", can_fail=True, retryable=False)
        r = c.post("/api/diff", json={
            "baseline_id": base["id"], "candidate_id": cand["id"],
            "bound": 12, "persist": True}).json()
        self.assertEqual(r["verdict"], "diff_found")
        self.assertTrue(r["run_id"])
        self.assertIsNotNone(r["divergence"])

        runs = c.get("/api/diff/runs").json()
        self.assertTrue(any(x["id"] == r["run_id"] for x in runs))
        # 按模型过滤
        runs_b = c.get(f"/api/diff/runs?model_id={base['id']}").json()
        self.assertTrue(all(x["baseline_id"] == base["id"]
                            or x["candidate_id"] == base["id"] for x in runs_b))

        full = c.get(f"/api/diff/runs/{r['run_id']}").json()
        self.assertEqual(full["result"]["verdict"], "diff_found")
        self.assertEqual(full["baseline_spec"]["start"], "a")
        self.assertEqual(full["candidate_spec"]["start"], "a")
        self.assertEqual(full["params"]["bound"], 12)

        # 从分叉见证的候选侧首步做改选复检（候选可致命失败）
        d = r["divergence"]
        fk = c.post("/api/diff/fork", json={
            "diff_run_id": r["run_id"], "side": "candidate",
            "prefix": [], "sig": d["sig"], "bound": 12}).json()
        self.assertEqual(fk["diff_side"], "candidate")
        self.assertEqual(fk["fork_point"], 0)
        self.assertIn(fk["verdict"], ("violated", "exhausted", "bound_reached"))
        self.assertTrue(fk["counterexample"]["steps"])

        # 不带记录、直接用两侧 id 复检
        fk2 = c.post("/api/diff/fork", json={
            "baseline_id": base["id"], "candidate_id": cand["id"],
            "side": "candidate", "prefix": [],
            "sig": ["success", "a", 1], "bound": 12}).json()
        self.assertIn(fk2["verdict"], ("violated", "exhausted", "bound_reached"))

    def test_same_model_rejected(self):
        c = type(self).client
        m = self._make_model("自比模型")
        resp = c.post("/api/diff", json={
            "baseline_id": m["id"], "candidate_id": m["id"], "bound": 8})
        self.assertEqual(resp.status_code, 400)

    def test_fork_after_record_model_deleted_still_uses_snapshot(self):
        c = type(self).client
        base = self._make_model("临时基线")
        cand = self._make_model("临时候选", can_fail=True, retryable=False)
        r = c.post("/api/diff", json={
            "baseline_id": base["id"], "candidate_id": cand["id"],
            "bound": 12}).json()
        rid = r["run_id"]
        sig = r["divergence"]["sig"]
        c.delete(f"/api/models/{cand['id']}")
        # 记录仍可读取（快照持久化）
        full = c.get(f"/api/diff/runs/{rid}").json()
        self.assertEqual(full["result"]["verdict"], "diff_found")
        # 基于快照的分叉复检仍可用
        fk = c.post("/api/diff/fork", json={
            "diff_run_id": rid, "side": "candidate",
            "prefix": [], "sig": sig, "bound": 12}).json()
        self.assertIn(fk["verdict"], ("violated", "exhausted", "bound_reached"))

    def test_existing_apis_unchanged(self):
        c = type(self).client
        m = self._make_model("兼容模型")
        chk = c.post(f"/api/models/{m['id']}/check",
                     json={"bound": 10, "persist": False}).json()
        self.assertIn(chk["verdict"], ("violated", "exhausted", "bound_reached"))
        v = c.post("/api/risk/validate",
                   json={"spec": m["spec"], "activities": []}).json()
        self.assertTrue(v["ok"])
        self.assertEqual(c.get("/api/health").json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
