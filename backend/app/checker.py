"""有界显式状态模型检验器。

策略：BFS 枚举成功/失败/重试/崩溃恢复/补偿的交错；按状态指纹合并等价分支。
不变量在“转移产生的每个新状态”上检查，因此 BFS 第一次发现的违反一定是
轨迹最短、且按事件签名 (kind, activity, attempt) 字典序最小的反例。

这是**有界**检验：只能在给定步数内给出确定结论，不宣称全局正确。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from . import expressions as E
from . import workflow as W

MAX_BOUND = 80
DEFAULT_BOUND = 16

# 不变量在 UI 上的固定展示顺序（同一状态多违反时的稳定优先级）
INV_AT_MOST_ONCE = "at_most_once"
INV_UNDONE = "undo_on_failure"


class CheckError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 快照（供前端逐步展示）
# ---------------------------------------------------------------------------

def snapshot(wf: W.Workflow, state: dict) -> dict:
    c = W._ctx(state)
    return {
        "phase": state["phase"],
        "current": state.get("current"),
        "outcome": state.get("outcome"),
        "activities": [
            {
                "id": aid,
                "name": wf.act(aid).name,
                "status": state["status"][aid],
                "tries": state["tries"][aid],
                "effect": wf.act(aid).effect,
            }
            for aid in sorted(wf.activities)
        ],
        "variables": dict(sorted(state["variables"].items())),
        "effects": [
            {"key": e["key"], "activity": e["activity"], "undone": bool(e["undone"]),
             "duplicate": bool(e.get("duplicate"))}
            for e in state["effects"]
        ],
        "counts": {"succ": c["succ"], "effects": c["effects"], "undone": c["undone"]},
        "comp_stack": [state["effects"][i]["activity"] for i in state.get("comp_stack", [])],
        "comp_tries": dict(state.get("comp_tries", {})),
    }


def check_invariants(wf: W.Workflow, state: dict) -> list[dict]:
    """返回当前状态破坏的不变量列表（稳定顺序）。"""
    violations: list[dict] = []

    if wf.invariant_at_most_once:
        dup_keys = []
        for e in state["effects"]:
            if not e["undone"] and e.get("duplicate"):
                dup_keys.append(e["key"])
        if dup_keys:
            key = dup_keys[0]
            violations.append({
                "id": INV_AT_MOST_ONCE,
                "name": "至多一次生效",
                "detail": (f"外部效果「{key}」被重复生效：超时重试后发现首次调用其实也已完成"
                           "（非幂等活动的“至少一次”交付），当前存在多条未撤销的生效记录"),
                "effect": key,
            })

    if wf.invariant_undo_on_failure and state.get("outcome") == "failed":
        pending = [e["key"] for e in state["effects"] if not e["undone"]]
        if pending:
            violations.append({
                "id": INV_UNDONE,
                "name": "失败后最终撤销",
                "detail": "工作流失败，但仍有外部效果未撤销：" + ", ".join(pending),
                "effects": pending,
            })

    funcs = W._z3_functions(state)
    for name, node in wf.invariants.items():
        try:
            if not bool(E.eval_node(node, state["variables"], funcs)):
                violations.append({
                    "id": f"custom:{name}",
                    "name": name,
                    "detail": f"自定义不变量「{name}」在当前状态下为假",
                })
        except E.ExpressionError as ex:
            raise CheckError(f"不变量「{name}」求值失败：{ex}") from ex

    return violations


# ---------------------------------------------------------------------------
# 主检验过程
# ---------------------------------------------------------------------------

class _Node:
    __slots__ = ("state", "fp", "parent", "event", "depth")

    def __init__(self, state, fp, parent, event, depth):
        self.state = state
        self.fp = fp
        self.parent = parent
        self.event = event
        self.depth = depth


def _trace(node: _Node) -> list[dict]:
    """从 BFS 节点还原真实事件轨迹（不含 ε 与初始状态）。"""
    events, cur = [], node
    while cur.parent is not None:
        if cur.event is not None:
            events.append(cur.event)
        cur = cur.parent
    events.reverse()
    return events


def _trace_with_states(wf: W.Workflow, events: list[dict], start_state: dict,
                       prefix_len: int = 0) -> list[dict]:
    """按事件序列逐步重放，产出带快照的步骤列表。"""
    state = start_state
    steps = [{
        "index": 0,
        "event": None,
        "snapshot": snapshot(wf, state),
        "violations": check_invariants(wf, state),
        "shared_prefix": True,
        "alternatives": _alternatives(wf, state),
    }]
    for i, ev in enumerate(events):
        state = _apply_event(wf, state, ev)
        steps.append({
            "index": i + 1,
            "event": _public_event(ev),
            "snapshot": snapshot(wf, state),
            "violations": check_invariants(wf, state),
            "shared_prefix": i < prefix_len,
            "alternatives": _alternatives(wf, state),
        })
    return steps


def _public_event(ev: dict) -> dict:
    # sig 作为前端分叉选择的不透明标识保留
    return dict(ev)


def _visible_successors(wf: W.Workflow, state: dict, depth_left: int = 8) -> list[tuple[dict, dict]]:
    """展开 ε 转移后的真实后继 [(next_state, event)]。

    歧义重调后“重新可调用”是唯一的 ε 后继，需要透明穿过后才能拿到
    success / duplicate / clean 等真实交错事件；RECOVERING 的恢复事件
    本身是显式事件，不会被折叠。
    """
    if state.get("outcome") is not None or depth_left < 0:
        return []
    out: list[tuple[dict, dict]] = []
    for nxt, ev in W.successors(wf, state):
        if ev is None:
            out.extend(_visible_successors(wf, nxt, depth_left - 1))
        else:
            out.append((nxt, ev))
    return out


def _alternatives(wf: W.Workflow, state: dict) -> list[dict]:
    """当前状态可选的下一步事件（供“修改结果后分叉”使用）。

    若状态唯一后继是 ε 转移（歧义重调后重新可调用），透明展开。
    """
    return [_public_event(ev) for _, ev in _visible_successors(wf, state)]


def _apply_event(wf: W.Workflow, state: dict, ev: dict) -> dict:
    """在给定状态上按事件签名挑选后继；透明穿过 ε 转移。"""
    target_sig = tuple(ev["sig"]) if "sig" in ev else None

    def probe(st, depth_left: int):
        for nxt, candidate in W.successors(wf, st):
            if candidate is None and depth_left > 0:
                found = probe(nxt, depth_left - 1)
                if found is not None:
                    return found
            elif candidate is not None and (
                    target_sig is None or tuple(candidate["sig"]) == target_sig):
                return nxt
        return None

    result = probe(state, 8)
    if result is None:
        raise CheckError("重放失败：轨迹事件在当前状态下不可触发（模型可能已修改）")
    return result


def _bfs(wf: W.Workflow, root: _Node, bound: int, depth_offset: int = 0):
    """从 root 做有界 BFS。

    root.depth 是相对根的边数；bound 用同一相对尺度（节点深度 >= bound 不再展开）。
    depth_offset 仅用于把统计中的 max_depth_reached 换算为全局轨迹长度。
    返回 (违反节点|None, 统计dict, 是否截断)。
    """
    seen = {root.fp}
    queue = deque([root])
    max_rel_depth = root.depth
    terminal = 0
    expanded = 0
    truncated = False

    while queue:
        node = queue.popleft()
        succ = W.successors(wf, node.state)
        if not succ:
            terminal += 1
            continue
        if node.depth >= bound:
            truncated = True
            continue
        expanded += 1
        for nxt_state, ev in succ:
            fp = W.fingerprint(nxt_state)
            if ev is None:
                # ε 转移（歧义重调后重新可调用）：不增加轨迹长度
                if fp not in seen:
                    seen.add(fp)
                    queue.append(_Node(nxt_state, fp, node, None, node.depth))
                continue
            violations = check_invariants(wf, nxt_state)
            if violations:
                child = _Node(nxt_state, fp, node, ev, node.depth + 1)
                stats = {
                    "states_visited": len(seen) + 1,
                    "transitions_expanded": expanded,
                    "terminal_states": terminal,
                    "max_depth_reached": depth_offset + max(max_rel_depth, node.depth + 1),
                    "truncated": truncated,
                }
                return child, stats, True
            if fp in seen:
                continue
            seen.add(fp)
            queue.append(_Node(nxt_state, fp, node, ev, node.depth + 1))
            max_rel_depth = max(max_rel_depth, node.depth + 1)

    stats = {
        "states_visited": len(seen),
        "transitions_expanded": expanded,
        "terminal_states": terminal,
        "max_depth_reached": depth_offset + max_rel_depth,
        "truncated": truncated,
    }
    return None, stats, False


def run_check(wf: W.Workflow, bound: int = DEFAULT_BOUND,
              init_vars: dict[str, Any] | None = None) -> dict:
    bound = min(max(int(bound), 1), MAX_BOUND)
    state0 = W.initial_state(wf, init_vars)
    root = _Node(state0, W.fingerprint(state0), None, None, 0)
    if check_invariants(wf, state0):
        return _violation_result(wf, root, bound, {
            "states_visited": 1, "transitions_expanded": 0, "terminal_states": 0,
            "max_depth_reached": 0, "truncated": False,
        })
    bad, stats, _ = _bfs(wf, root, bound)
    if bad is not None:
        return _violation_result(wf, bad, bound, stats)
    return _clean_result(wf, state0, bound, stats)


def _violation_result(wf: W.Workflow, node: _Node, bound: int, stats: dict) -> dict:
    events = _trace(node)
    state0 = _root_state_from_node(node)
    steps = _trace_with_states(wf, events, state0)
    last_violations = steps[-1]["violations"]
    return {
        "verdict": "violated",
        "verdict_text": f"发现反例：不变量「{last_violations[0]['name']}」被破坏",
        "bound": bound,
        "stats": stats,
        "counterexample": {
            "length": len(events),
            "violations": last_violations,
            "steps": steps,
        },
    }


def _root_state_from_node(node: _Node) -> dict:
    cur = node
    while cur.parent is not None:
        cur = cur.parent
    return cur.state


def _clean_result(wf: W.Workflow, state0: dict, bound: int, stats: dict) -> dict:
    # 一条按签名最小的代表轨迹（干净结论下同样支持单步回放与分叉）
    trace_events = _sample_path(wf, state0, bound)
    steps = _trace_with_states(wf, trace_events, state0)
    if stats["truncated"]:
        verdict = "bound_reached"
        text = (f"在 {bound} 步边界内未发现不变量被破坏，但搜索在边界处被截断"
                f"（共访问 {stats['states_visited']} 个不同状态，按指纹合并等价分支）。"
                f"仅对前 {bound} 步负责，不代表全局正确；可增大步数上限继续检验。")
    else:
        verdict = "exhausted"
        text = (f"已穷尽 {bound} 步边界内的全部 {stats['states_visited']} 个可达状态"
                f"（含 {stats['terminal_states']} 个终态），未发现不变量被破坏。"
                f"在该步数边界与给定交错模型下结论成立；不声称超出边界的全局正确性。")
    return {
        "verdict": verdict,
        "verdict_text": text,
        "bound": bound,
        "stats": stats,
        "initial_snapshot": snapshot(wf, state0),
        "initial_alternatives": _alternatives(wf, state0),
        "counterexample": {"length": len(trace_events), "violations": [], "steps": steps},
    }


# ---------------------------------------------------------------------------
# 分叉：沿前缀轨迹走到某一步，改选另一个结果，从该点续跑 BFS
# ---------------------------------------------------------------------------

def run_fork(wf: W.Workflow, prefix: list[dict], chosen_sig: list,
             bound: int = DEFAULT_BOUND,
             init_vars: dict[str, Any] | None = None) -> dict:
    """沿前缀轨迹走到分叉点，改选另一个事件，从该点继续有界 BFS。

    prefix: 共享前缀事件（含 sig）；chosen_sig: 分叉点要改选的事件签名。
    """
    bound = min(max(int(bound), 1), MAX_BOUND)
    state = W.initial_state(wf, init_vars)
    walked: list[dict] = []
    for ev in prefix:
        state = _apply_event(wf, state, ev)
        walked.append(ev)

    chosen_event = chosen_state = None
    sig = tuple(chosen_sig)
    for nxt, ev in W.successors(wf, state):
        if ev is None:
            # ε 展开：分叉点处于“歧义重调中”时，备选事件在 ε 之后
            for n2, e2 in W.successors(wf, nxt):
                if e2 is not None and tuple(e2["sig"]) == sig:
                    chosen_event, chosen_state = e2, n2
                    break
        elif tuple(ev["sig"]) == sig:
            chosen_event, chosen_state = ev, nxt
        if chosen_event is not None:
            break
    if chosen_event is None:
        raise CheckError("所选的分叉事件在当前状态下不可触发")

    # 分叉后的根：深度相对该根计为 1（根状态是“改选事件之后”），
    # 父节点用于轨迹还原。depth_offset 仅用于统计展示全局深度。
    fork_parent = _Node(state, W.fingerprint(state), None, None, 0)
    fork_node = _Node(chosen_state, W.fingerprint(chosen_state),
                      fork_parent, chosen_event, 1)

    # BFS 根深度为 1、边界 = 全局 bound - 前缀长度（根已用掉 1 条边，
    # 故其还可展开 (bound - prefix_len - 1) 条边；depth_offset 使相对深度对齐）。
    bfs_bound = max(bound - len(prefix), 1)
    if check_invariants(wf, chosen_state):
        bad, stats = fork_node, {
            "states_visited": 1, "transitions_expanded": 0, "terminal_states": 0,
            "max_depth_reached": len(prefix) + 1, "truncated": False,
        }
    else:
        bad, stats, _ = _bfs(wf, fork_node, bfs_bound, depth_offset=len(prefix))

    if bad is not None:
        all_events = walked + _trace(bad)
        steps = _trace_with_states(wf, all_events, W.initial_state(wf, init_vars),
                                   prefix_len=len(walked))
        return {
            "verdict": "violated",
            "verdict_text": f"分叉后发现反例：不变量「{steps[-1]['violations'][0]['name']}」被破坏",
            "bound": bound,
            "fork_point": len(walked),
            "chosen_event": _public_event(chosen_event),
            "stats": stats,
            "counterexample": {
                "length": len(all_events),
                "violations": steps[-1]["violations"],
                "steps": steps,
            },
        }

    # 未违反：改选事件 + 一条按签名最小的代表路径，供分叉对比。
    # 改选事件占 1 步，之后还可走 (bfs_bound - 1) 条边。
    remaining = max(bfs_bound - 1, 0)
    suffix = _sample_path(wf, chosen_state, remaining)
    all_events = walked + [chosen_event] + suffix
    steps = _trace_with_states(wf, all_events, W.initial_state(wf, init_vars),
                               prefix_len=len(walked))
    if stats["truncated"]:
        verdict, text = "bound_reached", (
            f"分叉后在全局 {bound} 步边界内（改选后又探索 {remaining} 步）"
            "未发现不变量被破坏，搜索在边界处被截断。仅对该边界负责，不代表全局正确。")
    else:
        verdict, text = "exhausted", (
            f"分叉后已穷尽改选点后 {remaining} 步内全部 {stats['states_visited']} 个可达状态，"
            "未发现不变量被破坏（在该步数边界内）。")
    return {
        "verdict": verdict,
        "verdict_text": text,
        "bound": bound,
        "fork_point": len(walked),
        "chosen_event": _public_event(chosen_event),
        "stats": stats,
        "counterexample": {"length": len(all_events), "violations": [], "steps": steps},
    }


def _sample_path(wf: W.Workflow, state: dict, limit: int) -> list[dict]:
    """无违反时取一条按签名最小的代表路径用于对比展示。

    已出现过的状态指纹（含起点）不再重入；但允许一次「确认前崩溃→恢复」
    回到原 ACTIVE 状态的转移（EV_RECOVER），以便代表轨迹展示恢复语义，
    恢复后崩溃自环会因指纹已见而被跳过，从而走向成功/失败等真正的后继。
    """
    out: list[dict] = []
    seen = {W.fingerprint(state)}
    for _ in range(limit):
        nxt = ev = None
        allowed_recover = False
        for candidate_state, candidate_ev in _visible_successors(wf, state):
            fp = W.fingerprint(candidate_state)
            if fp not in seen:
                nxt, ev = candidate_state, candidate_ev
                break
            # 唯一的例外：确定性恢复事件允许回到已见状态（恰好一次）
            if not allowed_recover and candidate_ev["type"] == W.EV_RECOVER:
                nxt, ev, allowed_recover = candidate_state, candidate_ev, True
        if ev is None:
            break
        if ev["type"] != W.EV_RECOVER:
            seen.add(W.fingerprint(nxt))
        out.append(ev)
        state = nxt
    return out
