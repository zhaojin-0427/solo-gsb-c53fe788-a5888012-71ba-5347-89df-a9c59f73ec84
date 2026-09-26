"""版本行为对比：两个已保存模型（基线 / 候选）在同一步数上限下的有界行为差异。

做法：对两个工作流分别复用既有串行转移语义（workflow.successors，透明穿过 ε
转移），从各自初始状态出发做**成对 BFS**：

* 每一层只沿两侧**稳定事件签名 sig 相同**的真实事件对齐展开（sig 即检验器
  用于最短反例排序与重放匹配的 `(类型, 活动, 尝试次数)`，见 workflow._event）；
* 只在一侧可触发的签名产生“轨迹分叉”见证（另一侧无法对齐该事件）；
* 每个对齐状态对按四类比较：**终态、变量、外部效果、不变量结论**；
* BFS 首次发现保证见证的对齐轨迹最短、同长时事件序字典序最小（可回放）；
* 搜索触及步数边界或状态对安全上限时，结果显式标注「未完备」。

对比使用与「有界检验」完全相同的串行转移系统；声明了 fork/join 汇合组的模型
在此按其串行结构枚举（不枚举部分序交错），这一约定会写入报告，避免与并发
分析的结论混淆。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from . import checker as C
from . import workflow as W

MAX_DIFF_BOUND = 80
DEFAULT_DIFF_BOUND = 16
# 成对状态空间安全上限（指纹对数量），触顶即截断并标注未完备
STATE_CAP = 200_000

# 四类状态差异（展示与稳定优先级：不变量 > 终态 > 外部效果 > 变量）
KIND_TERMINAL = "terminal"
KIND_VARIABLES = "variables"
KIND_EFFECTS = "effects"
KIND_INVARIANT = "invariant"
KIND_DIVERGENCE = "divergence"

KIND_LABELS = {
    KIND_TERMINAL: "终态变化",
    KIND_VARIABLES: "变量变化",
    KIND_EFFECTS: "外部效果变化",
    KIND_INVARIANT: "不变量结论变化",
    KIND_DIVERGENCE: "轨迹分叉（事件只在一侧可触发）",
}
KIND_ORDER = (KIND_INVARIANT, KIND_TERMINAL, KIND_EFFECTS, KIND_VARIABLES)

_OUTCOME_LABELS = {
    None: "非终态（仍在运行）",
    "succeeded": "成功完成",
    "compensated": "已完全补偿",
    "failed": "失败（有外部效果永久残留）",
}

INV_BUILTIN_NAMES = {
    C.INV_AT_MOST_ONCE: "至多一次生效",
    C.INV_UNDONE: "失败后最终撤销",
}


class DiffError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 成对 BFS 数据结构
# ---------------------------------------------------------------------------

class _PNode:
    """对齐状态对节点。ev_b/ev_c 是从父状态对到达本节点时两侧各自执行的事件，
    二者 sig 相同（对齐步）。"""
    __slots__ = ("sb", "sc", "parent", "ev_b", "ev_c", "depth")

    def __init__(self, sb, sc, parent, ev_b, ev_c, depth):
        self.sb = sb
        self.sc = sc
        self.parent = parent
        self.ev_b = ev_b
        self.ev_c = ev_c
        self.depth = depth


def _visible_map(wf: W.Workflow, state: dict) -> dict[tuple, tuple[dict, dict]]:
    """当前状态的可见后继，按事件签名索引（透明穿过 ε 转移）。

    与 checker._visible_successors 同源：真实事件消耗 1 个对齐步，ε 不消耗。
    """
    out: dict[tuple, tuple[dict, dict]] = {}
    for nxt, ev in C._visible_successors(wf, state):
        out.setdefault(tuple(ev["sig"]), (nxt, ev))
    return out


def _walk_pairs(node: _PNode) -> list[tuple[dict | None, dict | None]]:
    """从根到该节点的对齐事件对（不含初始状态）。"""
    pairs: list[tuple[dict | None, dict | None]] = []
    cur = node
    while cur.parent is not None:
        pairs.append((cur.ev_b, cur.ev_c))
        cur = cur.parent
    pairs.reverse()
    return pairs


# ---------------------------------------------------------------------------
# 差异计算
# ---------------------------------------------------------------------------

def _effect_summary(state: dict) -> dict[str, dict]:
    """外部效果的规范摘要：按效果键统计 生效中 / 已撤销 / 生效中重复 条数。"""
    summary: dict[str, dict] = {}
    for e in state["effects"]:
        row = summary.setdefault(e["key"], {"live": 0, "undone": 0, "duplicate": 0})
        if e["undone"]:
            row["undone"] += 1
        else:
            row["live"] += 1
            if e.get("duplicate"):
                row["duplicate"] += 1
    return summary


def _common_invariant_ids(wf_b: W.Workflow, wf_c: W.Workflow
                          ) -> tuple[list[str], list[str], list[str]]:
    """两侧共同启用的不变量（按稳定 id 对齐），以及只在一侧配置的不变量名。"""
    only_b: list[str] = []
    only_c: list[str] = []
    common: list[str] = []
    if wf_b.invariant_at_most_once and wf_c.invariant_at_most_once:
        common.append(C.INV_AT_MOST_ONCE)
    elif wf_b.invariant_at_most_once:
        only_b.append(INV_BUILTIN_NAMES[C.INV_AT_MOST_ONCE])
    elif wf_c.invariant_at_most_once:
        only_c.append(INV_BUILTIN_NAMES[C.INV_AT_MOST_ONCE])
    if wf_b.invariant_undo_on_failure and wf_c.invariant_undo_on_failure:
        common.append(C.INV_UNDONE)
    elif wf_b.invariant_undo_on_failure:
        only_b.append(INV_BUILTIN_NAMES[C.INV_UNDONE])
    elif wf_c.invariant_undo_on_failure:
        only_c.append(INV_BUILTIN_NAMES[C.INV_UNDONE])
    customs_b = set(wf_b.invariants)
    customs_c = set(wf_c.invariants)
    only_b += [f"自定义不变量「{n}」" for n in sorted(customs_b - customs_c)]
    only_c += [f"自定义不变量「{n}」" for n in sorted(customs_c - customs_b)]
    common += [f"custom:{n}" for n in sorted(customs_b & customs_c)]
    return common, only_b, only_c


def _state_changes(wf_b: W.Workflow, sb: dict,
                   wf_c: W.Workflow, sc: dict,
                   common_inv: list[str]) -> dict[str, list[dict]]:
    """比较一个对齐状态对，返回按类别分组的差异（空 dict 表示无差异）。"""
    changes: dict[str, list[dict]] = {}

    # 1) 终态
    ob, oc = sb.get("outcome"), sc.get("outcome")
    if ob != oc:
        changes[KIND_TERMINAL] = [{
            "kind": KIND_TERMINAL,
            "base_outcome": ob,
            "candidate_outcome": oc,
            "base_label": _OUTCOME_LABELS[ob],
            "candidate_label": _OUTCOME_LABELS[oc],
            "detail": f"基线为「{_OUTCOME_LABELS[ob]}」，候选为「{_OUTCOME_LABELS[oc]}」",
        }]

    # 2) 变量（按变量名对齐：changed / added / removed）
    var_changes: list[dict] = []
    for name in sorted(set(sb["variables"]) | set(sc["variables"])):
        in_b, in_c = name in sb["variables"], name in sc["variables"]
        vb, vc = sb["variables"].get(name), sc["variables"].get(name)
        if in_b and in_c:
            if vb != vc:
                var_changes.append({"kind": KIND_VARIABLES, "name": name,
                                    "change": "changed", "base": vb, "candidate": vc,
                                    "detail": f"变量 {name}：{vb} → {vc}"})
        elif in_b:
            var_changes.append({"kind": KIND_VARIABLES, "name": name,
                                "change": "removed", "base": vb, "candidate": None,
                                "detail": f"变量 {name} 在候选中不存在（基线值 {vb}）"})
        else:
            var_changes.append({"kind": KIND_VARIABLES, "name": name,
                                "change": "added", "base": None, "candidate": vc,
                                "detail": f"变量 {name} 只在候选中出现（值 {vc}）"})
    if var_changes:
        changes[KIND_VARIABLES] = var_changes

    # 3) 外部效果（生效中 / 已撤销 / 重复 计数按效果键对齐）
    eb, ec = _effect_summary(sb), _effect_summary(sc)
    eff_changes: list[dict] = []
    for key in sorted(set(eb) | set(ec)):
        rb, rc = eb.get(key, {"live": 0, "undone": 0, "duplicate": 0}), \
                 ec.get(key, {"live": 0, "undone": 0, "duplicate": 0})
        if rb != rc:
            eff_changes.append({
                "kind": KIND_EFFECTS, "key": key,
                "base": rb, "candidate": rc,
                "detail": (f"外部效果「{key}」：生效中 {rb['live']}→{rc['live']}，"
                           f"已撤销 {rb['undone']}→{rc['undone']}，"
                           f"其中重复 {rb['duplicate']}→{rc['duplicate']}"),
            })
    if eff_changes:
        changes[KIND_EFFECTS] = eff_changes

    # 4) 不变量结论（仅比较两侧共同启用的不变量；配置差异另行报告）
    vb_map = {v["id"]: v for v in C.check_invariants(wf_b, sb)}
    vc_map = {v["id"]: v for v in C.check_invariants(wf_c, sc)}
    inv_changes: list[dict] = []
    for iid in common_inv:
        in_b, in_c = iid in vb_map, iid in vc_map
        if in_b != in_c:
            name = INV_BUILTIN_NAMES.get(iid) or (vb_map.get(iid) or vc_map.get(iid))["name"]
            inv_changes.append({
                "kind": KIND_INVARIANT, "id": iid, "name": name,
                "base_violated": in_b, "candidate_violated": in_c,
                "base_detail": vb_map.get(iid, {}).get("detail"),
                "candidate_detail": vc_map.get(iid, {}).get("detail"),
                "detail": (f"不变量「{name}」：基线"
                           + ("被破坏" if in_b else "成立")
                           + "，候选" + ("被破坏" if in_c else "成立")),
            })
    if inv_changes:
        changes[KIND_INVARIANT] = inv_changes
    return changes


# ---------------------------------------------------------------------------
# 见证物化（带快照的可回放步骤）
# ---------------------------------------------------------------------------

def _side_steps(wf: W.Workflow, events: list[dict], state0: dict,
                side: str) -> list[dict]:
    steps = C._trace_with_states(wf, events, state0)
    for i, st in enumerate(steps):
        st["align_index"] = i
        st["side"] = side
    return steps


def _materialize_state_witness(wf_b: W.Workflow, state0_b: dict,
                               wf_c: W.Workflow, state0_c: dict,
                               node: _PNode, kind: str,
                               changes_here: list[dict]) -> dict:
    pairs = _walk_pairs(node)
    events_b = [p[0] for p in pairs]
    events_c = [p[1] for p in pairs]
    steps_b = _side_steps(wf_b, events_b, state0_b, "baseline")
    steps_c = _side_steps(wf_c, events_c, state0_c, "candidate")
    steps_b[-1]["diff_point"] = True
    steps_c[-1]["diff_point"] = True
    return {
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "length": node.depth,
        "changes": changes_here,
        "align_path": [list(p[0]["sig"]) for p in pairs],
        "baseline_steps": steps_b,
        "candidate_steps": steps_c,
    }


def _materialize_divergence(wf_b: W.Workflow, state0_b: dict,
                            wf_c: W.Workflow, state0_c: dict,
                            node: _PNode, side: str,
                            extra_ev: dict) -> dict:
    pairs = _walk_pairs(node)
    events_b = [p[0] for p in pairs]
    events_c = [p[1] for p in pairs]
    steps_b = _side_steps(wf_b, events_b, state0_b, "baseline")
    steps_c = _side_steps(wf_c, events_c, state0_c, "candidate")
    diverge_index = node.depth + 1
    # 触发侧：真实执行该独有事件（物化时会校验它在该状态确实可触发）
    if side == "baseline":
        nxt = C._apply_event(wf_b, node.sb, extra_ev)
        more_b = C._trace_with_states(wf_b, [extra_ev], node.sb)
        step = more_b[-1]
        step["index"] = diverge_index
        step["align_index"] = diverge_index
        step["side"] = "baseline"
        step["divergence"] = True
        step["diff_point"] = True
        steps_b.append(step)
        hold = dict(steps_c[-1])
    else:
        nxt = C._apply_event(wf_c, node.sc, extra_ev)
        more_c = C._trace_with_states(wf_c, [extra_ev], node.sc)
        step = more_c[-1]
        step["index"] = diverge_index
        step["align_index"] = diverge_index
        step["side"] = "candidate"
        step["divergence"] = True
        step["diff_point"] = True
        steps_c.append(step)
        hold = dict(steps_b[-1])
    # 另一侧：在分叉点保持（hold），不推进
    hold["index"] = diverge_index
    hold["align_index"] = diverge_index
    hold["event"] = None
    hold["hold"] = True
    hold["alternatives"] = []
    held_side = "candidate" if side == "baseline" else "baseline"
    hold["side"] = held_side
    if side == "baseline":
        steps_c.append(hold)
    else:
        steps_b.append(hold)
    other_state = node.sc if side == "baseline" else node.sb
    return {
        "kind": KIND_DIVERGENCE,
        "kind_label": KIND_LABELS[KIND_DIVERGENCE],
        "length": node.depth,
        "diverge_index": diverge_index,
        "side": side,
        "event": dict(extra_ev),
        "sig": list(extra_ev["sig"]),
        "detail": (f"对齐到第 {node.depth} 步后，事件「{extra_ev.get('label', extra_ev['type'])}"
                   f"（{extra_ev.get('activity') or '—'}）只在"
                   + ("基线" if side == "baseline" else "候选")
                   + "一侧可触发，另一侧不存在该交错，轨迹在此分叉"),
        "align_path": [list(p[0]["sig"]) for p in pairs],
        "baseline_steps": steps_b,
        "candidate_steps": steps_c,
        "terminal_after": {"baseline": node.sb.get("outcome"),
                           "candidate": node.sc.get("outcome")},
        "diverging_terminal": nxt.get("outcome"),
        "held_terminal": other_state.get("outcome"),
    }


# ---------------------------------------------------------------------------
# 主对比过程
# ---------------------------------------------------------------------------

def run_diff(wf_b: W.Workflow, wf_c: W.Workflow,
             bound: int = DEFAULT_DIFF_BOUND,
             init_vars_b: dict[str, Any] | None = None,
             init_vars_c: dict[str, Any] | None = None) -> dict:
    bound = min(max(int(bound), 1), MAX_DIFF_BOUND)
    state0_b = W.initial_state(wf_b, init_vars_b)
    state0_c = W.initial_state(wf_c, init_vars_c)
    common_inv, only_b_inv, only_c_inv = _common_invariant_ids(wf_b, wf_c)

    root = _PNode(state0_b, state0_c, None, None, None, 0)
    root_changes = _state_changes(wf_b, state0_b, wf_c, state0_c, common_inv)

    # kind -> (_PNode, 该节点该类别的变化列表)
    found: dict[str, tuple[_PNode, list[dict]]] = {}
    for kind in KIND_ORDER:
        if kind in root_changes:
            found[kind] = (root, root_changes[kind])

    seen = {(W.fingerprint(state0_b), W.fingerprint(state0_c))}
    queue: deque[_PNode] = deque([root])
    # 轨迹分叉：首个分叉（最短、事件序最小）单独保留
    divergence: tuple[_PNode, str, dict] | None = None
    # 独有事件触发后产生的四类状态变化：每类各自保留最短分叉见证
    # kind -> (_PNode, side, extra_ev, 变化列表)
    divergence_kind: dict[str, tuple[_PNode, str, dict, list[dict]]] = {}
    pairs_visited = 1
    aligned_expanded = 0
    divergence_count = 0
    terminal_pairs = 0
    max_depth = 0
    bound_reached = False
    state_cap_reached = False

    def _post_change(node: _PNode, side: str, extra_ev: dict
                     ) -> tuple[dict, dict[str, list[dict]]]:
        """执行单侧独有事件后与另一侧持留状态比较（返回新状态与差异）。"""
        if side == "baseline":
            nxt = C._apply_event(wf_b, node.sb, extra_ev)
            ch = _state_changes(wf_b, nxt, wf_c, node.sc, common_inv)
        else:
            nxt = C._apply_event(wf_c, node.sc, extra_ev)
            ch = _state_changes(wf_b, node.sb, wf_c, nxt, common_inv)
        return nxt, {k: v for k, v in ch.items() if k in KIND_ORDER}

    while queue:
        node = queue.popleft()
        mb = _visible_map(wf_b, node.sb)
        mc = _visible_map(wf_c, node.sc)
        if not mb and not mc:
            terminal_pairs += 1
            continue
        if node.depth >= bound:
            bound_reached = True
            continue
        aligned_expanded += 1

        # 共同签名：逐层对齐展开（sorted 保证同深度下字典序最小的先入队）
        for sig in sorted(set(mb) & set(mc)):
            nsb, ev_b = mb[sig]
            nsc, ev_c = mc[sig]
            key = (W.fingerprint(nsb), W.fingerprint(nsc))
            if key in seen:
                continue
            seen.add(key)
            pairs_visited += 1
            child = _PNode(nsb, nsc, node, ev_b, ev_c, node.depth + 1)
            max_depth = max(max_depth, child.depth)
            ch = _state_changes(wf_b, nsb, wf_c, nsc, common_inv)
            for kind in KIND_ORDER:
                if kind in ch and kind not in found:
                    found[kind] = (child, ch[kind])
            queue.append(child)

        # 单侧签名：轨迹分叉。首个分叉作为总见证；每个独有事件执行后与另一侧
        # 持留状态比较，四类变化分别按“最短、事件序最小”各保留一个分叉见证。
        only_b = sorted(set(mb) - set(mc))
        only_c = sorted(set(mc) - set(mb))
        if only_b or only_c:
            divergence_count += len(only_b) + len(only_c)
            if divergence is None:
                candidates = [(sig, "baseline", mb[sig][1]) for sig in only_b]
                candidates += [(sig, "candidate", mc[sig][1]) for sig in only_c]
                candidates.sort(key=lambda x: x[0])
                sig0, side0, ev0 = candidates[0]
                divergence = (node, side0, ev0)
            for sig in only_b:
                _, pch = _post_change(node, "baseline", mb[sig][1])
                for kind, changes_here in pch.items():
                    divergence_kind.setdefault(
                        kind, (node, "baseline", mb[sig][1], changes_here))
            for sig in only_c:
                _, pch = _post_change(node, "candidate", mc[sig][1])
                for kind, changes_here in pch.items():
                    divergence_kind.setdefault(
                        kind, (node, "candidate", mc[sig][1], changes_here))

        if len(seen) > STATE_CAP:
            state_cap_reached = True
            break

    truncated = bound_reached or state_cap_reached

    # 物化各类最短见证
    witnesses: dict[str, dict] = {}
    for kind, (node, changes_here) in found.items():
        witnesses[kind] = _materialize_state_witness(
            wf_b, state0_b, wf_c, state0_c, node, kind, changes_here)
    # 分叉产生的变化只在对齐展开找不到该类见证时补充（对齐见证优先）
    for kind, (node, side, ev, changes_here) in divergence_kind.items():
        if kind in found:
            continue
        w = _materialize_divergence(
            wf_b, state0_b, wf_c, state0_c, node, side, ev)
        w["kind"] = kind
        w["kind_label"] = KIND_LABELS[kind] + "（随轨迹分叉产生；见证为该分叉）"
        w["changes"] = changes_here
        w["via_divergence"] = True
        w["length"] = node.depth + 1
        witnesses[kind] = w
    divergence_witness = None
    if divergence is not None:
        dnode, side, ev = divergence
        divergence_witness = _materialize_divergence(
            wf_b, state0_b, wf_c, state0_c, dnode, side, ev)

    primary_kind = next((k for k in KIND_ORDER if k in witnesses), None)
    has_diff = bool(found) or bool(divergence_kind) or divergence_witness is not None

    incompleteness: list[str] = []
    if bound_reached:
        incompleteness.append(
            f"到达步数边界 {bound}：仍有对齐（或单侧）轨迹未展开，"
            "边界之外可能存在未被发现的差异，本报告结果未完备；可增大步数上限复检。")
    if state_cap_reached:
        incompleteness.append(
            f"成对状态数超过安全上限 {STATE_CAP}，展开被强制停止，结果未完备。")

    acts_b, acts_c = set(wf_b.activities), set(wf_c.activities)
    structure = {
        "activities_only_baseline": sorted(acts_b - acts_c),
        "activities_only_candidate": sorted(acts_c - acts_b),
        "common_activities": sorted(acts_b & acts_c),
    }
    invariant_config = {"only_baseline": only_b_inv, "only_candidate": only_c_inv}

    if has_diff and not truncated:
        verdict = "diff_found"
    elif has_diff and truncated:
        verdict = "diff_found_incomplete"
    elif truncated:
        verdict = "inconclusive"
    else:
        verdict = "identical"

    stats = {
        "pairs_visited": pairs_visited,
        "aligned_expansions": aligned_expanded,
        "divergence_points": divergence_count,
        "terminal_pairs": terminal_pairs,
        "max_depth_reached": max_depth,
        "truncated": truncated,
        "bound_reached": bound_reached,
        "state_cap_reached": state_cap_reached,
        "state_cap": STATE_CAP,
    }
    return {
        "verdict": verdict,
        "verdict_text": _verdict_text(verdict, found, divergence_witness, bound,
                                     incompleteness),
        "bound": bound,
        "stats": stats,
        "incompleteness": incompleteness,
        "primary_kind": primary_kind,
        "witnesses": witnesses,
        "divergence": divergence_witness,
        "structure": structure,
        "invariant_config_diff": invariant_config,
        "semantics_note": "对比复用有界检验的串行转移语义（按稳定事件签名对齐）；"
                          "声明了 fork/join 汇合组的模型不枚举部分序并发交错，"
                          "并发行为差异请以「并发调度安全」分析为准。",
        "initial_snapshots": {
            "baseline": C.snapshot(wf_b, state0_b),
            "candidate": C.snapshot(wf_c, state0_c),
        },
    }


def _verdict_text(verdict: str, found: dict, divergence: dict | None,
                  bound: int, incompleteness: list[str]) -> str:
    kinds = []
    for k in KIND_ORDER:
        if k in found:
            kinds.append(KIND_LABELS[k])
    if divergence is not None:
        kinds.append(KIND_LABELS[KIND_DIVERGENCE])
    if verdict == "identical":
        return (f"在同一步数上限 {bound} 内，基线与候选的可达状态按稳定事件签名逐一对齐，"
                "终态、变量、外部效果与共同启用的不变量结论均无差异，且搜索已穷尽边界内"
                "全部对齐状态。该结论仅在该步数边界内成立。")
    head = ("发现版本行为差异：" + "、".join(kinds) + "。"
            "每类差异均给出按最短对齐轨迹、稳定事件序选出的可回放见证。")
    if verdict == "diff_found_incomplete":
        head += "⚠ 但搜索触及边界，" + "；".join(incompleteness)
    elif verdict == "inconclusive":
        head = (f"在步数上限 {bound} 内未发现差异，但搜索被截断，无法据此断言两版本行为相同。"
                + "；".join(incompleteness))
    return head
