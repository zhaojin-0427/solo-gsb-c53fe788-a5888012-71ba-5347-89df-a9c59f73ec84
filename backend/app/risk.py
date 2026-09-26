"""随机故障风险分析：带记忆的概率状态传播。

在既有有界模型检验语义（见 workflow.py / checker.py）之上，为每个“非确定性
决策点”附加概率：

* 调用分支（活动处于 ACTIVE 时）：成功 / 可重试错误 / 不可重试错误 /
  确认前崩溃 / 确认后崩溃（应答丢失），五类概率之和必须为 1；
* 应答丢失的两个条件概率：恢复时确认已生效 p_ack，以及重调后“首次其实已
  生效”的条件概率 p_dup；
* 补偿分支（每个活动的补偿动作）：补偿成功 / 可重试错误 / 不可重试失败，
  概率之和必须为 1。

确定性的中间事件（错误持久化后重新入队 retry、确认前崩溃后的 recover、
补偿重调 comp_retry、ε 转移）概率恒为 1，不需要配置。

传播按“可见事件”（透明穿过 ε）逐层进行，与 checker._visible_successors 的
步长定义一致：每个真实事件消耗 1 步，ε 不消耗。概率质量按 **(状态指纹, 该
路径上已命中过的不变量集合)** 二元组合并（带记忆）：命中集合随每个传播分支
携带，不变量概率只在路径“首次进入违反”的那条转移上累计一次，之后即使不变
量持续为假、或经循环再次进入违反状态，该路径的质量也不会重复计入；违反不
是终态，持续违反的分支继续正常传播。同时为每个桶保留概率最大的前 top_n 条
前缀路径用于轨迹归因。输出终态概率分布、各不变量违反概率、期望未撤销效果
数、贡献最大的前 N 条轨迹，并显式标注步数边界截断与概率校验错误。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import checker as C
from . import workflow as W

MAX_RISK_BOUND = 80
DEFAULT_RISK_BOUND = 24
DEFAULT_TOP_N = 10
MAX_TOP_N = 50
SUM_TOLERANCE = 1e-9
# 安全上限：状态空间在边界内爆炸时停止展开，残余质量全部计入“边界截断”
STATE_CAP = 100_000

# 终态类别
CAT_SUCCEEDED = "succeeded"
CAT_COMPENSATED = "compensated"
CAT_FAILED = "failed"
CAT_TRUNCATED = "truncated"
CAT_VIOLATION = "violation"

CATEGORY_LABELS = {
    CAT_SUCCEEDED: "成功完成",
    CAT_COMPENSATED: "补偿完成",
    CAT_FAILED: "补偿失败（有效果永久残留）",
    CAT_TRUNCATED: "边界截断（未到达终态）",
    CAT_VIOLATION: "不变量违反",
}

# 调用分支可配置的五个基类 -> （中文名, 对应事件类型）
CALL_FIELDS = [
    ("success", "成功", W.EV_SUCCESS),
    ("retryable", "可重试错误", W.EV_RETRYABLE),
    ("fatal", "不可重试错误", W.EV_FATAL),
    ("pre_crash", "确认前崩溃", W.EV_PRE_CRASH),
    ("post_crash", "确认后崩溃/应答丢失", None),
]
COMP_FIELDS = [
    ("comp_success", "补偿成功", W.EV_COMP_SUCCESS),
    ("comp_retryable", "补偿可重试错误", W.EV_COMP_ERROR),
    ("comp_fatal", "补偿不可重试失败", W.EV_COMP_FATAL),
]


class RiskError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 配置解析与校验
# ---------------------------------------------------------------------------

@dataclass
class ResolvedActivity:
    aid: str
    probs: dict[str, float] = field(default_factory=dict)
    p_ack: float = 0.5
    p_dup: float = 0.5


def _default_activity(a: W.Activity) -> ResolvedActivity:
    """未配置（或全部缺省）的活动：确定性成功，条件概率 0.5。"""
    return ResolvedActivity(
        aid=a.id,
        probs={
            "success": 1.0, "retryable": 0.0, "fatal": 0.0,
            "pre_crash": 0.0, "post_crash": 0.0,
            "comp_success": 1.0, "comp_retryable": 0.0, "comp_fatal": 0.0,
        },
        p_ack=0.5, p_dup=0.5,
    )


def _eligible_call(a: W.Activity) -> set[str]:
    """活动当前开关下实际可枚举的调用基类。"""
    elig = {"success"}
    if a.can_fail:
        if a.retryable:
            elig.add("retryable")     # 最后一次尝试上表现为 attempts_exhausted
        elig.add("fatal")
    if a.pre_crash:
        elig.add("pre_crash")
    if a.post_crash:
        elig.add("post_crash")
    return elig


def _eligible_comp(a: W.Activity) -> set[str]:
    elig = {"comp_success"}
    if a.comp_can_fail:
        if a.comp_retryable:
            elig.add("comp_retryable")
        elig.add("comp_fatal")
    return elig


def resolve_config(wf: W.Workflow, raw_activities: list[dict]) -> dict[str, ResolvedActivity]:
    cfg: dict[str, ResolvedActivity] = {aid: _default_activity(wf.act(aid))
                                        for aid in wf.activities}
    for item in raw_activities or []:
        aid = str(item.get("id", "")).strip()
        if aid not in cfg:
            continue  # 未知活动由 validate_params 报错
        r = cfg[aid]
        for key, *_ in CALL_FIELDS:
            v = item.get(key)
            if v is not None:
                r.probs[key] = float(v)
        for key, *_ in COMP_FIELDS:
            v = item.get(key)
            if v is not None:
                r.probs[key] = float(v)
        if item.get("p_ack") is not None:
            r.p_ack = float(item["p_ack"])
        if item.get("p_dup") is not None:
            r.p_dup = float(item["p_dup"])
    return cfg


def validate_params(wf: W.Workflow, raw_activities: list[dict]
                    ) -> tuple[list[str], dict[str, ResolvedActivity], list[dict]]:
    """校验概率配置。返回 (错误列表, 解析后的配置, 每个活动的分支求和信息)。"""
    errors: list[str] = []
    known = set(wf.activities)
    seen: set[str] = set()
    for item in raw_activities or []:
        aid = str(item.get("id", "")).strip()
        if not aid:
            errors.append("存在没有活动 id 的概率配置")
            continue
        if aid not in known:
            errors.append(f"概率配置引用了不存在的活动：{aid}")
            continue
        if aid in seen:
            errors.append(f"活动 {aid} 的概率配置重复")
        seen.add(aid)

    cfg = resolve_config(wf, raw_activities)
    branches: list[dict] = []

    for aid in sorted(wf.activities):
        a = wf.act(aid)
        r = cfg[aid]
        binfo = {"activity": aid, "name": a.name,
                 "call_sum": 0.0, "comp_sum": 0.0,
                 "call_eligible": [], "comp_eligible": []}

        # 数值范围
        for key, label, _ in CALL_FIELDS + COMP_FIELDS:
            v = r.probs[key]
            if v < 0.0 or v > 1.0:
                errors.append(f"活动 {aid} 的「{label}」概率必须在 [0, 1] 内（当前 {v:g}）")
        if a.post_crash:
            for name, v in (("恢复确认已生效概率 p_ack", r.p_ack),
                            ("重调后首次已生效条件概率 p_dup", r.p_dup)):
                if v < 0.0 or v > 1.0:
                    errors.append(f"活动 {aid} 的{name}必须在 [0, 1] 内（当前 {v:g}）")

        call_elig = _eligible_call(a)
        comp_elig = _eligible_comp(a)
        binfo["call_eligible"] = sorted(call_elig)
        binfo["comp_eligible"] = sorted(comp_elig)

        # 未启用分支的概率必须为 0
        for key, label, _ in CALL_FIELDS:
            if key not in call_elig and r.probs[key] != 0.0:
                reasons = {
                    "retryable": "该活动未启用可重试错误",
                    "fatal": "该活动未启用“可能失败”",
                    "pre_crash": "该活动未启用确认前崩溃",
                    "post_crash": "该活动未启用应答丢失",
                }
                errors.append(f"活动 {aid} 调用分支：{reasons.get(key, '该结果未启用')}，"
                              f"「{label}」概率必须为 0（当前 {r.probs[key]:g}）")
        for key, label, _ in COMP_FIELDS:
            if key not in comp_elig and r.probs[key] != 0.0:
                if key == "comp_retryable":
                    why = "补偿动作未启用可重试错误"
                else:
                    why = "补偿动作未启用“可能失败”"
                errors.append(f"活动 {aid} 补偿分支：{why}，「{label}」概率必须为 0"
                              f"（当前 {r.probs[key]:g}）")

        call_sum = sum(r.probs[k] for k, *_ in CALL_FIELDS)
        comp_sum = sum(r.probs[k] for k, *_ in COMP_FIELDS)
        binfo["call_sum"] = call_sum
        binfo["comp_sum"] = comp_sum
        branches.append(binfo)
        if abs(call_sum - 1.0) > SUM_TOLERANCE:
            errors.append(f"活动 {aid}（{a.name}）调用分支概率和为 {call_sum:.6f}，必须等于 1")
        if abs(comp_sum - 1.0) > SUM_TOLERANCE:
            errors.append(f"活动 {aid}（{a.name}）补偿分支概率和为 {comp_sum:.6f}，必须等于 1")

    return errors, cfg, branches


def config_to_json(cfg: dict[str, ResolvedActivity]) -> list[dict]:
    out = []
    for aid in sorted(cfg):
        r = cfg[aid]
        out.append({"id": aid, **r.probs, "p_ack": r.p_ack, "p_dup": r.p_dup})
    return out


# ---------------------------------------------------------------------------
# 事件权重：把基类概率映射到具体交错事件
# ---------------------------------------------------------------------------

def _event_weight(wf: W.Workflow, state: dict, ev: dict,
                  cfg: dict[str, ResolvedActivity]) -> float:
    cur_status = state["status"].get(state.get("current"))
    if cur_status in (W.RETRYING, W.RECOVERING) or state.get("comp_retrying"):
        return 1.0  # 确定性中间事件（retry / recover / comp_retry）

    kind = ev["type"]
    if state["phase"] == W.PHASE_RUN:
        r = cfg[state["current"]]
        p = r.probs
        if kind == W.EV_SUCCESS:
            return p["success"]
        if kind in (W.EV_RETRYABLE, W.EV_EXHAUSTED):
            # 最后一次尝试上“可重试错误”按既有语义表现为尝试次数耗尽
            return p["retryable"]
        if kind == W.EV_FATAL:
            return p["fatal"]
        if kind == W.EV_PRE_CRASH:
            return p["pre_crash"]
        if kind == W.EV_ACK:
            return p["post_crash"] * r.p_ack
        if kind == W.EV_RETRY and ev.get("ambiguous"):
            return p["post_crash"] * (1.0 - r.p_ack)
        if kind == W.EV_RETRY_DUP:
            return p["success"] * r.p_dup
        if kind == W.EV_RETRY_CLEAN:
            return p["success"] * (1.0 - r.p_dup)
        raise RiskError(f"风险分析遇到未映射的调用事件：{kind}")

    # 补偿阶段
    if not state["comp_stack"]:
        return 1.0  # “所有效果均已撤销”的确定性终态事件
    top = state["effects"][state["comp_stack"][0]]
    r = cfg[top["activity"]]
    p = r.probs
    if kind == W.EV_COMP_SUCCESS:
        return p["comp_success"]
    if kind in (W.EV_COMP_ERROR, W.EV_COMP_EXHAUSTED):
        return p["comp_retryable"]
    if kind == W.EV_COMP_FATAL:
        return p["comp_fatal"]
    raise RiskError(f"风险分析遇到未映射的补偿事件：{kind}")


def _weighted_successors(wf: W.Workflow, state: dict, cfg: dict[str, ResolvedActivity],
                         depth: int = 0) -> list[tuple[dict, dict, float]]:
    """可见后继（透明穿过 ε）附带权重 [(next_state, event, weight)]。"""
    out: list[tuple[dict, dict, float]] = []
    for nxt, ev in W.successors(wf, state):
        if ev is None:
            if depth > 8:
                raise RiskError("ε 转移链过长（模型可能存在异常自环）")
            out.extend(_weighted_successors(wf, nxt, cfg, depth + 1))
        else:
            out.append((nxt, ev, _event_weight(wf, state, ev, cfg)))
    return out


# ---------------------------------------------------------------------------
# 路径前缀（父指针表示，避免逐路径复制事件列表）
# ---------------------------------------------------------------------------

@dataclass
class _Path:
    __slots__ = ("parent", "event", "prob", "depth", "key")
    parent: Any
    event: dict | None
    prob: float
    depth: int
    key: tuple

    def materialize(self) -> list[dict]:
        events: list[dict] = []
        node = self
        while node.event is not None:
            events.append(node.event)
            node = node.parent
        events.reverse()
        return events


_ROOT_PATH = _Path(None, None, 1.0, 0, ())


@dataclass
class _Bucket:
    """传播桶：同一 (状态指纹, 已命中不变量集合) 的质量在此合并。

    mass 是该桶携带的全部概率质量（精确）；hits 是桶内每条路径上已经
    首次违反过的不变量 id 有序元组（同一桶内必然相同），随分支继续传播。
    paths 只用于轨迹归因（近似召回）。
    """
    state: dict
    mass: float
    paths: list[_Path]
    hits: tuple


def _hit_key(ids: set[str] | tuple) -> tuple:
    """已命中不变量集合的稳定表示（用作桶合并键的一部分）。"""
    return tuple(sorted(ids))


def _keep_top(paths: list[_Path], k: int) -> list[_Path]:
    if len(paths) <= k:
        return paths
    paths.sort(key=lambda q: q.prob, reverse=True)
    return paths[:k]


def _outstanding(state: dict) -> int:
    return sum(1 for e in state["effects"] if not e["undone"])


def _outstanding_keys(state: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in state["effects"]:
        if not e["undone"]:
            counts[e["key"]] = counts.get(e["key"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 主分析过程
# ---------------------------------------------------------------------------

def run_risk_analysis(wf: W.Workflow, raw_activities: list[dict],
                      bound: int = DEFAULT_RISK_BOUND, top_n: int = DEFAULT_TOP_N,
                      init_vars: dict[str, Any] | None = None) -> dict:
    bound = min(max(int(bound), 1), MAX_RISK_BOUND)
    top_n = min(max(int(top_n), 1), MAX_TOP_N)

    errors, cfg, branches = validate_params(wf, raw_activities)
    validation = {"ok": not errors, "errors": errors, "branches": branches}
    if errors:
        return {
            "verdict": "risk_invalid",
            "verdict_text": "概率配置未通过校验，请修正标注的分支后重新分析",
            "bound": bound, "top_n": top_n,
            "params": {"activities": config_to_json(cfg), "bound": bound, "top_n": top_n},
            "validation": validation,
        }

    state0 = W.initial_state(wf, init_vars)
    root_fp = W.fingerprint(state0)
    # 轨迹归因是**近似**的（概率分布本身精确）：每个状态桶只保留贡献最大的
    # keep_k 条前缀，且概率低于 min_path_prob 的前缀直接丢弃。截断的只是
    # “出现在前 N 条轨迹列表里”的可能性，不影响结局/违反/期望的数值结论。
    keep_k = max(top_n * 2, 12)
    min_path_prob = 1e-6

    distribution = {CAT_SUCCEEDED: 0.0, CAT_COMPENSATED: 0.0,
                    CAT_FAILED: 0.0, CAT_TRUNCATED: 0.0}
    violation_any_mass = 0.0
    inv_probs: dict[str, float] = {}
    inv_names: dict[str, str] = {}
    # 期望未撤销效果：按质量加权的未撤销效果条数，按结局分类累计。
    # succeeded 下效果合法保留；failed 下的才是“补偿失败导致永久残留”的风险量。
    exp_by_outcome: dict[str, float] = {
        CAT_SUCCEEDED: 0.0, CAT_COMPENSATED: 0.0, CAT_FAILED: 0.0}
    exp_out_truncated = 0.0
    exp_by_key_failed: dict[str, float] = {}
    trace_rows: list[dict] = []     # 终态/违反的完整（或代表）轨迹
    transitions = 0
    state_registry = {root_fp: state0}
    # 违反概率是“路径**首次**进入违反”这一转移事件的概率：每个传播分支携带
    # 已命中不变量集合（_Bucket.hits），桶键 = (状态指纹, 已命中集合)。这样：
    #   * 同一路径在后续状态中“持续为假”不会被重复累计（命中集合不变）；
    #   * 首次命中时刻不同的路径即使到达同一指纹也不会相互污染；
    #   * 违反不是终态：已命中的质量继续随分支传播，结局分布不受影响。
    max_depth = 0

    def add_trace(path: _Path, probability: float, category: str, state: dict,
                  violations: list[dict], truncated: bool = False) -> None:
        trace_rows.append({
            "probability": probability,
            "length": path.depth,
            "category": category,
            "category_label": (CATEGORY_LABELS[category] if category != CAT_VIOLATION
                               else "不变量违反：" + violations[0]["name"]),
            "outcome": state.get("outcome"),
            "violations": [{"id": v["id"], "name": v["name"]} for v in violations],
            "truncated": truncated,
            "events": path.materialize(),
            # 同一事件序列可以同时是“终态轨迹”和“首次违反轨迹”，二者分别保留
            "_key": (category, path.key),
        })

    root_violations = C.check_invariants(wf, state0)
    if root_violations:
        violation_any_mass += 1.0
        for v in root_violations:
            inv_probs[v["id"]] = inv_probs.get(v["id"], 0.0) + 1.0
            inv_names[v["id"]] = v["name"]
        add_trace(_ROOT_PATH, 1.0, CAT_VIOLATION, state0, root_violations)
        # 初始状态即违反：不存在“有效的初始状态”，停止传播
        frontier: dict[tuple, _Bucket] = {}
    else:
        frontier = {(root_fp, ()): _Bucket(state0, 1.0, [_ROOT_PATH], ())}

    state_cap_reached = False
    for depth in range(bound):
        if not frontier:
            break
        # 质量按目标桶键 (指纹, 已命中集合) 聚合（分布计数精确）；路径候选先
        # 全部收集，再按 **完整事件签名序列** 跨来源合并、按目标桶裁剪到
        # keep_k：不同前缀（含经自环）作为不同序列保留，同序列质量合并。
        nxt_mass: dict[tuple, float] = {}
        nxt_state: dict[str, dict] = {}
        nxt_candidates: dict[tuple, list[tuple]] = {}
        # 每个“来源桶 -> 可见后继”的贡献只在路径**首次命中**时计入违反概率，
        # 贡献质量 = 来源桶质量 × 事件权重（与该来源保留哪些代表路径无关）。
        # nxt_new_viol 按目标桶收集本次合并中有质量首次命中的不变量（仅用于
        # 代表轨迹标注；数值概率在下方逐贡献精确累计）。
        nxt_new_viol: dict[tuple, dict[str, dict]] = {}
        new_any_mass = 0.0
        for (_fp, _hit_set), bucket in frontier.items():
            weighted = _weighted_successors(wf, bucket.state, cfg)
            for nxt, ev, w in weighted:
                transitions += 1
                if w <= 0.0:
                    continue
                contrib = bucket.mass * w
                nfp = W.fingerprint(nxt)
                state_registry.setdefault(nfp, nxt)
                nxt_state.setdefault(nfp, nxt)
                current_viols = C.check_invariants(wf, nxt)
                current_ids = {v["id"] for v in current_viols}
                # 已命中集合沿转移传播；本次转移新命中的 = 当前为假 - 此前已命中
                child_hits = _hit_key(set(bucket.hits) | current_ids)
                bkey = (nfp, child_hits)
                nxt_mass[bkey] = nxt_mass.get(bkey, 0.0) + contrib
                newly = [v for v in current_viols if v["id"] not in bucket.hits]
                if newly:
                    # 只在路径首次命中时累计：各具体不变量各自只计一次；
                    # “任一违反”只对此前**从未**违反过的路径计一次（之后该
                    # 路径再命中其他不变量只更新具体不变量概率，不重复计 any）。
                    if not bucket.hits:
                        new_any_mass += contrib
                    annot = nxt_new_viol.setdefault(bkey, {})
                    for v in newly:
                        inv_probs[v["id"]] = inv_probs.get(v["id"], 0.0) + contrib
                        inv_names[v["id"]] = v["name"]
                        annot.setdefault(v["id"], v)
                cands = nxt_candidates.setdefault(bkey, [])
                sig_t = tuple(ev["sig"])
                for path in bucket.paths:
                    np_ = path.prob * w
                    if np_ >= min_path_prob:
                        cands.append((path, ev, path.key + (sig_t,), np_))

        violation_any_mass += new_any_mass

        # 按目标桶合并同签名序列（跨来源桶），再裁剪到 keep_k：
        # 不同前缀（含经自环）作为不同序列保留，但同序列质量合并，规模可控。
        nxt_frontier: dict[tuple, _Bucket] = {}
        for bkey, mass in nxt_mass.items():
            nfp, child_hits = bkey
            nxt = nxt_state[nfp]
            merged: dict[tuple, tuple[_Path, dict, float]] = {}
            for parent, ev, full_key, np_ in nxt_candidates.get(bkey, []):
                old = merged.get(full_key)
                # 用 (父路径, 事件, 概率) 暂存，确认进入 keep_k 后才建节点
                if old is None:
                    merged[full_key] = (parent, ev, np_)
                else:
                    merged[full_key] = (old[0], old[1], old[2] + np_)
            ranked = sorted(merged.values(), key=lambda x: x[2], reverse=True)[:keep_k]
            paths: list[_Path] = []
            for parent, ev, np_ in ranked:
                paths.append(_Path(parent, ev, np_, parent.depth + 1,
                                   parent.key + (tuple(ev["sig"]),)))
            outcome = nxt.get("outcome")
            pending = _outstanding(nxt)
            newly_map = nxt_new_viol.get(bkey)
            newly = (list(newly_map.values()) if newly_map else [])

            if outcome is not None:
                distribution[outcome] = distribution.get(outcome, 0.0) + mass
                exp_by_outcome[outcome] += mass * pending
                if outcome == "failed":
                    for key, cnt in _outstanding_keys(nxt).items():
                        exp_by_key_failed[key] = (
                            exp_by_key_failed.get(key, 0.0) + mass * cnt)
                # 终态轨迹（按结局归类）；同节点首次命中时额外保留一条
                # “首次违反”代表轨迹（合并键含类别，两类轨迹分别参与前 N）。
                for node in paths:
                    add_trace(node, node.prob, outcome, nxt, [])
                if newly:
                    for node in paths:
                        add_trace(node, node.prob, CAT_VIOLATION, nxt, newly)
                continue  # 终态：吸收，不进下一层

            # 非终态首次违反状态：质量继续传播；仅留“首次违反”代表轨迹。
            # 持续为假（newly 为空）时既不累计概率也不重复标注轨迹。
            if newly:
                for node in paths:
                    add_trace(node, node.prob, CAT_VIOLATION, nxt, newly)

            nxt_frontier[bkey] = _Bucket(nxt, mass, paths, child_hits)

        if nxt_frontier:
            max_depth = depth + 1
        # 安全上限按**不同状态指纹**计（命中集合是同状态上的标注，不放大状态空间）
        distinct_states = {fp for fp, _h in nxt_frontier}
        if len(distinct_states) > STATE_CAP:
            state_cap_reached = True
            frontier = nxt_frontier
            break
        frontier = nxt_frontier

    # 边界截断：步数用尽（或状态数触顶）时仍未到达终态的质量；其中已首次
    # 命中过不变量的部分已在首次命中的转移上计入违反概率，这里不重复累计。
    truncated_mass = 0.0
    for _bkey, bucket in frontier.items():
        truncated_mass += bucket.mass
        pending = _outstanding(bucket.state)
        if pending:
            exp_out_truncated += bucket.mass * pending
        current_viols = C.check_invariants(wf, bucket.state)
        for path in bucket.paths:
            add_trace(path, path.prob, CAT_TRUNCATED, bucket.state,
                      current_viols, truncated=True)
    distribution[CAT_TRUNCATED] += truncated_mass

    # 相同完整轨迹（同类别 + 同事件签名序列）合并概率后取前 N；同一序列的
    # “终态轨迹”与“首次违反轨迹”类别不同，分别保留
    merged_traces: dict[tuple, dict] = {}
    for row in trace_rows:
        category, key = row.pop("_key")
        mkey = (category, key)
        old = merged_traces.get(mkey)
        if old is None:
            row["trace_key"] = [list(k) for k in key]
            merged_traces[mkey] = row
        else:
            old["probability"] += row["probability"]
    top_traces = sorted(merged_traces.values(),
                        key=lambda r: r["probability"], reverse=True)[:top_n]
    for i, row in enumerate(top_traces):
        row["rank"] = i + 1
        # 持久化/传输时事件只需公共字段（已是 checker 的公开事件格式）
        row["events"] = [dict(e) for e in row["events"]]

    accounted = (distribution[CAT_SUCCEEDED] + distribution[CAT_COMPENSATED]
                 + distribution[CAT_FAILED] + distribution[CAT_TRUNCATED])
    truncated = truncated_mass > SUM_TOLERANCE or state_cap_reached

    inv_list = [{"id": iid, "name": inv_names[iid], "probability": p}
                for iid, p in sorted(inv_probs.items(), key=lambda kv: kv[1], reverse=True)]
    verdict_text = _verdict_text(bound, distribution, inv_list, truncated,
                                 state_cap_reached, accounted)

    return {
        "verdict": "risk_ok",
        "verdict_text": verdict_text,
        "bound": bound,
        "top_n": top_n,
        "params": {"activities": config_to_json(cfg), "bound": bound, "top_n": top_n},
        "validation": validation,
        "distribution": {
            CAT_SUCCEEDED: distribution[CAT_SUCCEEDED],
            CAT_COMPENSATED: distribution[CAT_COMPENSATED],
            CAT_FAILED: distribution[CAT_FAILED],
            CAT_TRUNCATED: distribution[CAT_TRUNCATED],
            "violation_any": violation_any_mass,
            "invariants": inv_list,
            "accounted": accounted,
            "residual": max(0.0, 1.0 - accounted),
        },
        "expected_outstanding": {
            # 补偿失败导致永久残留（核心风险量）；另附按结局的完整分解
            "failed": exp_by_outcome[CAT_FAILED],
            "truncated": exp_out_truncated,
            "by_outcome": dict(exp_by_outcome),
            "by_key_failed": [{"key": k, "expected": v}
                              for k, v in sorted(exp_by_key_failed.items(),
                                                 key=lambda kv: kv[1], reverse=True)],
        },
        "top_traces": top_traces,
        "stats": {
            "states": len(state_registry),
            "transitions_expanded": transitions,
            "max_depth_reached": max_depth,
            "truncated": truncated,
            "truncated_probability": truncated_mass,
            "state_cap_reached": state_cap_reached,
            "state_cap": STATE_CAP,
            "probability_residual": max(0.0, 1.0 - accounted),
            # 概率分布精确；轨迹列表是归因近似（每桶保留 keep_k 条、
            # 概率低于 min_path_prob 的前缀不参与前 N 排序）
            "trace_approx": True,
            "trace_keep_per_state": keep_k,
            "trace_min_probability": min_path_prob,
        },
        "initial_snapshot": C.snapshot(wf, state0),
    }


def replay_trace(wf: W.Workflow, events: list[dict],
                 init_vars: dict[str, Any] | None = None) -> dict:
    """把风险分析轨迹（事件序列）重放为与检验结果同构的带快照步骤。"""
    state0 = W.initial_state(wf, init_vars)
    steps = C._trace_with_states(wf, events, state0)
    return {
        "verdict": "risk_replay",
        "counterexample": {"length": len(events),
                           "violations": steps[-1]["violations"],
                           "steps": steps},
    }


def _verdict_text(bound: int, dist: dict, inv_list: list[dict], truncated: bool,
                  cap: bool, accounted: float) -> str:
    parts = [f"步数上限 {bound} 的随机故障风险分析完成（结局/违反概率为精确值，"
             f"前 N 轨迹为概率最大的代表性归因）："
             f"成功 {dist[CAT_SUCCEEDED]:.2%}、补偿完成 {dist[CAT_COMPENSATED]:.2%}、"
             f"补偿失败 {dist[CAT_FAILED]:.2%}。"]
    if inv_list:
        top = "、".join(f"{v['name']} {v['probability']:.2%}" for v in inv_list[:3])
        parts.append("不变量违反概率：" + top + "。")
    if truncated:
        reason = "状态数触顶，展开被强制停止" if cap else f"仍有质量在第 {bound} 步未到达终态"
        parts.append(f"⚠ 边界截断：{reason}，"
                     f"截断概率 {dist[CAT_TRUNCATED]:.2%}，该部分结论不成立，请增大步数上限。")
    else:
        parts.append("全部概率质量均在边界内到达终态或暴露违反，无截断。")
    return "".join(parts)
