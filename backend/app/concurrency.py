"""并发调度安全分析：fork/join 汇合组 + 共享资源读写 + 互斥锁的交错枚举。

在既有工作流语义（workflow.py 的活动交错、崩溃恢复、补偿）之上叠加一层
**多线程部分序交错**：

* fork 活动成功后，其每条「守卫为真且前置成立」的出边派生一个分支线程；
  分支线程各自独立枚举活动的成功/失败/重试/崩溃交错，互不阻塞（除非竞争锁）；
* 分支到达本组 join 活动时交出**汇合令牌**；最后一个分支到达时汇合，新建一个
  续跑线程（时钟为各分支时钟之并）执行 join 活动，令牌弹栈；
* 活动可声明 ``locks``（按序获取的互斥锁）与对共享资源的 ``reads``/``writes``。
  锁获取是显式调度事件（``lock_acquired``），获取不到则 ``lock_wait`` 阻塞，
  其他可运行线程继续被枚举；阻塞边形成等待环即**锁顺序死锁**；
* 共享资源竞态用**向量时钟**判定：两个不同线程对同一资源的访问不存在 happens-before
  关系、且不持有公共锁时，写-写 / 读-写即为**数据竞态**；
* 步数边界内仍有汇合组未闭合（分支仍在运行、未全部到达 join）的截断状态只标记为
  **边界截断**（bound_reached），不是问题，增大步数上限即可继续分析；
* 分支线程在到达本组 join 前**正常结束**（最后一条边通向区域内无出边的活动）时，
  汇合令牌在结构上永远无法交齐：该转移立即生成「无法汇合」事件并封存终态；
  若最后一个可运行线程到达 join 时仍欠令牌（其他分支均已终止），到达转移同样
  封存。终态收集据此把「所有线程均不可继续且仍欠汇合令牌」的状态判定为问题而非
  干净终态——与步数边界无关，足够大的步数边界下同样最短可达、稳定可复现。

状态指纹包含**线程状态、各线程向量时钟、锁持有/等待、汇合令牌、资源访问史与
已发现问题集合**，按指纹合并等价交错；BFS 保证每类问题的见证轨迹最短、
同深度按事件签名稳定可复现。补偿、崩溃恢复与既有不变量在并发语义下复用同一套
规则（一个分支失败即释放全部分支持有的锁并进入全局 LIFO 补偿）。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from . import expressions as E
from . import workflow as W

MAX_CONC_BOUND = 80
DEFAULT_CONC_BOUND = 24
STATE_CAP = 200_000

# 并发调度事件（活动自身的成功/失败/崩溃事件沿用 workflow.EV_*）
EV_SCHEDULE = "thread_schedule"        # 调度器选中一个就绪分支
EV_LOCK_ACQUIRED = "lock_acquired"     # 获得互斥锁
EV_LOCK_WAIT = "lock_wait"             # 锁被占用，线程阻塞
EV_FORK = "fork_branches"              # fork：派生分支线程
EV_JOIN_ARRIVE = "join_arrive"         # 分支到达 join，交出汇合令牌
EV_JOIN_FIRE = "join_fire"             # 全部分支到达，汇合后续跑
EV_BRANCH_ENDED = "branch_ended_unjoinable"  # 分支在到达 join 前正常结束，永不汇合

EVENT_LABELS = {
    EV_SCHEDULE: "调度分支线程",
    EV_LOCK_ACQUIRED: "获得互斥锁",
    EV_LOCK_WAIT: "等待互斥锁（阻塞）",
    EV_FORK: "fork：派生并发分支",
    EV_JOIN_ARRIVE: "分支到达 join，等待其他分支",
    EV_JOIN_FIRE: "全部分支到达，汇合后续跑",
    EV_BRANCH_ENDED: "分支在汇合前正常结束：无法汇合",
}
EVENT_LABELS.update(W.EVENT_LABELS)

# 问题类别（稳定优先级；同深度多类问题时主见证的选择顺序）
FIND_RACE = "data_race"
FIND_DEADLOCK = "deadlock"
FIND_UNJOINABLE = "unjoinable"
FIND_INVARIANT = "invariant"
FIND_KINDS = (FIND_RACE, FIND_DEADLOCK, FIND_UNJOINABLE, FIND_INVARIANT)
FIND_LABELS = {
    FIND_RACE: "数据竞态",
    FIND_DEADLOCK: "锁顺序死锁",
    FIND_UNJOINABLE: "无法汇合的分支",
    FIND_INVARIANT: "不变量被破坏",
}

# 线程状态
T_READY = "ready"            # 已派生/已进入活动，等待调度器选中
T_ACQUIRING = "acquiring"    # 正在按序获取锁
T_ACTIVE = "active"          # 活动可调用（交错分叉点）
T_RETRYING = "retrying"
T_RECOVERING = "recovering"
T_WAITING = "waiting"        # 等待锁
T_JOINED = "joined"          # 已到达 join 交出令牌（保留快照信息）
T_FORKED = "forked"          # fork 后父线程终止，等待汇合
T_ENDED = "ended"            # 分支线程在到达 join 前正常结束（欠汇合令牌，无法汇合）
T_CANCELLED = "cancelled"    # 其他分支失败，本分支随全局补偿撤销


class ConcError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def _tid(n: int) -> str:
    # 定宽，保证 tid 的字典序与派生顺序一致（稳定事件序）
    return f"t{n:04d}"


def _branch_tid(gid: str, idx: int) -> str:
    """分支线程的稳定标识：汇合组 id + 出边序号。

    不随调度顺序变化（改选调度、分叉重放都能精确匹配）；同组最多两个分支时
    形如 b_pay_00。汇合后续跑线程仍用全局计数器。
    """
    safe = "".join(ch if ch.isalnum() else "_" for ch in gid)[:8]
    return f"b_{safe}_{idx:02d}"


def _continue_tid(state: dict) -> str:
    """汇合后续跑线程的稳定且不与分支 tid 冲突的标识。"""
    n = state["next_tid"]
    while True:
        cand = _tid(n)
        state["next_tid"] = n + 1
        n += 1
        if cand not in state["threads"]:
            return cand


def c_initial_state(wf: W.Workflow, init_vars: dict[str, Any] | None = None) -> dict:
    variables = {name: (False if t == "bool" else 0) for name, t in wf.variables.items()}
    if init_vars:
        for k, v in init_vars.items():
            if k in variables:
                variables[k] = bool(v) if wf.variables[k] == "bool" else int(v)
    return {
        "phase": W.PHASE_RUN,
        "variables": variables,
        "effects": [],
        "outcome": None,
        "threads": {
            "t0000": {
                "tid": "t0000", "status": T_ACTIVE, "activity": wf.start,
                "tries": 0, "need_locks": [], "have_locks": [],
                "clock": {"t0000": 0}, "ambiguous": None, "token_stack": [],
            },
        },
        "next_tid": 1,
        "locks": {lk: None for lk in wf.locks},
        "waiters": {},                       # tid -> 等待的锁
        "joins": {},                         # gid -> {needed, arrivals: {tid: clock}}
        "res_history": {r: [] for r in wf.resources},
        "done_counts": {aid: 0 for aid in wf.activities},
        "comp_stack": [],
        "comp_tries": {},
        "comp_retrying": None,
        "findings_seen": [],
    }


def _clone(state: dict) -> dict:
    return {
        "phase": state["phase"],
        "variables": dict(state["variables"]),
        "effects": [dict(e) for e in state["effects"]],
        "outcome": state["outcome"],
        "threads": {
            tid: {
                "tid": t["tid"], "status": t["status"], "activity": t["activity"],
                "tries": t["tries"], "need_locks": list(t["need_locks"]),
                "have_locks": list(t["have_locks"]),
                "clock": dict(t["clock"]),
                "ambiguous": (dict(t["ambiguous"]) if t.get("ambiguous") else None),
                "token_stack": list(t["token_stack"]),
            }
            for tid, t in state["threads"].items()
        },
        "next_tid": state["next_tid"],
        "locks": dict(state["locks"]),
        "waiters": dict(state["waiters"]),
        "joins": {
            gid: {"needed": j["needed"], "arrivals": dict(j["arrivals"])}
            for gid, j in state["joins"].items()
        },
        "res_history": {k: [dict(h) for h in hist]
                        for k, hist in state["res_history"].items()},
        "done_counts": dict(state["done_counts"]),
        "comp_stack": list(state["comp_stack"]),
        "comp_tries": dict(state["comp_tries"]),
        "comp_retrying": state["comp_retrying"],
        "findings_seen": [tuple(x) for x in state["findings_seen"]],
    }


def c_fingerprint(state: dict) -> str:
    import json
    threads = tuple(sorted(
        (t["tid"], t["status"], t["activity"], t["tries"],
         tuple(t["need_locks"]), tuple(t["have_locks"]),
         tuple(sorted(t["clock"].items())),
         (None if not t["ambiguous"]
          else (t["ambiguous"]["activity"], t["ambiguous"]["attempt"])),
         tuple(t["token_stack"]))
        for t in state["threads"].values()
    ))
    locks = tuple(sorted(state["locks"].items()))
    waiters = tuple(sorted(state["waiters"].items()))
    joins = tuple(sorted(
        (gid, j["needed"], tuple(sorted(
            (tid, tuple(sorted(clock.items()))) for tid, clock in j["arrivals"].items()
        )))
        for gid, j in state["joins"].items()
    ))
    history = tuple(
        (key, tuple((h["mode"], h["tid"], h["vc"], tuple(h["locks"])) for h in hist))
        for key, hist in sorted(state["res_history"].items())
    )
    effects = tuple((e["key"], e["activity"], 1 if e["undone"] else 0,
                     1 if e.get("duplicate") else 0) for e in state["effects"])
    return json.dumps(
        [state["phase"], state["outcome"],
         tuple(sorted(state["variables"].items())), effects,
         tuple(state["comp_stack"]), tuple(sorted(state["comp_tries"].items())),
         state["comp_retrying"], threads, locks, waiters, joins, history,
         tuple(state["findings_seen"])],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# 向量时钟
# ---------------------------------------------------------------------------

def _tick(t: dict) -> None:
    t["clock"][t["tid"]] = t["clock"].get(t["tid"], 0) + 1


def _merge_into(clock: dict, other: dict) -> None:
    for k, v in other.items():
        if v > clock.get(k, 0):
            clock[k] = v


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------

def _event(kind: str, tid: str, activity: str | None, attempt: int | None = None,
           **extra) -> dict:
    sig: tuple = (kind, tid)
    if activity is not None:
        sig = (*sig, activity)
    if attempt is not None:
        sig = (*sig, attempt)
    ev = {"type": kind, "thread": tid, "activity": activity, "attempt": attempt,
          "label": EVENT_LABELS.get(kind, kind), "sig": list(sig)}
    ev.update(extra)
    return ev


# ---------------------------------------------------------------------------
# 不变量（与 checker.check_invariants 同构，状态来源为并发状态）
# ---------------------------------------------------------------------------

def _ctx(state: dict) -> dict:
    effects: dict[str, int] = {}
    undone: dict[str, int] = {}
    for e in state["effects"]:
        if e["undone"]:
            undone[e["key"]] = undone.get(e["key"], 0) + 1
        else:
            effects[e["key"]] = effects.get(e["key"], 0) + 1
    return {"succ": dict(state["done_counts"]), "effects": effects, "undone": undone}


def _functions(state: dict):
    c = _ctx(state)
    return {
        "succ": lambda key: c["succ"].get(key, 0),
        "effects": lambda key: c["effects"].get(key, 0),
        "undone": lambda key: c["undone"].get(key, 0),
    }


def check_invariants(wf: W.Workflow, state: dict) -> list[dict]:
    violations: list[dict] = []
    if wf.invariant_at_most_once:
        dup_keys = [e["key"] for e in state["effects"]
                    if not e["undone"] and e.get("duplicate")]
        if dup_keys:
            key = dup_keys[0]
            violations.append({
                "id": "at_most_once",
                "name": "至多一次生效",
                "detail": (f"外部效果「{key}」被重复生效：超时重试后发现首次调用其实也已完成"
                           "（非幂等活动的“至少一次”交付），当前存在多条未撤销的生效记录"),
                "effect": key,
            })
    if wf.invariant_undo_on_failure and state.get("outcome") == "failed":
        pending = [e["key"] for e in state["effects"] if not e["undone"]]
        if pending:
            violations.append({
                "id": "undo_on_failure",
                "name": "失败后最终撤销",
                "detail": "工作流失败，但仍有外部效果未撤销：" + ", ".join(pending),
                "effects": pending,
            })
    funcs = _functions(state)
    for name, node in wf.invariants.items():
        try:
            if not bool(E.eval_node(node, state["variables"], funcs)):
                violations.append({
                    "id": f"custom:{name}",
                    "name": name,
                    "detail": f"自定义不变量「{name}」在当前并发状态下为假",
                })
        except E.ExpressionError as ex:
            raise ConcError(f"不变量「{name}」求值失败：{ex}") from ex
    return violations


# ---------------------------------------------------------------------------
# 快照与备选（供逐步回放）
# ---------------------------------------------------------------------------

_LIVE_STATUS = (T_ACTIVE, T_ACQUIRING, T_READY, T_RETRYING, T_RECOVERING, T_WAITING)
_STATUS_LABEL = {
    T_READY: "就绪待调度", T_ACQUIRING: "获取锁中", T_ACTIVE: "执行中",
    T_RETRYING: "重试中", T_RECOVERING: "恢复中", T_WAITING: "锁阻塞",
    T_JOINED: "已到汇合点", T_FORKED: "已分叉", T_ENDED: "汇合前已结束",
    T_CANCELLED: "已随失败撤销",
}


def snapshot(wf: W.Workflow, state: dict) -> dict:
    c = _ctx(state)
    # 活动聚合视图：成功计数优先；其次展示任一存活分支上的阶段
    live_by_act: dict[str, str] = {}
    tries_by_act: dict[str, int] = {}
    for t in state["threads"].values():
        aid = t["activity"]
        if aid is None:
            continue
        tries_by_act[aid] = max(tries_by_act.get(aid, 0), t["tries"])
        if t["status"] in _LIVE_STATUS and live_by_act.get(aid) not in (W.ACTIVE,):
            order = {T_ACTIVE: 0, T_RETRYING: 1, T_RECOVERING: 1,
                     T_ACQUIRING: 2, T_WAITING: 2, T_READY: 3}
            if aid not in live_by_act or order.get(t["status"], 9) < order.get(live_by_act[aid], 9):
                live_by_act[aid] = {
                    T_ACTIVE: W.ACTIVE, T_RETRYING: W.RETRYING,
                    T_RECOVERING: W.RECOVERING, T_ACQUIRING: W.ACTIVE,
                    T_WAITING: W.ACTIVE, T_READY: W.PENDING,
                }[t["status"]]
    activities = []
    for aid in sorted(wf.activities):
        if state["done_counts"].get(aid):
            status, tries = W.DONE, max(state["done_counts"][aid], tries_by_act.get(aid, 0))
        elif aid in live_by_act:
            status, tries = live_by_act[aid], tries_by_act.get(aid, 0)
        else:
            status, tries = W.PENDING, tries_by_act.get(aid, 0)
        activities.append({
            "id": aid, "name": wf.act(aid).name, "status": status, "tries": tries,
            "effect": wf.act(aid).effect,
        })

    threads = []
    for tid in sorted(state["threads"]):
        t = state["threads"][tid]
        g = wf.act(t["activity"]) if t["activity"] in wf.activities else None
        threads.append({
            "tid": tid,
            "status": t["status"],
            "status_label": _STATUS_LABEL.get(t["status"], t["status"]),
            "activity": t["activity"],
            "activity_name": g.name if g else None,
            "tries": t["tries"],
            "have_locks": list(t["have_locks"]),
            "need_locks": list(t["need_locks"]),
            "waits_on": state["waiters"].get(tid),
            "groups": list(t["token_stack"]),
            "clock": dict(sorted(t["clock"].items())),
        })

    return {
        "phase": state["phase"],
        "current": None,
        "outcome": state["outcome"],
        "concurrent": True,
        "activities": activities,
        "threads": threads,
        "locks": [{"name": lk, "owner": owner}
                  for lk, owner in sorted(state["locks"].items())],
        "joins": [
            {"group": gid, "fork": wf.conc_groups[gid].fork,
             "join": wf.conc_groups[gid].join,
             "needed": j["needed"], "arrived": sorted(j["arrivals"]),
             "pending": sorted(set(range(j["needed"])))}
            for gid, j in sorted(state["joins"].items())
        ],
        "resources": [
            {"key": key, "history": [
                {"mode": h["mode"], "tid": h["tid"], "vc": h["vc"],
                 "locks": list(h["locks"])}
                for h in hist]}
            for key, hist in sorted(state["res_history"].items())
        ],
        "variables": dict(sorted(state["variables"].items())),
        "effects": [
            {"key": e["key"], "activity": e["activity"], "undone": bool(e["undone"]),
             "duplicate": bool(e.get("duplicate"))}
            for e in state["effects"]
        ],
        "counts": {"succ": c["succ"], "effects": c["effects"], "undone": c["undone"]},
        "comp_stack": [state["effects"][i]["activity"] for i in state["comp_stack"]],
        "comp_tries": dict(state["comp_tries"]),
    }


# ---------------------------------------------------------------------------
# 后继枚举
# ---------------------------------------------------------------------------

def successors(wf: W.Workflow, state: dict) -> list[tuple[dict, dict]]:
    if state["outcome"] is not None:
        return []
    if state["phase"] == W.PHASE_COMP:
        out = _comp_successors(wf, state)
    else:
        out = []
        for tid in sorted(state["threads"]):
            out.extend(_thread_successors(wf, state, tid))
    out.sort(key=lambda pair: tuple(pair[1]["sig"]))
    return out


def _alive(t: dict) -> bool:
    return t["status"] not in (T_JOINED, T_FORKED, T_ENDED, T_CANCELLED)


def _record_effect(state: dict, activity: W.Activity, duplicate: bool = False) -> bool:
    if not duplicate and not activity.idempotent:
        for e in state["effects"]:
            if e["key"] == activity.effect and not e["undone"]:
                duplicate = True
                break
    state["effects"].append({"key": activity.effect, "activity": activity.id,
                             "undone": False, "duplicate": bool(duplicate)})
    return bool(duplicate)


def _eval_bool(node, state: dict) -> bool:
    return bool(E.eval_node(node, state["variables"], _functions(state)))


def _thread_successors(wf: W.Workflow, state: dict, tid: str) -> list[tuple[dict, dict]]:
    t = state["threads"][tid]
    st = t["status"]
    if st == T_READY:
        return [_schedule(wf, state, tid)]
    if st == T_ACQUIRING:
        return [_try_acquire(wf, state, tid)]
    if st == T_RETRYING:
        return [_deterministic_retry(wf, state, tid)]
    if st == T_RECOVERING:
        return [_deterministic_recover(wf, state, tid)]
    if st == T_WAITING:
        # 锁仍被占用 => 继续阻塞；锁已释放 => 调度器可再次选中本线程尝试获取
        lock = state["waiters"].get(tid)
        if lock is not None and state["locks"].get(lock) is None:
            s = _clone(state)
            s["threads"][tid]["status"] = T_ACQUIRING
            del s["waiters"][tid]
            return [_try_acquire(wf, s, tid)]
        return []
    if st == T_ACTIVE:
        return _active_successors(wf, state, tid)
    return []


def _schedule(wf: W.Workflow, state: dict, tid: str) -> tuple[dict, dict]:
    s = _clone(state)
    t = s["threads"][tid]
    _tick(t)
    a = wf.act(t["activity"])
    if a.locks:
        t["status"] = T_ACQUIRING
        t["need_locks"] = list(a.locks)
    else:
        t["status"] = T_ACTIVE
    ev = _event(EV_SCHEDULE, tid, t["activity"], None,
                target_status=t["status"], need_locks=list(a.locks))
    return s, ev


def _try_acquire(wf: W.Workflow, state: dict, tid: str) -> tuple[dict, dict]:
    s = _clone(state)
    t = s["threads"][tid]
    lock = t["need_locks"][0]
    owner = s["locks"].get(lock)
    if owner is not None and owner != tid:
        # 阻塞：记录等待关系并检测等待环（锁顺序死锁）
        t["status"] = T_WAITING
        s["waiters"][tid] = lock
        ev = _event(EV_LOCK_WAIT, tid, t["activity"], None, lock=lock,
                    holder=owner, note=f"锁 {lock} 被线程 {owner} 持有")
        cycle = _wait_cycle(s, tid)
        if cycle:
            s["outcome"] = "deadlocked"
            ev["deadlock"] = {"cycle": cycle, "lock": lock}
            ev["note"] = "等待环形成锁顺序死锁：" + " → ".join(
                f"{x['tid']}({x['activity']})等{x['waits_lock']}" for x in cycle)
        return s, ev
    # 获得锁
    _tick(t)
    s["locks"][lock] = tid
    t["have_locks"].append(lock)
    t["need_locks"] = t["need_locks"][1:]
    if not t["need_locks"]:
        t["status"] = T_ACTIVE
    else:
        t["status"] = T_ACQUIRING
    ev = _event(EV_LOCK_ACQUIRED, tid, t["activity"], None, lock=lock,
                remaining=list(t["need_locks"]),
                note=f"获得锁 {lock}" + (f"，继续申请 {', '.join(t['need_locks'])}"
                                        if t["need_locks"] else ""))
    return s, ev


def _wait_cycle(state: dict, start: str) -> list[dict] | None:
    """从 start 沿 等待锁->持有者 链找环，返回环上的 [(tid, activity, waits_lock)]。"""
    path: list[str] = []
    cur = start
    seen: set[str] = set()
    while cur in state["waiters"]:
        if cur in seen:
            i = path.index(cur)
            ring = path[i:]
            return [{"tid": x,
                     "activity": state["threads"][x]["activity"],
                     "waits_lock": state["waiters"][x],
                     "holder": state["locks"].get(state["waiters"][x])}
                    for x in ring]
        seen.add(cur)
        path.append(cur)
        held = state["waiters"][cur]
        cur = state["locks"].get(held)
        if cur is None:
            return None
    return None


def _deterministic_retry(wf: W.Workflow, state: dict, tid: str) -> tuple[dict, dict]:
    s = _clone(state)
    t = s["threads"][tid]
    _tick(t)
    if t.get("ambiguous"):
        t["status"] = T_ACTIVE
    else:
        t["status"] = T_ACTIVE
    return s, _event(W.EV_RETRY, tid, t["activity"], attempt=t["tries"],
                     ambiguous=bool(t.get("ambiguous")),
                     note="错误已持久化，重新入队调用" if not t.get("ambiguous")
                     else "应答丢失且无法查询真相，按超时重新调用")


def _deterministic_recover(wf: W.Workflow, state: dict, tid: str) -> tuple[dict, dict]:
    s = _clone(state)
    t = s["threads"][tid]
    _tick(t)
    t["status"] = T_ACTIVE
    return s, _event(W.EV_RECOVER, tid, t["activity"], attempt=t["tries"] + 1,
                     note="进程恢复：崩溃发生在写检查点之前，活动无持久痕迹，"
                          "重新调用同一活动（尝试次数不增加，未产生任何外部效果）")


# ---- ACTIVE：活动交错（移植 workflow._run_successors 的分支结构） -----------

def _active_successors(wf: W.Workflow, state: dict, tid: str) -> list[tuple[dict, dict]]:
    t = state["threads"][tid]
    a = wf.act(t["activity"])
    attempt = t["tries"]
    amb = t.get("ambiguous")
    results: list[tuple[dict, dict]] = []

    # 1) 成功
    if amb is not None:
        results.append(_finish_success(wf, state, tid, True))
        results.append(_finish_success(wf, state, tid, False))
    else:
        results.append(_finish_success(wf, state, tid, None))

    if a.can_fail:
        if a.retryable and attempt + 1 < a.max_attempts:
            s = _clone(state)
            tt = s["threads"][tid]
            _tick(tt)
            tt["tries"] = attempt + 1
            tt["status"] = T_RETRYING
            if amb is not None:
                tt["ambiguous"] = {"activity": a.id, "attempt": amb["attempt"]}
            results.append((s, _event(W.EV_RETRYABLE, tid, a.id, attempt=attempt + 1,
                                      note=f"收到可重试错误，已持久化第 {attempt + 1} 次失败")))
        else:
            s = _clone(state)
            tt = s["threads"][tid]
            _tick(tt)
            tt["tries"] = attempt + 1
            note = "最大尝试次数已用尽，放弃正向流程"
            if amb is not None and a.effect:
                _record_effect(s, a, duplicate=False)
                note += "；存疑的首次调用按“可能已生效”登记，补偿阶段尝试撤销"
            ev = _event(W.EV_EXHAUSTED, tid, a.id, attempt=attempt + 1, note=note)
            _fail_global(wf, s, tid, ev)
            results.append((s, ev))

        s = _clone(state)
        tt = s["threads"][tid]
        _tick(tt)
        tt["tries"] = attempt + 1
        note = "收到不可重试错误，立即放弃正向流程"
        if amb is not None and a.effect:
            _record_effect(s, a, duplicate=False)
            note += "；存疑的首次调用按“可能已生效”登记，补偿阶段尝试撤销"
        ev = _event(W.EV_FATAL, tid, a.id, attempt=attempt + 1, note=note)
        _fail_global(wf, s, tid, ev)
        results.append((s, ev))

    if a.pre_crash:
        s = _clone(state)
        tt = s["threads"][tid]
        _tick(tt)
        tt["status"] = T_RECOVERING
        results.append((s, _event(W.EV_PRE_CRASH, tid, a.id, attempt=attempt + 1,
                                  note="进程在写检查点之前崩溃，活动无任何持久痕迹，"
                                       "进入恢复；恢复后重新调用同一活动")))

    if a.post_crash:
        results.append(_finish_ack(wf, state, tid))
        s = _clone(state)
        tt = s["threads"][tid]
        _tick(tt)
        tt["status"] = T_RETRYING
        tt["ambiguous"] = {"activity": a.id, "attempt": attempt + 1}
        results.append((s, _event(W.EV_RETRY, tid, a.id, attempt=attempt + 1,
                                  ambiguous=True,
                                  note="应答丢失且无法查询真相，按超时重新调用；"
                                       "首次调用是否已生效将在重试结果中分叉")))
    return results


def _apply_accesses(wf: W.Workflow, s: dict, tid: str, ev: dict) -> None:
    """成功访问共享资源：向量时钟竞态判定（同锁保护的访问不算竞态）。"""
    t = s["threads"][tid]
    held = frozenset(t["have_locks"])
    for key, mode in [*( (k, "w") for k in wf.act(t["activity"]).writes),
                      *( (k, "r") for k in wf.act(t["activity"]).reads)]:
        hist = s["res_history"][key]
        own_vc = t["clock"][tid]
        clock = t["clock"]
        found = None
        for h in hist:
            if h["tid"] == tid:
                continue
            if mode == "r" and h["mode"] == "r":
                continue  # 读读不冲突
            if held and held.intersection(h["locks"]):
                continue  # 公共锁：互斥/有序，不算竞态
            # h 与当前访问并发 <=> 当前时钟未观察到 h 的那次滴答
            if clock.get(h["tid"], 0) < h["vc"]:
                found = h
                break
        if found is not None:
            rkey = (FIND_RACE, key, found["tid"], found["mode"], tid, mode)
            if rkey not in s["findings_seen"]:
                s["findings_seen"].append(rkey)
            ev["race"] = {
                "resource": key,
                "access": {"mode": mode, "tid": tid, "activity": t["activity"],
                           "locks": sorted(held)},
                "prior": {"mode": found["mode"], "tid": found["tid"],
                          "activity": found["activity"], "locks": list(found["locks"])},
                "reason": ("写-写冲突" if mode == "w" and found["mode"] == "w"
                           else "读-写冲突（无 happens-before 关系且无公共锁保护）"),
            }
        hist.append({"mode": mode, "tid": tid, "activity": t["activity"],
                     "vc": own_vc, "locks": tuple(sorted(held))})


def _release_locks(s: dict, tid: str) -> None:
    t = s["threads"][tid]
    for lk in t["have_locks"]:
        if s["locks"].get(lk) == tid:
            s["locks"][lk] = None
    t["have_locks"] = []
    t["need_locks"] = []


def _finish_success(wf: W.Workflow, state: dict, tid: str,
                    dup: bool | None) -> tuple[dict, dict]:
    s = _clone(state)
    t = s["threads"][tid]
    a = wf.act(t["activity"])
    attempt = t["tries"]
    amb = t.get("ambiguous")
    _tick(t)
    t["tries"] = attempt + 1
    for var, expr in a.assign.items():
        s["variables"][var] = E.eval_node(expr, s["variables"], _functions(s))
    if a.effect:
        if dup is True and not a.idempotent:
            _record_effect(s, a, duplicate=False)
            _record_effect(s, a, duplicate=True)
        else:
            _record_effect(s, a, duplicate=False)
    duplicate = any(e.get("duplicate") and not e["undone"] and e["activity"] == a.id
                    for e in s["effects"])
    s["done_counts"][a.id] += 1
    if dup is None:
        ev = _event(W.EV_SUCCESS, tid, a.id, attempt=attempt + 1,
                    effect=a.effect, duplicate=duplicate)
    else:
        kind = W.EV_RETRY_DUP if dup else W.EV_RETRY_CLEAN
        ev = _event(kind, tid, a.id, attempt=attempt + 1, effect=a.effect,
                    duplicate=duplicate, first_attempt=(amb["attempt"] if amb else None))
    _apply_accesses(wf, s, tid, ev)
    t["ambiguous"] = None
    _release_locks(s, tid)
    _advance_or_fork(wf, s, tid, a, ev)
    return s, ev


def _finish_ack(wf: W.Workflow, state: dict, tid: str) -> tuple[dict, dict]:
    s = _clone(state)
    t = s["threads"][tid]
    a = wf.act(t["activity"])
    attempt = t["tries"]
    _tick(t)
    t["tries"] = attempt + 1
    duplicate = False
    if a.effect:
        duplicate = _record_effect(s, a)
    s["done_counts"][a.id] += 1
    t["ambiguous"] = None
    ev = _event(W.EV_ACK, tid, a.id, attempt=attempt + 1, effect=a.effect,
                duplicate=duplicate,
                note="调用其实已在外部生效；恢复时通过查询确认，无需重试")
    _apply_accesses(wf, s, tid, ev)
    _release_locks(s, tid)
    _advance_or_fork(wf, s, tid, a, ev)
    return s, ev


def _select_edges(wf: W.Workflow, s: dict, a: W.Activity, ev: dict) -> list[W.Edge]:
    chosen: list[W.Edge] = []
    blocked: list[dict] = []
    for e in wf.out_edges.get(a.id, []):
        if e.guard is not None and not _eval_bool(e.guard, s):
            blocked.append({"target": e.dst, "reason": "guard_false",
                            "detail": e.guard_src})
            continue
        dst = wf.act(e.dst)
        if dst.pre is not None and not _eval_bool(dst.pre, s):
            blocked.append({"target": e.dst, "reason": "precondition_false"})
            continue
        chosen.append(e)
    ev["blocked"] = blocked
    return chosen


def _fire_join(wf: W.Workflow, s: dict, gid: str, ev: dict,
               arriving_tid: str | None) -> None:
    """全部汇合令牌到齐：合并分支时钟，新建续跑线程执行 join 活动。"""
    join_rec = s["joins"][gid]
    dst = wf.act(wf.conc_groups[gid].join)
    merged: dict[str, int] = {}
    for clock in join_rec["arrivals"].values():
        _merge_into(merged, clock)
    for x in list(join_rec["arrivals"]):
        s["threads"][x]["status"] = T_JOINED
    base_stack = (s["threads"][arriving_tid]["token_stack"]
                  if arriving_tid and arriving_tid in s["threads"]
                     and gid in s["threads"][arriving_tid].get("token_stack", [])
                  else [gid])
    del s["joins"][gid]
    nid = _continue_tid(s)
    merged[nid] = merged.get(nid, 0)
    s["threads"][nid] = {
        "tid": nid, "status": T_READY, "activity": dst.id, "tries": 0,
        "need_locks": [], "have_locks": [], "clock": merged,
        "ambiguous": None,
        "token_stack": base_stack[:-1],
    }
    ev["type"] = EV_JOIN_FIRE
    ev["label"] = EVENT_LABELS[EV_JOIN_FIRE]
    ev["sig"] = [EV_JOIN_FIRE, nid, gid]
    ev["thread"] = nid
    ev["target"] = dst.id
    ev["join_group"] = gid
    ev["join_continue_tid"] = nid
    ev["join_arrived"] = join_rec["needed"]
    ev["join_needed"] = join_rec["needed"]
    ev["note"] = (ev.get("note") or "") + (
        f" 全部分支到达，汇合组 {gid} 闭合，续跑线程 {nid} 执行 {dst.id}")


def _advance_or_fork(wf: W.Workflow, s: dict, tid: str, a: W.Activity, ev: dict) -> None:
    """活动成功后的控制流：fork 派生分支 / join 交令牌 / 普通沿边推进。"""
    # fork：每条生效出边一个分支线程
    if a.fork_group:
        edges = _select_edges(wf, s, a, ev)
        gid = a.fork_group
        if not edges:
            ev["note"] = ((ev.get("note") or "") +
                          " fork 的所有出边均未通过守卫/前置，正向流程失败").strip()
            _enter_compensation(wf, s, ev)
            return
        parent = s["threads"][tid]
        parent["status"] = T_FORKED
        parent["activity"] = a.id
        branches = []
        arrivals: dict[str, dict] = {}
        for bi, e in enumerate(edges):
            nid = _branch_tid(gid, bi)
            clock = dict(parent["clock"])
            gjoin = wf.conc_groups[gid].join
            target_thread = {
                "tid": nid, "status": T_READY, "activity": e.dst, "tries": 0,
                "need_locks": [], "have_locks": [], "clock": clock,
                "ambiguous": None,
                "token_stack": parent["token_stack"] + [gid],
            }
            s["threads"][nid] = target_thread
            branches.append({"tid": nid, "target": e.dst})
            # 边直接指向本组 join：该分支没有区域活动，fork 时立即交令牌
            if e.dst == gjoin:
                arrivals[nid] = dict(clock)
                target_thread["status"] = T_JOINED
                target_thread["activity"] = gjoin
        s["joins"][gid] = {"needed": len(edges), "arrivals": arrivals}
        ev["fork_group"] = gid
        ev["branches"] = branches
        ev["target"] = None
        note = f" fork 汇合组 {gid}：派生 {len(edges)} 个分支（" \
               + ", ".join(f"{b['tid']}→{b['target']}" for b in branches) + "）"
        if arrivals:
            note += f"；其中 {len(arrivals)} 个分支直接到达 join 并立即交令牌"
        ev["note"] = (ev.get("note") or "") + note
        # 全部分支都直连 join：立刻汇合，新建续跑线程执行 join 活动
        if len(arrivals) == len(edges):
            _fire_join(wf, s, gid, ev, tid)
        return

    edges = _select_edges(wf, s, a, ev)
    t = s["threads"][tid]

    # join：沿边进入的是本组 join 活动 => 交令牌
    if edges:
        dst0 = wf.act(edges[0].dst)
        gid = dst0.join_group
        if gid and t["token_stack"] and t["token_stack"][-1] == gid \
                and gid in s["joins"]:
            join_rec = s["joins"][gid]
            join_rec["arrivals"][tid] = dict(t["clock"])
            t["status"] = T_JOINED
            t["activity"] = dst0.id
            ev["target"] = dst0.id
            ev["join_group"] = gid
            ev["join_arrived"] = len(join_rec["arrivals"])
            ev["join_needed"] = join_rec["needed"]
            if len(join_rec["arrivals"]) < join_rec["needed"]:
                # 防御性封存：欠令牌且已无其他可运行分支（例如所有分支都在
                # join 前终止）。正常模型下该状态由分支结束转移先行封存，
                # 这里保证任何遗漏路径都不会产生「无后继又非终态」的卡死状态。
                if not any(_alive(x) for x in s["threads"].values()):
                    _seal_unjoinable(wf, s, ev, tid, a.id, gid)
                    return
                ev["type"] = EV_JOIN_ARRIVE
                ev["label"] = EVENT_LABELS[EV_JOIN_ARRIVE]
                ev["sig"] = [EV_JOIN_ARRIVE, tid, gid]
                ev["note"] = (ev.get("note") or "") + (
                    f" 分支 {tid} 到达汇合点 {dst0.id}，等待其他分支"
                    f"（{len(join_rec['arrivals'])}/{join_rec['needed']}）")
                return
            # 最后一个分支到达：汇合后续跑
            _fire_join(wf, s, gid, ev, tid)
            return

    # 普通推进（串行语义：第一条生效出边）
    if edges:
        e = edges[0]
        t["status"] = T_READY
        t["activity"] = e.dst
        t["tries"] = 0
        t["ambiguous"] = None
        ev["target"] = e.dst
        return

    if not wf.out_edges.get(a.id):
        ev["target"] = None
        # 分支线程仍持有未闭合汇合组的令牌：它在到达 join 前正常结束，
        # 令牌永远无法交齐——在该转移上封存「无法汇合」终态（与步数边界无关）。
        gid = t["token_stack"][-1] if t["token_stack"] else None
        if gid and gid in s["joins"]:
            _seal_unjoinable(wf, s, ev, tid, a.id, gid)
            return
        t["status"] = T_JOINED
        t["activity"] = a.id
        ev["note"] = (ev.get("note") or "") + " 无出边，分支结束"
        _maybe_succeed(wf, s, ev)
    else:
        ev["note"] = (ev.get("note", "") +
                      " 守卫通过的分支中没有前置条件成立的后继，正向流程失败").strip()
        _enter_compensation(wf, s, ev)


def _seal_unjoinable(wf: W.Workflow, s: dict, ev: dict, tid: str,
                     aid: str, gid: str) -> None:
    """分支在到达 join 前正常结束（或最后到达者发现再无其他可运行分支）：
    汇合令牌永远无法交齐。把该转移封存为「无法汇合」终态，事件携带完整见证信息。
    """
    t = s["threads"][tid]
    t["status"] = T_ENDED
    t["activity"] = aid
    s["outcome"] = FIND_UNJOINABLE
    ev["type"] = EV_BRANCH_ENDED
    ev["label"] = EVENT_LABELS[EV_BRANCH_ENDED]
    ev["sig"] = [EV_BRANCH_ENDED, tid, gid]
    ev["target"] = None
    ev["join_group"] = gid
    ev["unjoinable"] = _unjoinable_info(wf, s, terminal=True, ended_tid=tid)
    g = wf.conc_groups[gid]
    ev["note"] = (ev.get("note") or "") + (
        f" 分支 {tid} 在活动 {aid} 正常结束、未到达汇合点 {g.join}，"
        "汇合令牌无法交齐，汇合组永不闭合")


def _maybe_succeed(wf: W.Workflow, s: dict, ev: dict) -> None:
    """没有存活线程且无未闭合汇合组时，工作流成功完成。"""
    if any(_alive(t) for t in s["threads"].values()):
        return
    if s["joins"]:
        # 所有分支都已结束但汇合组未闭合：结构上应已由分支结束转移封存为
        # 「无法汇合」终态；兜底收集，避免无后继又非终态的“卡死”状态漏报。
        gid = next(iter(s["joins"]))
        ended = next((x for x in s["threads"].values()
                      if gid in x.get("token_stack", [])
                      and x["tid"] not in s["joins"][gid]["arrivals"]), None)
        if ended is not None:
            _seal_unjoinable(wf, s, ev, ended["tid"], ended["activity"], gid)
        else:
            g = wf.conc_groups[gid]
            _seal_unjoinable(wf, s, ev, next(iter(s["threads"])), g.fork, gid)
        return
    s["outcome"] = "succeeded"
    ev["note"] = (ev.get("note") or "") + " 全部分支结束，工作流正常完成"


def _fail_global(wf: W.Workflow, s: dict, fail_tid: str, ev: dict) -> None:
    """一个分支失败：释放全部锁、撤销其余分支，进入全局 LIFO 补偿。"""
    for tid, t in s["threads"].items():
        if tid == fail_tid:
            t["status"] = T_CANCELLED
        elif _alive(t):
            t["status"] = T_CANCELLED
        _release_locks(s, tid)
    s["waiters"] = {}
    _enter_compensation(wf, s, ev)


def _enter_compensation(wf: W.Workflow, s: dict, ev: dict) -> None:
    stack = [i for i, e in enumerate(s["effects"]) if not e["undone"]]
    s["phase"] = W.PHASE_COMP
    s["comp_stack"] = list(reversed(stack))
    s["comp_tries"] = {}
    s["comp_retrying"] = None
    s["waiters"] = {}
    s["joins"] = {}   # 已进入全局补偿，不存在“等待汇合”的分支
    if not stack:
        s["outcome"] = "compensated"
        ev["comp_note"] = "没有已生效的外部效果需要撤销"


# ---- 补偿阶段（与 workflow._comp_successors 同构） --------------------------

def _comp_successors(wf: W.Workflow, state: dict) -> list[tuple[dict, dict]]:
    if state.get("comp_retrying"):
        s = _clone(state)
        idx = s["comp_retrying"]
        aid = s["effects"][idx]["activity"]
        s["comp_retrying"] = None
        return [(s, _event(W.EV_COMP_RETRY, "comp", aid,
                           attempt=s["comp_tries"].get(aid, 0),
                           note="补偿错误已记录，重新调度补偿动作"))]
    stack = state["comp_stack"]
    if not stack:
        s = _clone(state)
        s["outcome"] = "compensated"
        return [(s, _event(W.EV_COMP_SUCCESS, "comp", None,
                           note="所有外部效果均已撤销"))]
    idx = stack[0]
    top = state["effects"][idx]
    a = wf.act(top["activity"])
    tries = state["comp_tries"].get(a.id, 0)
    results: list[tuple[dict, dict]] = []

    s = _clone(state)
    s["comp_tries"][a.id] = tries + 1
    s["effects"][idx]["undone"] = True
    s["comp_stack"] = stack[1:]
    nxt = s["effects"][stack[1]]["activity"] if len(stack) > 1 else None
    ev = _event(W.EV_COMP_SUCCESS, "comp", a.id, attempt=tries + 1,
                effect=top["key"], target=nxt)
    if not s["comp_stack"]:
        s["outcome"] = "compensated"
    results.append((s, ev))

    if not a.comp_can_fail:
        return results
    if a.comp_retryable and tries + 1 < a.comp_max_attempts:
        s = _clone(state)
        s["comp_tries"][a.id] = tries + 1
        s["comp_retrying"] = idx
        results.append((s, _event(W.EV_COMP_ERROR, "comp", a.id, attempt=tries + 1,
                                  note="补偿动作遇到可重试错误")))
    elif a.comp_retryable:
        s = _clone(state)
        s["comp_tries"][a.id] = tries + 1
        s["outcome"] = "failed"
        results.append((s, _event(W.EV_COMP_EXHAUSTED, "comp", a.id, attempt=tries + 1,
                                  note="补偿动作最大尝试次数用尽，外部效果永久残留")))
    s = _clone(state)
    s["comp_tries"][a.id] = tries + 1
    s["outcome"] = "failed"
    results.append((s, _event(W.EV_COMP_FATAL, "comp", a.id, attempt=tries + 1,
                              note="补偿动作自身不可重试地失败，外部效果永久残留")))
    return results


# ---------------------------------------------------------------------------
# BFS
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
    events, cur = [], node
    while cur.parent is not None:
        if cur.event is not None:
            events.append(cur.event)
        cur = cur.parent
    events.reverse()
    return events


def _tail(node: _Node, root: _Node) -> list[dict]:
    """还原 node 相对 BFS 子树根 root 之后的事件（不含 root 自身事件）。"""
    events, cur = [], node
    while cur is not root and cur.parent is not None:
        if cur.event is not None:
            events.append(cur.event)
        cur = cur.parent
    events.reverse()
    return events


def apply_event(wf: W.Workflow, state: dict, ev: dict) -> dict:
    target = tuple(ev["sig"]) if "sig" in ev else None
    for nxt, cand in successors(wf, state):
        if target is None or tuple(cand["sig"]) == target:
            return nxt
    raise ConcError("重放失败：轨迹事件在当前并发状态下不可触发（模型可能已修改）")


def alternatives(wf: W.Workflow, state: dict) -> list[dict]:
    return [dict(ev) for _, ev in successors(wf, state)]


def trace_with_states(wf: W.Workflow, events: list[dict], state0: dict,
                      prefix_len: int = 0) -> list[dict]:
    state = state0
    steps = [{
        "index": 0, "event": None, "snapshot": snapshot(wf, state),
        "violations": check_invariants(wf, state), "shared_prefix": True,
        "alternatives": alternatives(wf, state),
    }]
    for i, ev in enumerate(events):
        state = apply_event(wf, state, ev)
        steps.append({
            "index": i + 1, "event": dict(ev),
            "snapshot": snapshot(wf, state),
            "violations": check_invariants(wf, state),
            "shared_prefix": i < prefix_len,
            "alternatives": alternatives(wf, state),
        })
    return steps


def _mode_zh(mode: str) -> str:
    return "写" if mode == "w" else "读"


def _finding_from_event(wf: W.Workflow, node: _Node, ev: dict,
                        violations: list[dict]) -> tuple[str, tuple, dict] | None:
    if ev.get("race"):
        r = ev["race"]
        key = (FIND_RACE, r["resource"], r["prior"]["tid"], r["prior"]["mode"],
               r["access"]["tid"], r["access"]["mode"])
        info = {
            "kind": FIND_RACE,
            "kind_label": FIND_LABELS[FIND_RACE],
            "resource": r["resource"],
            "resources": [r["resource"]],
            "accesses": [r["prior"], r["access"]],
            "blocking": [],
            "detail": (f"共享资源「{r['resource']}」存在{r['reason']}：线程 "
                       f"{r['prior']['tid']} 在活动 {r['prior']['activity']} 上的"
                       f"{_mode_zh(r['prior']['mode'])}访问，与线程 {r['access']['tid']} "
                       f"在活动 {r['access']['activity']} 上的"
                       f"{_mode_zh(r['access']['mode'])}访问之间不存在 happens-before 关系，"
                       f"且两次访问没有公共锁保护（前者持锁 {r['prior']['locks'] or '无'}、"
                       f"后者持锁 {r['access']['locks'] or '无'}）。"),
        }
        return FIND_RACE, key, info
    if ev.get("deadlock"):
        d = ev["deadlock"]
        cycle = d["cycle"]
        key = (FIND_DEADLOCK, tuple(sorted((x["tid"], x["waits_lock"]) for x in cycle)))
        resources = sorted({x["waits_lock"] for x in cycle})
        info = {
            "kind": FIND_DEADLOCK,
            "kind_label": FIND_LABELS[FIND_DEADLOCK],
            "resources": resources,
            "locks": resources,
            "cycle": cycle,
            "blocking": [
                {"tid": x["tid"], "activity": x["activity"],
                 "reason": f"等待锁 {x['waits_lock']}（被线程 {x['holder']} 持有）"}
                for x in cycle],
            "detail": "锁获取顺序在分支间形成等待环：" + "；".join(
                f"线程 {x['tid']} 执行 {x['activity']} 时等待锁 {x['waits_lock']}，"
                f"该锁由线程 {x['holder']} 持有" for x in cycle) + "，无线程能继续。",
        }
        return FIND_DEADLOCK, key, info
    if ev.get("unjoinable"):
        info = dict(ev["unjoinable"])
        gids = tuple(sorted(g["group"] for g in info["groups"]))
        return FIND_UNJOINABLE, (FIND_UNJOINABLE, "terminal", gids), info
    if violations:
        v = violations[0]
        key = (FIND_INVARIANT, v["id"])
        info = {"kind": FIND_INVARIANT, "kind_label": FIND_LABELS[FIND_INVARIANT],
                "violations": violations, "resources": [], "blocking": [],
                "detail": v["detail"]}
        return FIND_INVARIANT, key, info
    return None


def _unjoinable_info(wf: W.Workflow, state: dict, *, terminal: bool = False,
                     ended_tid: str | None = None) -> dict:
    """构造「无法汇合的分支」见证信息。

    terminal=True：已封存的终态（分支在 join 前正常结束 / 全部线程不可继续且
    仍欠令牌），与步数边界无关；ended_tid 给出提前结束的分支（由封存事件标注）。
    terminal=False：步数边界处的截断呈现，仅描述仍在运行、尚未交令牌的分支。
    """
    pending_groups = []
    blocking = []
    resources: set[str] = set()
    for gid, j in sorted(state["joins"].items()):
        g = wf.conc_groups[gid]
        arrived = sorted(j["arrivals"])
        arrived_set = set(j["arrivals"])
        alive = [t for t in state["threads"].values()
                 if gid in t.get("token_stack", []) and _alive(t)]
        branches = []
        for t in alive:
            waits = state["waiters"].get(t["tid"])
            if waits:
                reason = (f"在活动 {t['activity']} 处等待锁 {waits}"
                          f"（持有者 {state['locks'].get(waits)}）")
                resources.add(waits)
            elif t["status"] == T_WAITING:
                reason = f"在活动 {t['activity']} 处阻塞"
            else:
                reason = f"仍在活动 {t['activity']} 上继续执行"
            blocking.append({"tid": t["tid"], "activity": t["activity"],
                             "group": gid, "reason": reason})
            branches.append({"tid": t["tid"], "activity": t["activity"],
                             "waits_on": waits})
        # 终态呈现：已终止但从未交令牌的分支是「无法汇合」的直接原因
        stopped = []
        if terminal:
            for t in state["threads"].values():
                if (gid in t.get("token_stack", [])
                        and t["tid"] not in arrived_set and not _alive(t)):
                    reason = (f"已在活动 {t['activity']} 正常结束，"
                              f"未到达汇合点 {g.join}")
                    entry = {"tid": t["tid"], "activity": t["activity"],
                             "group": gid, "reason": reason, "waits_on": None}
                    blocking.insert(0, entry)
                    stopped.append(entry)
        pending_groups.append({
            "group": gid, "fork": g.fork, "join": g.join,
            "needed": j["needed"], "arrived": arrived,
            "missing": j["needed"] - len(arrived),
            "branches": stopped + branches, "_stopped": stopped,
        })
    if terminal:
        parts = []
        for x in pending_groups:
            stopped = x["_stopped"]
            tail = ("、".join(f"{b['tid']} 在 {b['activity']} 处提前结束"
                              for b in stopped)
                    if stopped else "存在分支永远无法交回令牌")
            parts.append(
                f"组 {x['group']}（{x['fork']}→{x['join']}）已有 "
                f"{len(x['arrived'])}/{x['needed']} 个分支到达，缺 "
                f"{x['missing']} 个令牌，其中 {tail}")
        detail = "汇合组无法闭合（与步数边界无关）：" + "；".join(parts)
        stopped_tids = {b["tid"] for x in pending_groups for b in x["_stopped"]}
        ended = [b for b in blocking if b.get("tid") == ended_tid]
        if ended:
            detail += "。直接原因：" + "；".join(b["reason"] for b in ended)
        rest = [b["reason"] for b in blocking
                if b.get("tid") != ended_tid and b["tid"] not in stopped_tids]
        if rest:
            detail += "。其余分支：" + "；".join(rest)
    else:
        detail = "步数边界内汇合组尚未闭合（边界截断，非问题判定）：" + "；".join(
            f"组 {x['group']}（{x['fork']}→{x['join']}）已有 {len(x['arrived'])}/"
            f"{x['needed']} 个分支到达，缺 {x['missing']} 个" for x in pending_groups)
        if blocking:
            detail += "。未到分支：" + "；".join(b["reason"] for b in blocking)
    for x in pending_groups:
        x.pop("_stopped", None)
    return {
        "kind": FIND_UNJOINABLE,
        "kind_label": FIND_LABELS[FIND_UNJOINABLE],
        "terminal": terminal,
        "groups": pending_groups,
        "resources": sorted(resources),
        "blocking": blocking,
        "detail": detail,
    }


def _materialize_witness(wf: W.Workflow, node: _Node, info: dict,
                         state0: dict, prefix: list[dict],
                         chosen: dict | None, root: _Node | None = None) -> dict:
    """把发现点节点物化为带快照的见证（可选共享前缀与改选首跳）。"""
    tail = _trace(node) if root is None else _tail(node, root)
    events = (list(prefix) + ([chosen] if chosen else [])) + tail
    prefix_len = len(prefix)
    steps = trace_with_states(wf, events, state0, prefix_len)
    out = {"length": len(events), "steps": steps}
    out.update(info)
    return out


def _bfs_search(wf: W.Workflow, root: _Node, bound: int, state0: dict,
                prefix: list[dict], chosen: dict | None,
                depth_offset: int = 0) -> tuple[dict[str, dict], dict, list[_Node]]:
    """从 root 有界 BFS。bound 为 root 之后可展开的边数。

    返回 (各类问题的见证 dict, 统计, 边界处的截断节点列表)。
    root 若携带 event（分叉根），物化见证时不重复计入该事件（它由 chosen 给出）。
    """
    from_root_carry = root.event is not None and chosen is not None
    seen = {root.fp}
    queue = deque([root])
    hits: dict[str, tuple[_Node, dict]] = {}
    expanded = 0
    max_depth = root.depth
    truncated = cap = False
    frontier: list[_Node] = []
    terminal_nodes: list[_Node] = []
    terminal_outcomes: dict[str, int] = {}

    while queue:
        node = queue.popleft()
        if node.depth - root.depth >= bound:
            truncated = True
            frontier.append(node)
            continue
        succ = successors(wf, node.state)
        if not succ:
            # 终态收集：按结局分类计数。「所有线程均不可继续、无结局且仍欠
            # 汇合令牌」的卡死状态是「无法汇合」问题（分支提前结束漏封存的
            # 兜底，不得当作无问题终态静默吞掉）；已有结局的终态（成功/补偿/
            # 死锁）按各自结局归类，不重复计为无法汇合。
            terminal_nodes.append(node)
            oc = node.state.get("outcome")
            key_outcome = oc or "stuck"
            terminal_outcomes[key_outcome] = terminal_outcomes.get(key_outcome, 0) + 1
            if oc is None and node.state.get("joins") and FIND_UNJOINABLE not in hits:
                ev = node.event or {"type": EV_BRANCH_ENDED, "thread": None,
                                    "activity": None, "sig": [EV_BRANCH_ENDED]}
                ev = dict(ev)
                ev["unjoinable"] = _unjoinable_info(wf, node.state, terminal=True)
                hits[FIND_UNJOINABLE] = (node, ev["unjoinable"])
            continue
        expanded += 1
        for nxt, ev in succ:
            fp = c_fingerprint(nxt)
            viols = check_invariants(wf, nxt)
            found = _finding_from_event(wf, node, ev, viols)
            child = _Node(nxt, fp, node, ev, node.depth + 1)
            if found is not None and found[0] not in hits:
                hits[found[0]] = (child, found[2])
            if fp in seen:
                continue
            seen.add(fp)
            max_depth = max(max_depth, child.depth)
            queue.append(child)
            if len(seen) > STATE_CAP:
                truncated = cap = True
                break
        if cap:
            break

    witnesses: dict[str, dict] = {}
    for kind, (node, info) in hits.items():
        if from_root_carry:
            witnesses[kind] = _materialize_witness(
                wf, node, info, state0, prefix, chosen, root=root)
        else:
            witnesses[kind] = _materialize_witness(
                wf, node, info, state0, prefix, chosen)
    stats = {"states_visited": len(seen), "transitions_expanded": expanded,
             "max_depth_reached": depth_offset + max_depth, "truncated": truncated,
             "state_cap_reached": cap, "state_cap": STATE_CAP,
             "terminal_states": len(terminal_nodes),
             "terminal_outcomes": terminal_outcomes}
    return witnesses, stats, frontier


def run_check(wf: W.Workflow, bound: int = DEFAULT_CONC_BOUND,
              init_vars: dict[str, Any] | None = None) -> dict:
    if not wf.concurrent:
        raise ConcError("当前模型未声明 fork/join 汇合组（concurrency.groups），"
                        "请先在「并发调度」标签中配置后再分析")
    bound = min(max(int(bound), 1), MAX_CONC_BOUND)
    state0 = c_initial_state(wf, init_vars)
    root = _Node(state0, c_fingerprint(state0), None, None, 0)

    root_viols = check_invariants(wf, state0)
    if root_viols:
        info = {"kind": FIND_INVARIANT, "kind_label": FIND_LABELS[FIND_INVARIANT],
                "violations": root_viols, "resources": [], "blocking": [],
                "detail": root_viols[0]["detail"]}
        wit = _materialize_witness(wf, root, info, state0, [], None)
        return _result(wf, state0, bound, {FIND_INVARIANT: wit},
                       {"states_visited": 1, "transitions_expanded": 0,
                        "max_depth_reached": 0, "truncated": False,
                        "state_cap_reached": False, "state_cap": STATE_CAP,
                        "terminal_states": 0, "terminal_outcomes": {}})

    witnesses, stats, frontier = _bfs_search(wf, root, bound, state0, [], None)

    # 步数边界处仍未闭合的汇合组只是「边界截断」信息，不是问题判定；
    # 分支提前结束导致的无法汇合已在转移上封存，由 BFS 作为问题命中收集。
    pending_at_bound = sorted({
        gid for n in frontier for gid in n.state["joins"]})
    stats["pending_groups_at_bound"] = pending_at_bound

    return _result(wf, state0, bound, witnesses, stats)


def _result(wf: W.Workflow, state0: dict, bound: int,
            witnesses: dict[str, dict], stats: dict) -> dict:
    primary_kind = next((k for k in FIND_KINDS if k in witnesses), None)
    if primary_kind is not None:
        verdict = "conc_unsafe"
        labels = "、".join(witnesses[k]["kind_label"]
                          for k in FIND_KINDS if k in witnesses)
        text = (f"并发调度安全分析发现问题：{labels}。已按最短调度、稳定事件序给出见证轨迹"
                "（分别给出涉及资源与阻塞原因）。")
        primary = witnesses[primary_kind]
        counterexample = {"length": primary["length"],
                          "violations": primary.get("violations", []),
                          "steps": primary["steps"]}
    else:
        # 一条按签名最小的代表轨迹（尽快到达终态），供回放与分叉
        sample = _sample_path(wf, state0, bound)
        steps = trace_with_states(wf, sample, state0)
        counterexample = {"length": len(sample), "violations": [], "steps": steps}
        if stats["truncated"]:
            verdict = "bound_reached"
            text = (f"在 {bound} 步边界内未发现数据竞态、锁顺序死锁或无法汇合的分支，"
                    f"但搜索在边界处被截断（访问 {stats['states_visited']} 个指纹状态，"
                    "按线程与汇合令牌合并）。仅对边界内负责，可增大步数上限继续分析。")
            pending = stats.get("pending_groups_at_bound") or []
            if pending:
                text += ("边界处仍未闭合的汇合组：" + "、".join(pending)
                         + "（属于步数不足导致的暂未闭合，只作截断提示，不判定为问题）。")
        else:
            verdict = "exhausted"
            text = (f"已穷尽 {bound} 步边界内全部 {stats['states_visited']} 个并发状态"
                    "（按线程、锁、汇合令牌与向量时钟指纹合并），未发现数据竞态、"
                    "锁顺序死锁或无法汇合的分支。结论仅在该步数边界与给定交错模型下成立。")
    return {
        "verdict": verdict,
        "verdict_text": text,
        "bound": bound,
        "stats": stats,
        "primary_kind": primary_kind,
        "findings": witnesses,
        "counterexample": counterexample,
        "config": {
            "resources": list(wf.resources),
            "locks": list(wf.locks),
            "groups": [{"id": g.id, "fork": g.fork, "join": g.join}
                       for g in sorted(wf.conc_groups.values(), key=lambda g: g.id)],
        },
        "initial_snapshot": snapshot(wf, state0),
        "initial_alternatives": alternatives(wf, state0),
    }


def _sample_path(wf: W.Workflow, state: dict, limit: int) -> list[dict]:
    """无问题时取一条按签名最小、尽快到达终态的代表轨迹。"""
    out: list[dict] = []
    seen = {c_fingerprint(state)}
    for _ in range(limit):
        picked = None
        for nxt, ev in successors(wf, state):
            fp = c_fingerprint(nxt)
            if fp not in seen:
                picked = (nxt, ev)
                break
        if picked is None:
            break
        nxt, ev = picked
        seen.add(c_fingerprint(nxt))
        out.append(ev)
        state = nxt
    return out


# ---------------------------------------------------------------------------
# 从见证节点分叉
# ---------------------------------------------------------------------------

def run_fork(wf: W.Workflow, prefix: list[dict], chosen_sig: list,
             bound: int = DEFAULT_CONC_BOUND,
             init_vars: dict[str, Any] | None = None) -> dict:
    bound = min(max(int(bound), 1), MAX_CONC_BOUND)
    state0 = c_initial_state(wf, init_vars)
    state = state0
    walked: list[dict] = []
    for ev in prefix:
        state = apply_event(wf, state, ev)
        walked.append(ev)

    chosen_event = chosen_state = None
    sig = tuple(chosen_sig)
    for nxt, ev in successors(wf, state):
        if tuple(ev["sig"]) == sig:
            chosen_event, chosen_state = ev, nxt
            break
    if chosen_event is None:
        raise ConcError("所选的分叉事件在当前并发状态下不可触发")

    fork_parent = _Node(state, c_fingerprint(state), None, None, 0)
    fork_node = _Node(chosen_state, c_fingerprint(chosen_state),
                      fork_parent, chosen_event, 1)

    # 改选首跳本身就命中问题（如锁等待成环）：单独捕获
    first_found = _finding_from_event(
        wf, fork_parent, chosen_event, check_invariants(wf, chosen_state))

    bfs_bound = max(bound - len(prefix), 1)
    witnesses, stats, frontier = _bfs_search(
        wf, fork_node, bfs_bound, state0, walked, chosen_event,
        depth_offset=len(walked))
    if first_found is not None and first_found[0] not in witnesses:
        info = first_found[2]
        witnesses[first_found[0]] = _materialize_witness(
            wf, fork_node, info, state0, walked, chosen_event, root=fork_node)

    # 边界处未闭合的汇合组仅作截断信息；提前结束的无法汇合由转移封存命中
    stats["pending_groups_at_bound"] = sorted({
        gid for n in frontier for gid in n.state["joins"]})

    primary_kind = next((k for k in FIND_KINDS if k in witnesses), None)
    if primary_kind is not None:
        primary = witnesses[primary_kind]
        return {
            "verdict": "conc_unsafe",
            "verdict_text": f"分叉后发现问题：{primary['kind_label']}。",
            "bound": bound,
            "fork_point": len(walked),
            "chosen_event": dict(chosen_event),
            "stats": stats,
            "primary_kind": primary_kind,
            "findings": witnesses,
            "counterexample": {"length": primary["length"],
                               "violations": primary.get("violations", []),
                               "steps": primary["steps"]},
        }

    # 未发现问题：改选首跳 + 一条按签名最小的代表路径
    remaining = max(bfs_bound - 1, 0)
    suffix = _sample_path(wf, chosen_state, remaining)
    all_events = walked + [chosen_event] + suffix
    steps = trace_with_states(wf, all_events, state0, prefix_len=len(walked))
    text = (f"分叉后在全局 {bound} 步边界内（改选后又探索 {remaining} 步）"
            "未发现数据竞态、锁顺序死锁或无法汇合的分支。"
            + ("搜索在边界处被截断，仅对边界内负责。" if stats["truncated"] else ""))
    return {
        "verdict": "bound_reached" if stats["truncated"] else "exhausted",
        "verdict_text": text,
        "bound": bound,
        "fork_point": len(walked),
        "chosen_event": dict(chosen_event),
        "stats": stats,
        "primary_kind": None,
        "findings": {},
        "counterexample": {"length": len(all_events), "violations": [], "steps": steps},
    }


def replay(wf: W.Workflow, events: list[dict],
           init_vars: dict[str, Any] | None = None) -> dict:
    state0 = c_initial_state(wf, init_vars)
    steps = trace_with_states(wf, events, state0)
    return {"verdict": "conc_replay",
            "counterexample": {"length": len(events),
                               "violations": steps[-1]["violations"],
                               "steps": steps}}
