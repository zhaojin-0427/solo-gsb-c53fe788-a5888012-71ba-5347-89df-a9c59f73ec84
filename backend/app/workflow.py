"""补偿工作流的语义模型与转移系统。

一个工作流是活动（activity）的有向图。每个活动声明：
  * pre       前置条件（Z3 布尔表达式）
  * assign    成功后的变量赋值（可选）
  * effect    外部效果键（可选；该效果由补偿动作撤销）
  * idempotent 幂等标志：崩溃恢复后重复确认是否安全
  * retryable 活动是否可能报可重试错误
  * max_attempts 最大尝试次数（1 = 不重试）
  * pre_crash / post_crash 是否建模「确认前崩溃」「确认后/应答丢失」
  * comp_* 补偿动作的可重试性与最大尝试次数

运行阶段 (phase)：
  run           正常执行
  compensating  正在按 LIFO 顺序撤销已生效的外部效果
终态：succeeded（全部成功）/ compensated（失败后全部撤销）/ failed（补偿也失败）。

每个状态给出全部后继（一次交错事件），由 checker.py 做有界 BFS。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import expressions as E

# 活动阶段状态
PENDING = "pending"          # 尚未执行
ACTIVE = "active"            # 当前正在执行，交错从这里分叉
RETRYING = "retrying"        # 刚经历可重试错误（或丢失后恢复），下一步确定性地重试
RECOVERING = "recovering"    # 写检查点前崩溃，正在恢复：重新调用同一活动（无持久痕迹）
DONE = "done"                # 成功（若有效果，效果处于“已生效”状态）
FAILED = "failed"            # 活动终态失败（正向流程结束）

PHASE_RUN = "run"
PHASE_COMP = "compensating"
TERMINAL = {"succeeded", "compensated", "failed"}

# 事件类型（中文名供 UI 展示；sig 用于稳定排序与重放匹配）
EV_INVOKE = "invoke"
EV_SUCCESS = "success"                     # 一次调用即成功
EV_RETRYABLE = "retryable_error"           # 可重试错误
EV_RETRY = "retry"                         # 确定性：重新调度调用
EV_FATAL = "fatal_error"                   # 不可重试错误
EV_EXHAUSTED = "attempts_exhausted"        # 重试次数耗尽
EV_PRE_CRASH = "crash_before_ack"          # 确认前崩溃（进入可区分的“恢复中”状态）
EV_RECOVER = "recover_pre_ack"             # 恢复完成：重新调用同一活动，尝试次数不变
EV_ACK = "recover_acknowledged"            # 恢复时确认：调用其实已生效，无需重试
EV_RETRY_DUP = "retry_timeout_duplicate"   # 超时重试成功，但首次调用其实也生效了（重复）
EV_RETRY_CLEAN = "retry_timeout_clean"     # 超时重试成功，首次调用确实未生效
EV_COMPENSATE = "compensate"
EV_COMP_SUCCESS = "compensated"            # 单个效果撤销成功
EV_COMP_ERROR = "comp_retryable_error"
EV_COMP_RETRY = "comp_retry"
EV_COMP_FATAL = "comp_fatal_error"
EV_COMP_EXHAUSTED = "comp_attempts_exhausted"

EVENT_LABELS = {
    EV_INVOKE: "调用活动",
    EV_SUCCESS: "调用成功，效果生效",
    EV_RETRYABLE: "可重试错误",
    EV_RETRY: "重新调度调用",
    EV_FATAL: "不可重试错误",
    EV_EXHAUSTED: "尝试次数耗尽",
    EV_PRE_CRASH: "确认前崩溃（无持久痕迹，进入恢复）",
    EV_RECOVER: "崩溃恢复：重新调用同一活动（尝试次数不增加、无外部效果）",
    EV_ACK: "恢复确认：调用已生效，无需重试",
    EV_RETRY_DUP: "超时重试成功，但首次调用其实也生效（重复）",
    EV_RETRY_CLEAN: "超时重试成功，首次调用未生效",
    EV_COMPENSATE: "调用补偿动作",
    EV_COMP_SUCCESS: "补偿成功，效果已撤销",
    EV_COMP_ERROR: "补偿可重试错误",
    EV_COMP_RETRY: "重新调度补偿",
    EV_COMP_FATAL: "补偿不可重试失败",
    EV_COMP_EXHAUSTED: "补偿重试耗尽",
}


class WorkflowError(ValueError):
    pass


@dataclass
class Activity:
    id: str
    name: str
    pre: E.Node | None = None
    assign: dict[str, E.Node] = field(default_factory=dict)
    effect: str | None = None
    idempotent: bool = False
    can_fail: bool = True
    retryable: bool = True
    max_attempts: int = 3
    pre_crash: bool = False
    post_crash: bool = False
    comp_can_fail: bool = True
    comp_retryable: bool = True
    comp_max_attempts: int = 3
    # 并发调度安全分析（见 concurrency.py）：缺省即纯串行模型，语义零变化
    fork_group: str | None = None     # 该活动是某 fork/join 组的分叉点
    join_group: str | None = None     # 该活动是某 fork/join 组的汇合点
    locks: tuple[str, ...] = ()       # 进入活动时按序获取的互斥锁
    reads: tuple[str, ...] = ()       # 成功时对共享资源的读访问
    writes: tuple[str, ...] = ()      # 成功时对共享资源的写访问


@dataclass
class Edge:
    src: str
    dst: str
    guard: E.Node | None = None
    guard_src: str = ""


@dataclass
class ConcGroup:
    """fork/join 汇合组：fork 活动的每条生效出边派生一个分支线程，
    全部分支到达 join 活动后汇合，join 活动本身再由续跑线程执行一次。"""
    id: str
    fork: str
    join: str


@dataclass
class Workflow:
    activities: dict[str, Activity]
    edges: list[Edge]
    start: str
    variables: dict[str, str]
    invariants: dict[str, E.Node]
    invariant_at_most_once: bool = True
    invariant_undo_on_failure: bool = True
    out_edges: dict[str, list[Edge]] = field(default_factory=dict)
    # 并发声明（无并发配置时为空容器，既有串行语义完全不变）
    resources: tuple[str, ...] = ()
    locks: tuple[str, ...] = ()
    conc_groups: dict[str, ConcGroup] = field(default_factory=dict)

    def act(self, aid: str) -> Activity:
        return self.activities[aid]

    @property
    def concurrent(self) -> bool:
        return bool(self.conc_groups)


# ---------------------------------------------------------------------------
# 编译：原始 dict（来自 API）-> Workflow，含完整静态校验（Z3 可行性）
# ---------------------------------------------------------------------------

def compile_workflow(spec: dict[str, Any]) -> Workflow:
    errors: list[str] = []
    activities: dict[str, Activity] = {}
    var_types: dict[str, str] = {}
    for v in spec.get("variables", []):
        name = str(v.get("name", "")).strip()
        kind = v.get("type", "int")
        if not name:
            errors.append("存在未命名的变量")
            continue
        if name in var_types:
            errors.append(f"变量 {name} 重复声明")
        if kind not in ("int", "bool"):
            errors.append(f"变量 {name} 的类型必须是 int 或 bool")
        else:
            var_types[name] = kind

    for a in spec.get("activities", []):
        aid = str(a.get("id", "")).strip()
        if not aid:
            errors.append("存在没有 id 的活动")
            continue
        if aid in activities:
            errors.append(f"活动 id 重复：{aid}")
            continue
        try:
            max_attempts = int(a.get("max_attempts", 3))
            comp_max = int(a.get("comp_max_attempts", 3))
            if max_attempts < 1 or comp_max < 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"活动 {aid} 的最大尝试次数必须是 >= 1 的整数")
            max_attempts, comp_max = 3, 3
        pre = _compile_opt(a.get("pre"), var_types, "bool", errors, f"活动 {aid} 的前置条件")
        assign: dict[str, E.Node] = {}
        for asn in a.get("assign", []):
            vn = str(asn.get("var", "")).strip()
            if vn not in var_types:
                errors.append(f"活动 {aid} 给未声明的变量 {vn} 赋值")
                continue
            node = _compile_opt(asn.get("expr"), var_types, var_types[vn], errors,
                                f"活动 {aid} 对 {vn} 的赋值")
            if node is not None:
                assign[vn] = node
        def _name_tuple(raw: Any, where: str) -> tuple[str, ...]:
            if raw is None:
                return ()
            if isinstance(raw, str):
                raw = [x.strip() for x in raw.split(",") if x.strip()]
            if not isinstance(raw, (list, tuple)):
                errors.append(f"{where}必须是名称列表")
                return ()
            out: list[str] = []
            for x in raw:
                t = str(x).strip()
                if t:
                    out.append(t)
            return tuple(out)

        activities[aid] = Activity(
            id=aid,
            name=str(a.get("name", aid)) or aid,
            pre=pre,
            assign=assign,
            effect=(str(a["effect"]).strip() if a.get("effect") else None),
            idempotent=bool(a.get("idempotent", False)),
            can_fail=bool(a.get("can_fail", True)),
            retryable=bool(a.get("retryable", True)),
            max_attempts=max_attempts,
            pre_crash=bool(a.get("pre_crash", False)),
            post_crash=bool(a.get("post_crash", False)),
            comp_can_fail=bool(a.get("comp_can_fail", True)),
            comp_retryable=bool(a.get("comp_retryable", True)),
            comp_max_attempts=comp_max,
            fork_group=(str(a["fork_group"]).strip() if a.get("fork_group") else None),
            join_group=(str(a["join_group"]).strip() if a.get("join_group") else None),
            locks=_name_tuple(a.get("locks"), f"活动 {aid} 的锁列表"),
            reads=_name_tuple(a.get("reads"), f"活动 {aid} 的读资源列表"),
            writes=_name_tuple(a.get("writes"), f"活动 {aid} 的写资源列表"),
        )

    edges: list[Edge] = []
    out_edges: dict[str, list[Edge]] = {}
    for e in spec.get("edges", []):
        s, d = str(e.get("src", "")).strip(), str(e.get("dst", "")).strip()
        if s not in activities:
            errors.append(f"边的起点不存在：{s} -> {d}")
        if d not in activities:
            errors.append(f"边的终点不存在：{s} -> {d}")
        if s in activities and d in activities:
            g = _compile_opt(e.get("guard"), var_types, "bool", errors,
                             f"边 {s} -> {d} 的守卫")
            edge = Edge(s, d, g, str(e.get("guard", "") or ""))
            edges.append(edge)
            out_edges.setdefault(s, []).append(edge)

    start = str(spec.get("start", "")).strip()
    if not start:
        errors.append("未声明起始活动")
    elif start not in activities:
        errors.append(f"起始活动不存在：{start}")

    inv_cfg = spec.get("invariants", {}) or {}
    invariants: dict[str, E.Node] = {}
    for inv in inv_cfg.get("custom", []):
        label = str(inv.get("name", "")).strip() or "自定义不变量"
        node = _compile_opt(inv.get("expr"), var_types, "bool", errors, f"不变量「{label}」")
        if node is not None:
            invariants[label] = node

    # 并发声明：资源 / 锁 / fork-join 汇合组（全部可选；不声明即纯串行）
    conc_cfg = spec.get("concurrency", {}) or {}
    resources: list[str] = []
    for r in conc_cfg.get("resources", []):
        name = str(r.get("name", "") if isinstance(r, dict) else r).strip()
        if not name:
            errors.append("存在未命名的共享资源")
            continue
        if name in resources:
            errors.append(f"共享资源 {name} 重复声明")
            continue
        resources.append(name)
    locks: list[str] = []
    for lk in conc_cfg.get("locks", []):
        name = str(lk.get("name", "") if isinstance(lk, dict) else lk).strip()
        if not name:
            errors.append("存在未命名的锁")
            continue
        if name in locks:
            errors.append(f"锁 {name} 重复声明")
            continue
        locks.append(name)
    resource_set, lock_set = set(resources), set(locks)
    conc_groups: dict[str, ConcGroup] = {}
    for g in conc_cfg.get("groups", []):
        gid = str(g.get("id", "")).strip()
        fk, jn = str(g.get("fork", "")).strip(), str(g.get("join", "")).strip()
        if not gid:
            errors.append("存在没有 id 的 fork/join 汇合组")
            continue
        if gid in conc_groups:
            errors.append(f"汇合组 id 重复：{gid}")
            continue
        if not fk or fk not in activities:
            errors.append(f"汇合组 {gid} 的 fork 活动不存在：{fk or '（未声明）'}")
        if not jn or jn not in activities:
            errors.append(f"汇合组 {gid} 的 join 活动不存在：{jn or '（未声明）'}")
        if fk and jn and fk == jn:
            errors.append(f"汇合组 {gid} 的 fork 与 join 不能是同一活动")
        if gid in resource_set or gid in lock_set:
            errors.append(f"汇合组 id {gid} 与资源/锁重名")
        conc_groups[gid] = ConcGroup(gid, fk, jn)

    wf = Workflow(
        activities=activities,
        edges=edges,
        start=start,
        variables=var_types,
        invariants=invariants,
        invariant_at_most_once=bool(inv_cfg.get("at_most_once", True)),
        invariant_undo_on_failure=bool(inv_cfg.get("undo_on_failure", True)),
        out_edges=out_edges,
        resources=tuple(resources),
        locks=tuple(locks),
        conc_groups=conc_groups,
    )
    _check_reachability(wf, errors)
    _check_concurrency(wf, errors)
    if errors:
        raise WorkflowError("；".join(errors))
    return wf


def _compile_opt(src: Any, var_types, want: str, errors: list[str], where: str):
    if src is None or not str(src).strip():
        return None
    try:
        node = E.parse(str(src))
        got = E.infer_type(node, var_types)
        if got != want:
            kind_got = "布尔" if got == "bool" else "整数"
            kind_want = "布尔" if want == "bool" else "整数"
            errors.append(f"{where}必须是{kind_want}表达式（当前推断为{kind_got}）")
            return None
        return node
    except E.ExpressionError as ex:
        errors.append(f"{where}有误：{ex}")
        return None


def _check_reachability(wf: Workflow, errors: list[str]) -> None:
    """结构可达性 + 前置条件 Z3 可行性检查。"""
    if not wf.activities or wf.start not in wf.activities:
        return
    seen, stack = set(), [wf.start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for e in wf.out_edges.get(cur, []):
            stack.append(e.dst)
    unreachable = set(wf.activities) - seen
    if unreachable:
        errors.append("以下活动从起点不可达：" + ", ".join(sorted(unreachable)))
    # 前置条件 Z3 可行性：恒假说明该分支永远不可启用
    for aid in sorted(seen):
        a = wf.activities[aid]
        if a.pre is not None:
            try:
                res = E.satisfiable(a.pre, wf.variables)
                if res == "unsat":
                    errors.append(f"活动 {aid} 的前置条件恒为假（Z3 判定不可满足），它永远不会被启用")
            except Exception as ex:
                errors.append(f"活动 {aid} 的前置条件可行性检查失败：{ex}")


def _check_concurrency(wf: Workflow, errors: list[str]) -> None:
    """fork/join 汇合组与共享资源/锁的静态结构校验。"""
    if not wf.activities:
        return
    local_errors_before = len(errors)
    # 1) 活动上的 fork/join/锁/资源引用合法性
    fork_by: dict[str, str] = {}
    join_by: dict[str, str] = {}
    for gid, g in wf.conc_groups.items():
        if g.fork in wf.activities:
            a = wf.act(g.fork)
            if a.fork_group and a.fork_group != gid:
                errors.append(f"活动 {g.fork} 被声明为多个汇合组的 fork（{a.fork_group} 与 {gid}）")
            a.fork_group = gid
            fork_by[g.fork] = gid
        if g.join in wf.activities:
            a = wf.act(g.join)
            if a.join_group and a.join_group != gid:
                errors.append(f"活动 {g.join} 被声明为多个汇合组的 join（{a.join_group} 与 {gid}）")
            a.join_group = gid
            join_by[g.join] = gid
    for aid in sorted(wf.activities):
        a = wf.activities[aid]
        if a.fork_group and a.fork_group not in wf.conc_groups:
            errors.append(f"活动 {aid} 声明的 fork 汇合组不存在：{a.fork_group}")
        if a.join_group and a.join_group not in wf.conc_groups:
            errors.append(f"活动 {aid} 声明的 join 汇合组不存在：{a.join_group}")
        if a.fork_group and a.join_group:
            errors.append(f"活动 {aid} 不能同时作为 fork 与 join")
        for lk in a.locks:
            if lk not in wf.locks:
                errors.append(f"活动 {aid} 获取的锁未声明：{lk}")
        if len(set(a.locks)) != len(a.locks):
            errors.append(f"活动 {aid} 的锁列表存在重复（同一活动不得重复加锁）")
        for key in (*a.reads, *a.writes):
            if key not in wf.resources:
                errors.append(f"活动 {aid} 访问的共享资源未声明：{key}")
        if set(a.reads) & set(a.writes):
            errors.append(f"活动 {aid} 对同一资源不应同时声明读和写（写已包含读语义）："
                          + ", ".join(sorted(set(a.reads) & set(a.writes))))
    if len(errors) > local_errors_before:
        return  # 引用类错误已使结构不一致，后续区域检查无意义

    # 2) 每个汇合组的区域：fork 出边派生的、不经过 join 可达的全部活动
    for gid, g in sorted(wf.conc_groups.items()):
        out = [e for e in wf.out_edges.get(g.fork, [])]
        if len(out) < 2:
            errors.append(f"汇合组 {gid} 的 fork 活动 {g.fork} 至少需要 2 条出边（实际 {len(out)} 条）")
        region: set[str] = set()
        stack: list[str] = []
        for e in out:
            if e.dst != g.join:
                stack.append(e.dst)
        while stack:
            cur = stack.pop()
            if cur in region or cur == g.join:
                continue
            region.add(cur)
            for e in wf.out_edges.get(cur, []):
                if e.dst != g.join and e.dst not in region:
                    stack.append(e.dst)
        if not region:
            errors.append(f"汇合组 {gid} 的所有分支都直接连到 join {g.join}，没有可并发活动")
        # 区域内活动不得再充当其他组的 fork/join（仅支持非嵌套、不重叠的汇合组）
        for aid in sorted(region):
            a = wf.act(aid)
            if a.fork_group or a.join_group:
                errors.append(f"汇合组 {gid} 区域内的活动 {aid} 不能再声明 fork/join"
                              "（暂不支持嵌套或重叠的汇合组）")
        # join 必须从区域（或 fork 直连）可达
        join_reachable = any(e.dst == g.join for e in out) or any(
            e.dst == g.join for aid in region for e in wf.out_edges.get(aid, []))
        if not join_reachable:
            errors.append(f"汇合组 {gid} 的 join 活动 {g.join} 从任何分支都不可达"
                          "（存在无法汇合的分支）")
        # 区域内不得有环：否则分支可能永远无法到达 join
        color: dict[str, int] = {}

        def dfs_cycle(x: str) -> bool:
            color[x] = 1
            for e in wf.out_edges.get(x, []):
                if e.dst == g.join or e.dst not in region:
                    continue
                if color.get(e.dst) == 1 or (color.get(e.dst) == 0 and dfs_cycle(e.dst)):
                    return True
            color[x] = 2
            return False

        for aid in sorted(region):
            if color.get(aid, 0) == 0 and dfs_cycle(aid):
                errors.append(f"汇合组 {gid} 的分支区域内存在环（分支可能永远无法到达 join {g.join}）")
                break


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def initial_state(wf: Workflow, init_vars: dict[str, Any] | None = None) -> dict:
    variables = {name: (False if t == "bool" else 0) for name, t in wf.variables.items()}
    if init_vars:
        for k, v in init_vars.items():
            if k in variables:
                variables[k] = bool(v) if wf.variables[k] == "bool" else int(v)
    status = {aid: PENDING for aid in wf.activities}
    status[wf.start] = ACTIVE
    return {
        "phase": PHASE_RUN,
        "status": status,
        "current": wf.start,
        "tries": {aid: 0 for aid in wf.activities},
        "variables": variables,
        "effects": [],          # [{key, activity, undone}]
        "comp_stack": [],       # effects 列表中的索引（LIFO）
        "comp_tries": {},
        "comp_retrying": None,
        "ambiguous": None,      # 存疑调用：{"activity":.., "attempt":k}（超时/应答丢失后重试中）
        "outcome": None,
    }


def fingerprint(state: dict) -> str:
    import json
    effects = [(e["key"], e["activity"], 1 if e["undone"] else 0) for e in state["effects"]]
    status = tuple(sorted((a, s) for a, s in state["status"].items() if s != PENDING))
    tries = tuple(sorted(state["tries"].items()))
    variables = tuple(sorted(state["variables"].items()))
    comp_stack = tuple(state.get("comp_stack", []))
    comp_tries = tuple(sorted(state.get("comp_tries", {}).items()))
    amb = (None if not state.get("ambiguous")
           else (state["ambiguous"]["activity"], state["ambiguous"]["attempt"]))
    return json.dumps(
        [state["phase"], status, tries, variables, effects, comp_stack, comp_tries,
         amb, state["outcome"]],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _ctx(state: dict) -> dict:
    succ: dict[str, int] = {a: 0 for a in state["status"]}
    effects: dict[str, int] = {}
    undone: dict[str, int] = {}
    for a, s in state["status"].items():
        if s == DONE:
            succ[a] += 1
    for e in state["effects"]:
        if e["undone"]:
            undone[e["key"]] = undone.get(e["key"], 0) + 1
        else:
            effects[e["key"]] = effects.get(e["key"], 0) + 1
    # 当前仍处于“重复生效”状态的键（未撤销）
    duplicates = [e["key"] for e in state["effects"]
                  if not e["undone"] and e.get("duplicate")]
    return {"succ": succ, "effects": effects, "undone": undone,
            "duplicates": duplicates}


def _z3_functions(state: dict):
    c = _ctx(state)
    return {
        "succ": lambda key: c["succ"].get(key, 0),
        "effects": lambda key: c["effects"].get(key, 0),
        "undone": lambda key: c["undone"].get(key, 0),
    }


def _eval_bool(node, state: dict) -> bool:
    return bool(E.eval_node(node, state["variables"], _z3_functions(state)))


# ---------------------------------------------------------------------------
# 后继枚举
# ---------------------------------------------------------------------------

def successors(wf: Workflow, state: dict) -> list[tuple[dict, dict]]:
    """返回 [(next_state, event)]，按事件签名稳定排序。"""
    if state["outcome"] is not None:
        return []
    cur_status = state["status"].get(state.get("current"))
    if cur_status == RETRYING:
        if state.get("ambiguous"):
            # 超时/应答丢失后的重调：重入队已由“超时重调”事件表示，此处直接可调用
            s = _clone(state)
            s["status"][s["current"]] = ACTIVE
            return [(s, None)]
        return [_deterministic_retry(state)]
    if cur_status == RECOVERING:
        return [_deterministic_recover(state)]
    if state.get("comp_retrying"):
        return [_deterministic_comp_retry(state)]
    if state["phase"] == PHASE_RUN:
        out = _run_successors(wf, state)
    else:
        out = _comp_successors(wf, state)
    out.sort(key=lambda pair: pair[1]["sig"])
    return out


def _deterministic_retry(state: dict) -> tuple[dict, dict]:
    s = _clone(state)
    cur = s["current"]
    s["status"][cur] = ACTIVE
    return s, _event(EV_RETRY, cur, attempt=s["tries"][cur],
                     note="错误已持久化，重新入队调用")


def _deterministic_recover(state: dict) -> tuple[dict, dict]:
    """确认前崩溃后的恢复：重新调用同一活动。

    崩溃发生在写检查点之前，活动没有留下任何持久痕迹：不登记外部效果、
    不增加尝试次数、无歧义；状态回到 ACTIVE，由同一活动的全部交错重新展开。
    """
    s = _clone(state)
    cur = s["current"]
    s["status"][cur] = ACTIVE
    return s, _event(EV_RECOVER, cur, attempt=s["tries"][cur] + 1,
                     note="进程恢复：崩溃发生在写检查点之前，活动无持久痕迹，"
                          "重新调用同一活动（尝试次数不增加，未产生任何外部效果）")


def _deterministic_comp_retry(state: dict) -> tuple[dict, dict]:
    s = _clone_comp(state)
    idx = s["comp_retrying"]
    aid = s["effects"][idx]["activity"]
    s["comp_retrying"] = None
    return s, _event(EV_COMP_RETRY, aid, attempt=s["comp_tries"].get(aid, 0),
                     note="补偿错误已记录，重新调度补偿动作")


def _clone(state: dict) -> dict:
    return {
        "phase": state["phase"],
        "status": dict(state["status"]),
        "current": state["current"],
        "tries": dict(state["tries"]),
        "variables": dict(state["variables"]),
        "effects": [dict(e) for e in state["effects"]],
        "comp_stack": list(state.get("comp_stack", [])),
        "comp_tries": dict(state.get("comp_tries", {})),
        "comp_retrying": state.get("comp_retrying"),
        "ambiguous": (dict(state["ambiguous"]) if state.get("ambiguous") else None),
        "outcome": state["outcome"],
    }


def _clone_comp(state: dict) -> dict:
    return _clone(state)


def _event(kind: str, activity: str, attempt: int | None = None, **extra) -> dict:
    sig: tuple = (kind,) if activity is None else (kind, activity)
    if attempt is not None:
        sig = (*sig, attempt)
    ev = {"type": kind, "activity": activity or None, "label": EVENT_LABELS.get(kind, kind),
          "attempt": attempt, "sig": list(sig)}
    ev.update(extra)
    return ev


def _record_effect(state: dict, activity: Activity, duplicate: bool = False) -> bool:
    """登记外部效果。duplicate 由调用方（歧义重试场景）给出；返回最终的重复标志。"""
    if not duplicate and not activity.idempotent:
        for e in state["effects"]:
            if e["key"] == activity.effect and not e["undone"]:
                duplicate = True
                break
    state["effects"].append({"key": activity.effect, "activity": activity.id,
                             "undone": False, "duplicate": bool(duplicate)})
    return bool(duplicate)


def _run_successors(wf: Workflow, state: dict) -> list[tuple[dict, dict]]:
    cur_id = state["current"]
    a = wf.act(cur_id)
    attempt = state["tries"][cur_id]            # 已完成的尝试次数
    amb = state.get("ambiguous")                # 本次调用是否是“存疑调用后的重试”
    results: list[tuple[dict, dict]] = []

    def finish_success(dup: bool | None) -> tuple[dict, dict]:
        """成功收尾。dup=None 普通成功；dup=True/False 为歧义重试的两种真相。"""
        s = _clone(state)
        s["tries"][cur_id] = attempt + 1
        for var, expr in a.assign.items():
            s["variables"][var] = E.eval_node(expr, s["variables"], _z3_functions(s))
        if a.effect:
            if dup is True and not a.idempotent:
                # 非幂等：首次其实已生效 + 重试再次生效 => 两条记录，后者标记重复
                _record_effect(s, a, duplicate=False)
                _record_effect(s, a, duplicate=True)
            else:
                # 普通成功；或“首次已生效但活动幂等”——重试与首次合并为同一逻辑效果
                _record_effect(s, a, duplicate=False)
        # 检查当前活动是否留下了未撤销的重复生效（仅用于事件标记）
        duplicate = any(e.get("duplicate") and not e["undone"] and e["activity"] == cur_id
                        for e in s["effects"])
        s["status"][cur_id] = DONE
        s["ambiguous"] = None
        if dup is None:
            ev = _event(EV_SUCCESS, cur_id, attempt=attempt + 1,
                        effect=a.effect, duplicate=duplicate)
        else:
            kind = EV_RETRY_DUP if dup else EV_RETRY_CLEAN
            ev = _event(kind, cur_id, attempt=attempt + 1, effect=a.effect,
                        duplicate=duplicate,
                        first_attempt=(amb["attempt"] if amb else None))
        _advance(wf, s, a, ev)
        return s, ev

    # 1) 成功（歧义重试时分为“重复 / 干净”两种交错）
    if amb is not None:
        results.append(finish_success(True))
        results.append(finish_success(False))
    else:
        results.append(finish_success(None))

    # 2) 失败结局（仅当活动可能失败时）
    if a.can_fail:
        if a.retryable and attempt + 1 < a.max_attempts:
            # 可重试错误，且仍有额度 -> 确定性重试
            s = _clone(state)
            s["tries"][cur_id] = attempt + 1
            s["status"][cur_id] = RETRYING
            if amb is not None:
                # 歧义重试又失败：首次调用仍可能已生效，歧义保持
                s["ambiguous"] = {"activity": cur_id, "attempt": amb["attempt"]}
            results.append((s, _event(EV_RETRYABLE, cur_id, attempt=attempt + 1,
                                      note=f"收到可重试错误，已持久化第 {attempt + 1} 次失败")))
        else:
            # 额度用尽：歧义状态下首次调用可能已生效，需登记效果等待补偿
            s = _clone(state)
            s["tries"][cur_id] = attempt + 1
            note = "最大尝试次数已用尽，放弃正向流程"
            if amb is not None and a.effect:
                _record_effect(s, a, duplicate=False)
                note += "；存疑的首次调用按“可能已生效”登记，补偿阶段尝试撤销"
            s["status"][cur_id] = FAILED
            s["current"] = None
            s["ambiguous"] = None
            ev = _event(EV_EXHAUSTED, cur_id, attempt=attempt + 1, note=note)
            _init_compensation(wf, s, ev)
            results.append((s, ev))

        # 3) 不可重试错误（任何一次尝试都可能发生；歧义时同样按可能已生效登记）
        s = _clone(state)
        s["tries"][cur_id] = attempt + 1
        note = "收到不可重试错误，立即放弃正向流程"
        if amb is not None and a.effect:
            _record_effect(s, a, duplicate=False)
            note += "；存疑的首次调用按“可能已生效”登记，补偿阶段尝试撤销"
        s["status"][cur_id] = FAILED
        s["current"] = None
        s["ambiguous"] = None
        ev = _event(EV_FATAL, cur_id, attempt=attempt + 1, note=note)
        _init_compensation(wf, s, ev)
        results.append((s, ev))

    # 4) 确认前崩溃：无持久痕迹，进入可区分的“恢复中”状态；恢复后重新调用
    #    （尝试次数不增加、无外部效果，由 EV_RECOVER 确定性地回到 ACTIVE）。
    #    RECOVERING 进入状态指纹，保证它不会因与崩溃前的 ACTIVE 状态业务字段
    #    相同而被有界 BFS 的去重丢弃；再次崩溃与恢复也因此是可枚举、可回放、
    #    可分叉的显式状态与事件。
    if a.pre_crash:
        s = _clone(state)
        s["status"][cur_id] = RECOVERING
        results.append((s, _event(EV_PRE_CRASH, cur_id, attempt=attempt + 1,
                                  note="进程在写检查点之前崩溃，活动无任何持久痕迹，"
                                       "进入恢复；恢复后重新调用同一活动")))

    # 5) 确认后崩溃 / 应答丢失：恢复时确认真相，或进入“存疑重试”
    if a.post_crash:
        # 5a) 恢复确认：调用其实已生效，无需重试（幂等活动不会产生重复标记）
        s = _clone(state)
        s["tries"][cur_id] = attempt + 1
        duplicate = False
        if a.effect:
            duplicate = _record_effect(s, a)
        s["status"][cur_id] = DONE
        s["ambiguous"] = None
        ev = _event(EV_ACK, cur_id, attempt=attempt + 1, effect=a.effect,
                    duplicate=duplicate,
                    note="调用其实已在外部生效；恢复时通过查询确认，无需重试")
        _advance(wf, s, a, ev)
        results.append((s, ev))

        # 5b) 无法确认真相 -> 重新调用；成功时由 finish_success 的两种交错表示
        s = _clone(state)
        # 不消耗尝试次数：结果未知，本次调用“悬而未决”
        s["status"][cur_id] = RETRYING
        s["ambiguous"] = {"activity": cur_id, "attempt": attempt + 1}
        results.append((s, _event(EV_RETRY, cur_id, attempt=attempt + 1,
                                  ambiguous=True,
                                  note="应答丢失且无法查询真相，按超时重新调用；"
                                       "首次调用是否已生效将在重试结果中分叉")))

    return results


def _advance(wf: Workflow, state: dict, a: Activity, ev: dict) -> None:
    """成功后沿出边选取第一个「守卫为真且前置条件成立」的后继。"""
    chosen: Edge | None = None
    blocked: list[dict] = []
    for e in wf.out_edges.get(a.id, []):
        if e.guard is not None and not _eval_bool(e.guard, state):
            blocked.append({"target": e.dst, "reason": "guard_false",
                            "detail": e.guard_src})
            continue
        dst = wf.act(e.dst)
        if dst.pre is not None and not _eval_bool(dst.pre, state):
            blocked.append({"target": e.dst, "reason": "precondition_false"})
            continue
        chosen = e
        break
    ev["blocked"] = blocked
    if chosen is not None:
        # 回到此前执行过的活动（循环）：重置尝试计数
        state["status"][chosen.dst] = ACTIVE
        state["tries"][chosen.dst] = 0
        state["current"] = chosen.dst
        ev["target"] = chosen.dst
        return
    if not wf.out_edges.get(a.id):
        state["current"] = None
        state["outcome"] = "succeeded"
        ev["note"] = (ev.get("note") or "") + " 无出边，工作流正常完成"
    else:
        state["current"] = None
        ev["note"] = (ev.get("note", "") + " 守卫通过的分支中没有前置条件成立的后继，正向流程失败").strip()
        _init_compensation(wf, state, ev)


def _init_compensation(wf: Workflow, state: dict, ev: dict) -> None:
    """失败后构造 LIFO 补偿栈：仍未撤销的已生效效果索引，后生效先补偿。"""
    stack = [i for i, e in enumerate(state["effects"]) if not e["undone"]]
    state["phase"] = PHASE_COMP
    state["comp_stack"] = list(reversed(stack))
    state["comp_tries"] = {}
    state["comp_retrying"] = None
    if not stack:
        state["outcome"] = "compensated"
        ev["comp_note"] = "没有已生效的外部效果需要撤销"


def _comp_successors(wf: Workflow, state: dict) -> list[tuple[dict, dict]]:
    stack = state["comp_stack"]
    if not stack:
        s = _clone_comp(state)
        s["outcome"] = "compensated"
        return [(s, _event(EV_COMP_SUCCESS, "", note="所有外部效果均已撤销"))]
    idx = stack[0]
    top = state["effects"][idx]
    a = wf.act(top["activity"])
    tries = state["comp_tries"].get(a.id, 0)
    results: list[tuple[dict, dict]] = []

    # 补偿成功：撤销栈顶效果
    s = _clone_comp(state)
    s["comp_tries"][a.id] = tries + 1
    s["effects"][idx]["undone"] = True
    s["comp_stack"] = stack[1:]
    next_target = s["effects"][stack[1]]["activity"] if len(stack) > 1 else None
    ev = _event(EV_COMP_SUCCESS, a.id, attempt=tries + 1, effect=top["key"], target=next_target)
    if not s["comp_stack"]:
        s["outcome"] = "compensated"
    results.append((s, ev))

    if not a.comp_can_fail:
        # 补偿动作被建模为确定性安全（例如可信的反向操作）：仅成功
        return results

    if a.comp_retryable and tries + 1 < a.comp_max_attempts:
        # 可重试错误，仍有额度 -> 确定性重新调度
        s = _clone_comp(state)
        s["comp_tries"][a.id] = tries + 1
        s["comp_retrying"] = idx
        results.append((s, _event(EV_COMP_ERROR, a.id, attempt=tries + 1,
                                  note="补偿动作遇到可重试错误")))
    elif a.comp_retryable:
        # 可重试但额度用尽 -> 永久残留
        s = _clone_comp(state)
        s["comp_tries"][a.id] = tries + 1
        s["outcome"] = "failed"
        results.append((s, _event(EV_COMP_EXHAUSTED, a.id, attempt=tries + 1,
                                  note="补偿动作最大尝试次数用尽，外部效果永久残留")))

    # 补偿动作不可重试地失败（任何一次尝试都可能发生）
    s = _clone_comp(state)
    s["comp_tries"][a.id] = tries + 1
    s["outcome"] = "failed"
    results.append((s, _event(EV_COMP_FATAL, a.id, attempt=tries + 1,
                              note="补偿动作自身不可重试地失败，外部效果永久残留")))
    return results
