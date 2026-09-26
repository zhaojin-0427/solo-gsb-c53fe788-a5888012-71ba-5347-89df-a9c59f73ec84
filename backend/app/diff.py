"""版本行为对比：两个已保存模型（基线 / 候选）在同一步数上限下的有界行为差异。

做法：对两个工作流分别复用既有串行转移语义（workflow.successors，透明穿过 ε
转移），从各自初始状态出发做**成对 BFS**：

* 每一层只沿两侧**稳定事件签名 sig 相同**的真实事件对齐展开（sig 即检验器
  用于最短反例排序与重放匹配的 `(类型, 活动, 尝试次数)`，见 workflow._event）；
* 只在一侧可触发的签名产生“轨迹分叉”见证（另一侧无法对齐该事件）；
* **分叉后不立即停止该侧**：每个独有事件触发后，触发侧在**剩余步数上限内**
  继续独立做有界 BFS（按该侧状态指纹去重），另一侧在分叉点持留（hold）；
  续跑到达的每个状态都与持留状态按四类比较。这样独有事件**之后**才出现的
  重调、重复生效、终态/变量/不变量变化（例如基线无“确认后崩溃”、候选启用了
  非幂等的应答丢失：独有恢复事件后重调成功、首次其实已生效而重复）不会被漏检；
* 每个对齐状态对按四类比较：**终态、变量、外部效果、不变量结论**；
* BFS 首次发现保证见证的对齐轨迹最短、同长时事件序字典序最小（可回放）；
* 搜索触及步数边界（对齐展开或任一侧分叉后续跑仍有未展开状态）或状态总数
  安全上限时，结果显式标注「未完备」。

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
# 状态总数安全上限（成对对齐指纹 + 各分叉点单侧续跑访问的指纹），触顶即截断
# 并标注未完备
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


class _CNode:
    """单侧续跑节点：某对齐节点上触发独有事件后，该侧独立 BFS 的一个状态。

    parent 链回溯到续跑起点；events() 给出含独有事件在内的完整单侧事件序列
    （均为真实事件，ε 已在展开时透明穿过）。depth 为相对对齐节点的真实事件数，
    独有事件本身占第 1 步。根节点（独有事件刚执行完）以 _CNODE_ROOT 为父哨兵，
    使 events() 能取到独有事件本身。"""
    __slots__ = ("state", "parent", "ev", "depth")

    def __init__(self, state, parent, ev, depth):
        self.state = state
        self.parent = parent
        self.ev = ev
        self.depth = depth

    def events(self) -> list[dict]:
        evs: list[dict] = []
        cur = self
        while cur is not _CNODE_ROOT:
            evs.append(cur.ev)
            cur = cur.parent
        evs.reverse()
        return evs


_CNODE_ROOT = _CNode(None, None, None, 0)


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
# 分叉后的单侧续跑枚举
# ---------------------------------------------------------------------------

def _explore_continuation(wf_b: W.Workflow, wf_c: W.Workflow,
                          anchor: _PNode, side: str,
                          extra_ev: dict, bound: int,
                          common_inv: list[str], total_states: int,
                          cap: int) -> tuple[dict[str, tuple[_CNode, list[dict]]],
                                             int, bool, bool, int]:
    """独有事件触发后，触发侧在剩余步数内独立做有界 BFS，与另一侧持留状态比较。

    返回：(每类首个最短发现 (_CNode, 变化列表), 续跑访问状态数,
          该续跑是否在步数处截断, 是否触顶, 展开数)。
    BFS 按深度、同层按事件签名排序，因此每类首次发现即“总轨迹最短、事件序
    字典序最小”的单侧见证（持留状态固定为分叉点状态）。
    """
    if side == "baseline":
        wf, wf_held = wf_b, wf_c
        start_state = C._apply_event(wf_b, anchor.sb, extra_ev)
        held_state = anchor.sc
    else:
        wf, wf_held = wf_c, wf_b
        start_state = C._apply_event(wf_c, anchor.sc, extra_ev)
        held_state = anchor.sb
    root = _CNode(start_state, _CNODE_ROOT, extra_ev, 1)
    found: dict[str, tuple[_CNode, list[dict]]] = {}

    def compare(st: dict) -> dict[str, list[dict]]:
        if side == "baseline":
            return _state_changes(wf, st, wf_held, held_state, common_inv)
        return _state_changes(wf_held, held_state, wf, st, common_inv)

    # 续跑起点本身（独有事件刚执行完）先比较一次
    ch0 = compare(root.state)
    for kind in KIND_ORDER:
        if kind in ch0:
            found[kind] = (root, ch0[kind])

    seen = {W.fingerprint(start_state)}
    queue = deque([root])
    bound_hit = False
    visited = 1
    expanded = 0
    cap_hit = False
    max_depth = bound - anchor.depth  # 单侧续跑允许的最大真实事件数

    while queue:
        node = queue.popleft()
        succ = _visible_map(wf, node.state)
        if not succ:
            continue
        if node.depth >= max_depth:
            bound_hit = True
            continue
        expanded += 1
        for sig in sorted(succ):
            nxt, ev = succ[sig]
            fp = W.fingerprint(nxt)
            if fp in seen:
                continue
            seen.add(fp)
            visited += 1
            child = _CNode(nxt, node, ev, node.depth + 1)
            ch = compare(nxt)
            for kind in KIND_ORDER:
                if kind in ch and kind not in found:
                    found[kind] = (child, ch[kind])
            queue.append(child)
            if total_states + visited > cap:
                return found, visited, True, True, expanded

    return found, visited, bound_hit, cap_hit, expanded


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
                            extra_events: list[dict],
                            changes_here: list[dict] | None = None) -> dict:
    """物化一个单侧续跑见证：对齐到 node 后，触发侧执行 extra_events（首个即
    独有事件，其余为分叉后该侧独立续跑的真实事件），另一侧在分叉点持续持留。"""
    pairs = _walk_pairs(node)
    events_b = [p[0] for p in pairs]
    events_c = [p[1] for p in pairs]
    steps_b = _side_steps(wf_b, events_b, state0_b, "baseline")
    steps_c = _side_steps(wf_c, events_c, state0_c, "candidate")
    diverge_index = node.depth + 1
    extra_ev = extra_events[0]

    def _hold_step(template: dict, idx: int) -> dict:
        hold = dict(template)
        hold["index"] = idx
        hold["align_index"] = idx
        hold["event"] = None
        hold["hold"] = True
        hold["alternatives"] = []
        return hold

    mark_diff = changes_here is not None
    if side == "baseline":
        more = C._trace_with_states(wf_b, extra_events, node.sb)
        more_steps = more[1:]
        for k, st in enumerate(more_steps):
            idx = diverge_index + k
            st["index"] = idx
            st["align_index"] = idx
            st["side"] = "baseline"
            if k == 0:
                st["divergence"] = True
            # 裸分叉见证：分叉步即差异点；续跑差异见证：差异在续跑末步成立
            if (mark_diff and k == len(more_steps) - 1) or (not mark_diff and k == 0):
                st["diff_point"] = True
            steps_b.append(st)
            steps_c.append(_hold_step(steps_c[-1], idx))
        final_state = _replay_states(wf_b, node.sb, extra_events)
        other_state = node.sc
    else:
        more = C._trace_with_states(wf_c, extra_events, node.sc)
        more_steps = more[1:]
        for k, st in enumerate(more_steps):
            idx = diverge_index + k
            st["index"] = idx
            st["align_index"] = idx
            st["side"] = "candidate"
            if k == 0:
                st["divergence"] = True
            if (mark_diff and k == len(more_steps) - 1) or (not mark_diff and k == 0):
                st["diff_point"] = True
            steps_c.append(st)
            steps_b.append(_hold_step(steps_b[-1], idx))
        final_state = _replay_states(wf_c, node.sc, extra_events)
        other_state = node.sb

    cont_len = len(extra_events)
    continuation = cont_len > 1
    detail = (f"对齐到第 {node.depth} 步后，事件「{extra_ev.get('label', extra_ev['type'])}"
              f"（{extra_ev.get('activity') or '—'}）只在"
              + ("基线" if side == "baseline" else "候选")
              + "一侧可触发，另一侧不存在该交错，轨迹在此分叉")
    if continuation:
        detail += (f"；触发侧在分叉后继续独立推进 {cont_len - 1} 步"
                   "（另一侧在分叉点持留），差异在续跑到达的状态上成立")
    return {
        "kind": KIND_DIVERGENCE,
        "kind_label": KIND_LABELS[KIND_DIVERGENCE],
        "length": node.depth + cont_len,
        "diverge_index": diverge_index,
        "side": side,
        "event": dict(extra_ev),
        "sig": list(extra_ev["sig"]),
        "detail": detail,
        "align_path": [list(p[0]["sig"]) for p in pairs],
        "baseline_steps": steps_b,
        "candidate_steps": steps_c,
        "terminal_after": {"baseline": node.sb.get("outcome"),
                           "candidate": node.sc.get("outcome")},
        "diverging_terminal": final_state.get("outcome"),
        "held_terminal": other_state.get("outcome"),
        "continuation": continuation,
        "continuation_steps": cont_len - 1,
    }


def _replay_states(wf: W.Workflow, start: dict, events: list[dict]) -> dict:
    state = start
    for ev in events:
        state = C._apply_event(wf, state, ev)
    return state


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
    # 独有事件触发并单侧续跑后产生的四类状态变化：每类各自保留最短见证
    # kind -> (anchor _PNode, side, 单侧事件序列, 变化列表)
    divergence_kind: dict[str, tuple[_PNode, str, list[dict], list[dict]]] = {}
    pairs_visited = 1
    aligned_expanded = 0
    divergence_count = 0
    terminal_pairs = 0
    max_depth = 0
    bound_reached = False            # 对齐展开在步数边界处仍有未展开状态对
    continuation_bound_reached = False  # 任一分叉的单侧续跑在步数边界处被截断
    state_cap_reached = False
    continuation_states = 0         # 各分叉点单侧续跑访问的状态总数（去重后）
    continuation_expansions = 0
    continuation_points = 0         # 启动过单侧续跑的独有事件数

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

        # 单侧签名：轨迹分叉。首个分叉作为总见证；每个独有事件触发后，触发侧在
        # 剩余步数内继续独立枚举（另一侧在分叉点持留），续跑到达的每个状态都与
        # 持留状态按四类比较——独有事件之后才出现的重调、重复生效、终态/变量/
        # 不变量变化由此进入报告，而不是只看独有事件的下一步就停止该侧。
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
            def _run_one_side(side: str, uniq_sigs: list[tuple], mmap: dict) -> None:
                nonlocal continuation_states, continuation_expansions
                nonlocal continuation_points, continuation_bound_reached
                nonlocal state_cap_reached
                for sig in uniq_sigs:
                    if state_cap_reached:
                        return
                    extra_ev = mmap[sig][1]
                    continuation_points += 1
                    cfound, visited, c_bound, cap_hit, expanded = _explore_continuation(
                        wf_b, wf_c, node, side, extra_ev, bound, common_inv,
                        len(seen) + continuation_states, STATE_CAP)
                    continuation_states += visited
                    continuation_expansions += expanded
                    if c_bound:
                        continuation_bound_reached = True
                    if cap_hit:
                        state_cap_reached = True
                        return
                    # 与已有同类分叉见证比较，保留总轨迹最短、事件序最小者
                    for kind, (cnode, changes_here) in cfound.items():
                        cand_events = cnode.events()
                        cand_key = (node.depth + cnode.depth,
                                    tuple(tuple(e["sig"]) for e in cand_events))
                        prev = divergence_kind.get(kind)
                        if prev is None:
                            divergence_kind[kind] = (node, side, cand_events, changes_here)
                            continue
                        pnode, pside, pevents, _pch = prev
                        prev_key = (pnode.depth + len(pevents),
                                    tuple(tuple(e["sig"]) for e in pevents))
                        if cand_key < prev_key:
                            divergence_kind[kind] = (node, side, cand_events, changes_here)

            _run_one_side("baseline", only_b, mb)
            if not state_cap_reached:
                _run_one_side("candidate", only_c, mc)

        if len(seen) + continuation_states > STATE_CAP:
            state_cap_reached = True
        if state_cap_reached:
            break

    truncated = bound_reached or continuation_bound_reached or state_cap_reached

    # 物化各类最短见证
    witnesses: dict[str, dict] = {}
    for kind, (node, changes_here) in found.items():
        witnesses[kind] = _materialize_state_witness(
            wf_b, state0_b, wf_c, state0_c, node, kind, changes_here)
    # 分叉产生的变化只在对齐展开找不到该类见证时补充（对齐见证优先）
    for kind, (node, side, extra_events, changes_here) in divergence_kind.items():
        if kind in found:
            continue
        w = _materialize_divergence(
            wf_b, state0_b, wf_c, state0_c, node, side, extra_events, changes_here)
        w["kind"] = kind
        suffix = "（随轨迹分叉产生；见证为该分叉的单侧续跑）" if w["continuation"] \
            else "（随轨迹分叉产生；见证为该分叉）"
        w["kind_label"] = KIND_LABELS[kind] + suffix
        w["changes"] = changes_here
        w["via_divergence"] = True
        witnesses[kind] = w
    divergence_witness = None
    if divergence is not None:
        dnode, side, ev = divergence
        divergence_witness = _materialize_divergence(
            wf_b, state0_b, wf_c, state0_c, dnode, side, [ev])

    primary_kind = next((k for k in KIND_ORDER if k in witnesses), None)
    has_diff = bool(found) or bool(divergence_kind) or divergence_witness is not None

    incompleteness: list[str] = []
    if bound_reached:
        incompleteness.append(
            f"到达步数边界 {bound}：仍有对齐轨迹未展开，"
            "边界之外可能存在未被发现的差异，本报告结果未完备；可增大步数上限复检。")
    if continuation_bound_reached and not bound_reached:
        incompleteness.append(
            f"到达步数边界 {bound}：某条单侧独有事件后的续跑仍有未展开状态"
            "（重调/后续分支可能未穷尽），该分叉之外的差异未完备；可增大步数上限复检。")
    if state_cap_reached:
        incompleteness.append(
            f"状态总数（对齐状态对 + 单侧续跑状态）超过安全上限 {STATE_CAP}，"
            "展开被强制停止，结果未完备。")

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
        "continuation_points": continuation_points,
        "continuation_states": continuation_states,
        "continuation_expansions": continuation_expansions,
        "continuation_bound_reached": continuation_bound_reached,
        "total_states": pairs_visited + continuation_states,
        "truncated": truncated,
        "bound_reached": bound_reached,
        "state_cap_reached": state_cap_reached,
        "state_cap": STATE_CAP,
    }
    return {
        "verdict": verdict,
        "verdict_text": _verdict_text(verdict, witnesses, divergence_witness, bound,
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


def _verdict_text(verdict: str, witnesses: dict, divergence: dict | None,
                  bound: int, incompleteness: list[str]) -> str:
    kinds = []
    for k in KIND_ORDER:
        if k in witnesses:
            kinds.append(KIND_LABELS[k])
    if divergence is not None:
        kinds.append(KIND_LABELS[KIND_DIVERGENCE])
    if verdict == "identical":
        return (f"在同一步数上限 {bound} 内，基线与候选的可达状态按稳定事件签名逐一对齐"
                "（含每条单侧独有事件触发后的续跑状态），终态、变量、外部效果与共同启用的"
                "不变量结论均无差异，且搜索已穷尽边界内全部对齐与单侧续跑状态。"
                "该结论仅在该步数边界内成立。")
    head = ("发现版本行为差异：" + "、".join(kinds) + "。"
            "每类差异均给出按最短轨迹、稳定事件序选出的可回放见证。")
    if verdict == "diff_found_incomplete":
        head += "⚠ 但搜索触及边界，" + "；".join(incompleteness)
    elif verdict == "inconclusive":
        head = (f"在步数上限 {bound} 内未发现差异，但搜索被截断，无法据此断言两版本行为相同。"
                + "；".join(incompleteness))
    return head
