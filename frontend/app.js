/* 补偿工作流模型检验台 —— 原生 JavaScript，无构建步骤 */
"use strict";

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------

const EMPTY_MODEL = {
  name: "未命名模型",
  spec: {
    start: "a",
    variables: [],
    activities: [
      { id: "a", name: "活动A", pre: "", assign: [], effect: "", idempotent: false,
        can_fail: true, retryable: true, max_attempts: 3,
        pre_crash: false, post_crash: false,
        comp_can_fail: true, comp_retryable: true, comp_max_attempts: 3,
        fork_group: "", join_group: "", locks: [], reads: [], writes: [] },
    ],
    edges: [],
    invariants: { at_most_once: true, undo_on_failure: true, custom: [] },
    concurrency: { resources: [], locks: [], groups: [] },
  },
};

let model = structuredClone(EMPTY_MODEL);
let currentModelId = null;
let checkResult = null;      // 最近一次检验结果
let replayIndex = 0;         // 当前回放步骤
let autoTimer = null;
let forkCompare = null;      // {result, steps, index} 分叉对比轨迹
let viewMode = "original";   // original | fork
let appMode = "check";       // check | conc | risk | diff（右栏模式）

// 并发调度安全分析
let concResult = null;       // 最近一次并发分析结果
let concReplaySteps = null;  // 当前并发见证回放步骤
let concActiveKind = null;   // 当前查看的问题类别

// 版本行为对比
let diffResult = null;       // 最近一次对比结果
let diffWitness = null;      // 当前查看的见证（四类状态差异之一）或分叉见证
let diffWitnessKind = null;  // terminal | variables | effects | invariant | divergence
let diffIndex = 0;           // 双列回放的对齐步
let diffRunRecord = null;    // 当前打开的持久化对比记录（含两侧快照）
let diffForkResult = null;   // 差异节点分叉复检结果

// 随机故障风险分析的概率配置（按活动 id 索引，独立于模型 spec 保存于分析记录）
let riskConfig = {};         // { [aid]: {success,retryable,fatal,pre_crash,post_crash,p_ack,p_dup,comp_success,comp_retryable,comp_fatal} }
let riskResult = null;       // 最近一次风险分析结果
let riskSort = { key: "probability", desc: true };
let replayContext = null;    // {mode:"check"|"risk", traceMeta} 当前回放来源

const RISK_DEFAULTS = () => ({
  success: 1, retryable: 0, fatal: 0, pre_crash: 0, post_crash: 0,
  p_ack: 0.5, p_dup: 0.5,
  comp_success: 1, comp_retryable: 0, comp_fatal: 0,
});

function riskCfg(aid) {
  if (!riskConfig[aid]) riskConfig[aid] = RISK_DEFAULTS();
  return riskConfig[aid];
}

// 同步概率配置：模型中的活动集合变化时补默认项
function syncRiskConfig() {
  const ids = new Set(model.spec.activities.map(a => a.id));
  for (const id of Object.keys(riskConfig)) if (!ids.has(id)) delete riskConfig[id];
  for (const a of model.spec.activities) riskCfg(a.id);
}

// 为从 JSON 粘贴的精简模型补全字段
function withDefaults(spec) {
  const s = spec || {};
  s.variables ??= [];
  s.edges ??= [];
  s.activities = (s.activities || []).map(a => ({
    id: a.id, name: a.name ?? a.id ?? "", pre: a.pre ?? "", assign: a.assign ?? [],
    effect: a.effect ?? "", idempotent: !!a.idempotent,
    can_fail: a.can_fail ?? true, retryable: a.retryable ?? true,
    max_attempts: a.max_attempts ?? 3,
    pre_crash: !!a.pre_crash, post_crash: !!a.post_crash,
    comp_can_fail: a.comp_can_fail ?? true,
    comp_retryable: a.comp_retryable ?? true,
    comp_max_attempts: a.comp_max_attempts ?? 3,
    fork_group: a.fork_group ?? "", join_group: a.join_group ?? "",
    locks: Array.isArray(a.locks) ? a.locks : (a.locks ? String(a.locks).split(",").map(x => x.trim()).filter(Boolean) : []),
    reads: Array.isArray(a.reads) ? a.reads : (a.reads ? String(a.reads).split(",").map(x => x.trim()).filter(Boolean) : []),
    writes: Array.isArray(a.writes) ? a.writes : (a.writes ? String(a.writes).split(",").map(x => x.trim()).filter(Boolean) : []),
  }));
  s.invariants = {
    at_most_once: s.invariants?.at_most_once ?? true,
    undo_on_failure: s.invariants?.undo_on_failure ?? true,
    custom: s.invariants?.custom ?? [],
  };
  const normNames = (arr) => (arr || []).map(x =>
    typeof x === "string" ? x : (x?.name ?? "")).filter(Boolean).map(name => ({ name }));
  s.concurrency = {
    resources: normNames(s.concurrency?.resources),
    locks: normNames(s.concurrency?.locks),
    groups: (s.concurrency?.groups || []).map(g => ({
      id: g.id ?? "", fork: g.fork ?? "", join: g.join ?? "",
    })),
  };
  s.start ??= s.activities[0]?.id ?? "";
  return s;
}

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    if (detail && Array.isArray(detail.errors)) throw new Error(detail.errors.join("\n"));
    throw new Error(typeof detail === "string" ? detail : "请求失败：" + res.status);
  }
  return data;
}

// ---------------------------------------------------------------------------
// 模型编辑：渲染
// ---------------------------------------------------------------------------

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "checked") node.checked = v;
    else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function input(value, cls, oninput, extra = {}) {
  const i = el("input", { class: cls, value: value ?? "", oninput: (e) => oninput(e.target.value), ...extra });
  return i;
}

function checkbox(checked, onchange, title = "") {
  return el("input", { type: "checkbox", checked, onchange: (e) => onchange(e.target.checked), title });
}

function renderActivities() {
  const list = $("#activityList");
  list.innerHTML = "";
  model.spec.activities.forEach((a, idx) => {
    const set = (patch) => { Object.assign(a, patch); renderAll(); };
    const card = el("div", { class: "entity-card" }, [
      el("div", { class: "row" }, [
        input(a.id, "w-id", (v) => { a.id = v; syncSelectors(); drawGraph(); }, { placeholder: "id" }),
        input(a.name, "w-name", (v) => { a.name = v; }, { placeholder: "名称" }),
        el("button", {
          class: "btn btn-small btn-danger del-btn",
          onclick: () => deleteActivity(idx),
        }, "删除"),
      ]),
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, "前置:"),
        input(a.pre, "w-full", (v) => { a.pre = v; }, { placeholder: '如 effects("x") > 0' }),
      ]),
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, "外部效果键:"),
        input(a.effect, "w-id", (v) => { a.effect = v; drawGraph(); }, { placeholder: "无" }),
        el("label", { class: "check" }, [
          checkbox(a.idempotent, (v) => set({ idempotent: v })),
          "幂等",
        ]),
      ]),
      el("div", { class: "row flags" }, [
        el("label", { class: "check" }, [
          checkbox(a.can_fail, (v) => set({ can_fail: v })), "可能失败"]),
        el("label", { class: "check" }, [
          checkbox(a.retryable, (v) => set({ retryable: v })), "可重试错误"]),
        el("label", { class: "check", title: "确认前崩溃：无持久痕迹，恢复后重新调用" }, [
          checkbox(a.pre_crash, (v) => set({ pre_crash: v })), "确认前崩溃"]),
        el("label", { class: "check", title: "确认后崩溃/应答丢失：可能重复生效" }, [
          checkbox(a.post_crash, (v) => set({ post_crash: v })), "应答丢失"]),
      ]),
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, "最大尝试"),
        input(a.max_attempts, "num-small", (v) => { a.max_attempts = Math.max(1, +v || 1); },
              { type: "number", min: 1 }),
      ]),
      el("div", { class: "row flags" }, [
        el("label", { class: "check" }, [
          checkbox(a.comp_can_fail, (v) => set({ comp_can_fail: v })), "补偿可能失败"]),
        el("label", { class: "check" }, [
          checkbox(a.comp_retryable, (v) => set({ comp_retryable: v })), "补偿可重试"]),
        el("span", { class: "hint" }, "补偿最大尝试"),
        input(a.comp_max_attempts, "num-small", (v) => { a.comp_max_attempts = Math.max(1, +v || 1); },
              { type: "number", min: 1 }),
      ]),
      a.assign.length ? el("div", { class: "section-title", style: "margin:6px 0 4px" },
        "成功赋值") : null,
      ...a.assign.map((asn, ai) => el("div", { class: "row" }, [
        el("select", {
          onchange: (e) => { asn.var = e.target.value; },
        }, model.spec.variables.map(v => el("option", { value: v.name, ...(v.name === asn.var ? { selected: true } : {}) }, v.name))),
        input(asn.expr, "w-full", (v) => { asn.expr = v; }, { placeholder: "赋值表达式" }),
        el("button", { class: "btn btn-small btn-danger", onclick: () => { a.assign.splice(ai, 1); renderAll(); } }, "✕"),
      ])),
      el("button", {
        class: "btn btn-small",
        onclick: () => {
          const firstName = model.spec.variables[0]?.name;
          a.assign.push({ var: firstName || "", expr: "0" });
          renderAll();
        },
      }, "+ 成功后赋值"),
    ]);
    list.appendChild(card);
  });
}

function renderEdges() {
  const list = $("#edgeList");
  list.innerHTML = "";
  model.spec.edges.forEach((e, idx) => {
    const actOptions = (sel) => model.spec.activities.map(a =>
      el("option", { value: a.id, ...(a.id === sel ? { selected: true } : {}) }, a.id));
    const card = el("div", { class: "entity-card" }, [
      el("div", { class: "row" }, [
        el("select", { onchange: (ev) => { e.src = ev.target.value; drawGraph(); } }, actOptions(e.src)),
        "→",
        el("select", { onchange: (ev) => { e.dst = ev.target.value; drawGraph(); } }, actOptions(e.dst)),
        el("button", {
          class: "btn btn-small btn-danger del-btn",
          onclick: () => { model.spec.edges.splice(idx, 1); renderAll(); },
        }, "删除"),
      ]),
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, "守卫:"),
        input(e.guard, "w-full", (v) => { e.guard = v; }, { placeholder: "可空，如 vip" }),
      ]),
    ]);
    list.appendChild(card);
  });
}

function renderVars() {
  const list = $("#varList");
  list.innerHTML = "";
  model.spec.variables.forEach((v, idx) => {
    const card = el("div", { class: "entity-card" }, [
      el("div", { class: "row" }, [
        input(v.name, "w-name", (val) => { v.name = val; }, { placeholder: "变量名" }),
        el("select", { onchange: (e) => { v.type = e.target.value; renderAll(); } }, [
          el("option", { value: "int", ...(v.type === "int" ? { selected: true } : {}) }, "整数 int"),
          el("option", { value: "bool", ...(v.type === "bool" ? { selected: true } : {}) }, "布尔 bool"),
        ]),
        el("span", { class: "hint" }, "初值"),
        v.type === "bool"
          ? el("input", { type: "checkbox", checked: !!v.initial, onchange: (e) => { v.initial = e.target.checked; } })
          : input(v.initial, "num-small", (val) => { v.initial = +val || 0; }, { type: "number" }),
        el("button", {
          class: "btn btn-small btn-danger del-btn",
          onclick: () => deleteVariable(idx),
        }, "删除"),
      ]),
    ]);
    list.appendChild(card);
  });
}

function renderInvariants() {
  const inv = model.spec.invariants;
  $("#invOnce").checked = inv.at_most_once !== false;
  $("#invUndo").checked = inv.undo_on_failure !== false;
  const list = $("#invList");
  list.innerHTML = "";
  inv.custom.forEach((c, idx) => {
    list.appendChild(el("div", { class: "entity-card" }, [
      el("div", { class: "row" }, [
        input(c.name, "w-name", (v) => { c.name = v; }, { placeholder: "不变量名称" }),
        el("button", {
          class: "btn btn-small btn-danger del-btn",
          onclick: () => { inv.custom.splice(idx, 1); renderInvariants(); },
        }, "删除"),
      ]),
      el("div", { class: "row" }, [
        input(c.expr, "w-full", (v) => { c.expr = v; }, {
          placeholder: '如 effects("charge") <= 1',
        }),
      ]),
    ]));
  });
}

// ---------------------------------------------------------------------------
// 并发调度：资源 / 锁 / 汇合组 / 活动访问注解
// ---------------------------------------------------------------------------

function concConfig() {
  model.spec.concurrency ??= { resources: [], locks: [], groups: [] };
  const c = model.spec.concurrency;
  c.resources ??= []; c.locks ??= []; c.groups ??= [];
  return c;
}

function renderConcurrency() {
  const tab = $("#tab-concurrency");
  if (!tab) return;
  const conc = concConfig();
  const acts = model.spec.activities;
  const actOptions = (sel) => acts.map(a =>
    el("option", { value: a.id, ...(a.id === sel ? { selected: true } : {}) }, a.id));

  // 汇合组
  const gl = $("#concGroupList");
  gl.innerHTML = "";
  conc.groups.forEach((g, idx) => {
    gl.appendChild(el("div", { class: "conc-item" }, [
      input(g.id, "ci-name", v => { g.id = v; }, { placeholder: "组 id，如 pay" }),
      el("span", { class: "hint" }, "fork"),
      el("select", { onchange: e => { g.fork = e.target.value; renderAll(); } }, actOptions(g.fork)),
      el("span", { class: "ci-arrow" }, "→ join"),
      el("select", { onchange: e => { g.join = e.target.value; renderAll(); } }, actOptions(g.join)),
      el("button", { class: "btn btn-small btn-danger", onclick: () => {
        conc.groups.splice(idx, 1);
        // 清除活动上残留的 fork/join 标记
        acts.forEach(a => { if (a.fork_group === g.id) a.fork_group = "";
          if (a.join_group === g.id) a.join_group = ""; });
        renderAll();
      } }, "删除"),
    ]));
  });

  // 资源
  const rl = $("#concResourceList");
  rl.innerHTML = "";
  conc.resources.forEach((r, idx) => {
    rl.appendChild(el("div", { class: "conc-item" }, [
      input(r.name, "ci-name", v => { r.name = v; }, { placeholder: "资源名，如 accountBalance" }),
      el("button", { class: "btn btn-small btn-danger", onclick: () => {
        conc.resources.splice(idx, 1);
        acts.forEach(a => {
          a.reads = (a.reads || []).filter(x => x !== r.name);
          a.writes = (a.writes || []).filter(x => x !== r.name);
        });
        renderAll();
      } }, "删除"),
    ]));
  });

  // 锁
  const ll = $("#concLockList");
  ll.innerHTML = "";
  conc.locks.forEach((lk, idx) => {
    ll.appendChild(el("div", { class: "conc-item" }, [
      input(lk.name, "ci-name", v => {
        const old = lk.name; lk.name = v;
        acts.forEach(a => { a.locks = (a.locks || []).map(x => x === old ? v : x); });
        renderAll();
      }, { placeholder: "锁名，如 accountLock" }),
      el("button", { class: "btn btn-small btn-danger", onclick: () => {
        conc.locks.splice(idx, 1);
        acts.forEach(a => { a.locks = (a.locks || []).filter(x => x !== lk.name); });
        renderAll();
      } }, "删除"),
    ]));
  });

  // 活动注解（只展示有 fork/join 角色或已标注访问的活动，外加全部可选）
  const al = $("#concActivityList");
  al.innerHTML = "";
  const resNames = conc.resources.map(r => r.name);
  const lockNames = conc.locks.map(l => l.name);
  const groupIds = conc.groups.map(g => g.id);
  acts.forEach(a => {
    a.locks ??= []; a.reads ??= []; a.writes ??= [];
    a.fork_group ??= ""; a.join_group ??= "";
    const tagged = groupIds.map(gid =>
      el("label", { class: "check", title: "该活动是此汇合组的 fork（派生分支）" }, [
        el("input", { type: "radio", name: `forkrole-${a.id}`,
          ...(a.fork_group === gid ? { checked: true } : {}),
          onchange: e => {
            if (e.target.checked) {
              // fork 角色互斥；清除其他活动上同一组的 fork
              acts.forEach(o => { if (o !== a && o.fork_group === gid) o.fork_group = ""; });
              a.fork_group = gid; renderAll();
            }
          } }),
        `fork:${gid}`,
      ]));
    const joinTagged = groupIds.map(gid =>
      el("label", { class: "check", title: "该活动是此汇合组的 join（汇合点）" }, [
        el("input", { type: "radio", name: `joinrole-${a.id}`,
          ...(a.join_group === gid ? { checked: true } : {}),
          onchange: e => {
            if (e.target.checked) {
              acts.forEach(o => { if (o !== a && o.join_group === gid) o.join_group = ""; });
              a.join_group = gid; renderAll();
            }
          } }),
        `join:${gid}`,
      ]));
    al.appendChild(el("div", { class: "entity-card" }, [
      el("div", { class: "row" }, [
        el("b", {}, `${a.name}（${a.id}）`),
        ...tagged, ...joinTagged,
      ]),
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, "读资源（逗号分隔）"),
        csvInput(a.reads, resNames, vals => { a.reads = vals; }),
      ]),
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, "写资源（逗号分隔）"),
        csvInput(a.writes, resNames, vals => { a.writes = vals; }),
      ]),
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, "加锁顺序（逗号分隔）"),
        csvInput(a.locks, lockNames, vals => { a.locks = vals; }),
      ]),
    ]));
  });
}

function csvInput(values, allowed, onChange) {
  return input((values || []).join(", "), "tag-input", v => {
    const vals = v.split(",").map(x => x.trim()).filter(Boolean);
    onChange(vals);
  }, { list: undefined, placeholder: allowed.length ? "可选：" + allowed.join(", ") : "先在上方声明",
      title: allowed.length ? "合法名称：" + allowed.join(", ") : "" });
}

// ---------------------------------------------------------------------------
// 随机风险：概率配置面板
// ---------------------------------------------------------------------------

const RISK_CALL_FIELDS = [
  { key: "success", label: "成功", enabled: a => true },
  { key: "retryable", label: "可重试错误", enabled: a => a.can_fail && a.retryable },
  { key: "fatal", label: "不可重试错误", enabled: a => a.can_fail },
  { key: "pre_crash", label: "确认前崩溃", enabled: a => a.pre_crash },
  { key: "post_crash", label: "确认后崩溃/应答丢失", enabled: a => a.post_crash },
];
const RISK_COMP_FIELDS = [
  { key: "comp_success", label: "补偿成功", enabled: () => true },
  { key: "comp_retryable", label: "补偿可重试错误", enabled: a => a.comp_can_fail && a.comp_retryable },
  { key: "comp_fatal", label: "补偿不可重试失败", enabled: a => a.comp_can_fail },
];

function probInput(cfg, key, enabled, onChange, label = null) {
  const wrap = el("label", { class: "prob-field" + (enabled ? "" : " disabled") });
  wrap.appendChild(el("span", { class: "prob-label", title: enabled ? "" : "该结果未在模型开关中启用，概率必须为 0" },
    enabled ? "" : "🔒 "));
  wrap.appendChild(el("span", { class: "prob-name" },
    label || { success: "成功", retryable: "可重试", fatal: "致命",
       pre_crash: "确认前崩溃", post_crash: "应答丢失",
       comp_success: "补偿成功", comp_retryable: "补偿可重试", comp_fatal: "补偿致命",
       p_ack: "p_ack", p_dup: "p_dup" }[key]));
  const inp = el("input", {
    class: "prob-input num-small", type: "number", min: 0, max: 1, step: 0.05,
    value: cfg[key], disabled: enabled ? false : true,
    onchange: () => {
      let v = parseFloat(inp.value);
      if (Number.isNaN(v)) v = 0;
      cfg[key] = Math.min(1, Math.max(0, v));
      if (!enabled) cfg[key] = 0;
      renderRiskConfig();
      onChange?.();
    },
  });
  wrap.appendChild(inp);
  return wrap;
}

function sumBadge(value, ok) {
  return el("span", { class: "prob-sum " + (ok ? "ok" : "bad"), title: "该分支全部启用结果的概率之和，必须等于 1" },
    `Σ=${(Math.round(value * 1e6) / 1e6)}`);
}

function renderRiskConfig() {
  const list = $("#riskActivityList");
  if (!list) return;
  list.innerHTML = "";
  for (const a of model.spec.activities) {
    const cfg = riskCfg(a.id);
    const callInputs = RISK_CALL_FIELDS.map(f =>
      probInput(cfg, f.key, f.enabled(a)));
    const compInputs = RISK_COMP_FIELDS.map(f =>
      probInput(cfg, f.key, f.enabled(a)));

    const callSum = RISK_CALL_FIELDS.reduce((s, f) => s + (f.enabled(a) ? cfg[f.key] : 0), 0);
    const compSum = RISK_COMP_FIELDS.reduce((s, f) => s + (f.enabled(a) ? cfg[f.key] : 0), 0);

    const card = el("div", { class: "entity-card risk-card" }, [
      el("div", { class: "row risk-head" }, [
        el("b", {}, `${a.name}（${a.id}）`),
        el("span", { class: "hint" }, a.effect ? "效果键：" + a.effect : "无外部效果"),
      ]),
      el("div", { class: "prob-row" }, [
        el("span", { class: "prob-branch-name" }, "调用分支"),
        el("div", { class: "prob-fields" }, callInputs),
        sumBadge(callSum, Math.abs(callSum - 1) < 1e-6),
      ]),
    ]);

    if (a.post_crash) {
      card.appendChild(el("div", { class: "prob-row conditional" }, [
        el("span", { class: "prob-branch-name" }, "应答丢失条件"),
        el("div", { class: "prob-fields" }, [
          probInput(cfg, "p_ack", true),
          probInput(cfg, "p_dup", true),
        ]),
        el("span", { class: "hint" }, "p_ack：恢复确认已生效；p_dup：重调成功时首次已生效"),
      ]));
    }

    card.appendChild(el("div", { class: "prob-row" }, [
      el("span", { class: "prob-branch-name" }, "补偿分支"),
      el("div", { class: "prob-fields" }, compInputs),
      sumBadge(compSum, Math.abs(compSum - 1) < 1e-6),
    ]));
    list.appendChild(card);
  }
}

function setRiskPreset(kind) {
  for (const a of model.spec.activities) {
    const cfg = riskCfg(a.id);
    const call = RISK_CALL_FIELDS.filter(f => f.enabled(a)).map(f => f.key);
    const comp = RISK_COMP_FIELDS.filter(f => f.enabled(a)).map(f => f.key);
    for (const k of Object.keys(cfg)) {
      if (k === "p_ack" || k === "p_dup") { cfg[k] = 0.5; continue; }
      cfg[k] = 0;
    }
    if (kind === "uniform") {
      call.forEach(k => { cfg[k] = 1 / call.length; });
      comp.forEach(k => { cfg[k] = 1 / comp.length; });
      // 四舍五入误差留给最后一个字段吸收
      const fix = (keys) => {
        const s = keys.reduce((x, k) => x + cfg[k], 0);
        cfg[keys[0]] += 1 - s;
      };
      fix(call); fix(comp);
    } else {
      cfg.success = 1;
      cfg.comp_success = 1;
    }
  }
  renderRiskConfig();
}

function riskConfigPayload() {
  return model.spec.activities.map(a => ({ id: a.id, ...riskCfg(a.id) }));
}

function renderStartSelect() {
  const sel = $("#startSelect");
  sel.innerHTML = "";
  model.spec.activities.forEach(a => {
    const opt = el("option", { value: a.id, ...(a.id === model.spec.start ? { selected: true } : {}) },
      `${a.id}（${a.name || a.id}）`);
    sel.appendChild(opt);
  });
}

function renderModelSelect(models) {
  const sel = $("#modelSelect");
  sel.innerHTML = "";
  sel.appendChild(el("option", { value: "" }, "— 已保存模型 —"));
  models.forEach(m => sel.appendChild(el("option", {
    value: m.id, ...(m.id === currentModelId ? { selected: true } : {}),
  }, m.name)));
}

function syncSelectors() {
  // id 改名后修复悬空边和 start
  const ids = new Set(model.spec.activities.map(a => a.id));
  model.spec.edges = model.spec.edges.filter(e => ids.has(e.src) && ids.has(e.dst));
  if (!ids.has(model.spec.start)) model.spec.start = model.spec.activities[0]?.id || "";
  renderAll();
}

function deleteActivity(idx) {
  const a = model.spec.activities[idx];
  model.spec.activities.splice(idx, 1);
  model.spec.edges = model.spec.edges.filter(e => e.src !== a.id && e.dst !== a.id);
  if (model.spec.start === a.id) model.spec.start = model.spec.activities[0]?.id || "";
  renderAll();
}

function deleteVariable(idx) {
  const v = model.spec.variables[idx];
  model.spec.variables.splice(idx, 1);
  model.spec.activities.forEach(a => { a.assign = a.assign.filter(x => x.var !== v.name); });
  renderAll();
}

function renderAll() {
  syncRiskConfig();
  renderActivities();
  renderEdges();
  renderVars();
  renderInvariants();
  renderConcurrency();
  renderRiskConfig();
  renderStartSelect();
  drawGraph();
  refreshJsonEditor();
}

function refreshJsonEditor() {
  if (!$("#tab-json").classList.contains("hidden")) {
    $("#jsonEditor").value = JSON.stringify(model.spec, null, 2);
  }
}

// ---------------------------------------------------------------------------
// 简单的有向图布局（拓扑分层 + canvas 绘制）
// ---------------------------------------------------------------------------

function layoutGraph() {
  const acts = model.spec.activities;
  const indeg = Object.fromEntries(acts.map(a => [a.id, 0]));
  const adj = Object.fromEntries(acts.map(a => [a.id, []]));
  model.spec.edges.forEach(e => {
    if (indeg[e.dst] !== undefined && adj[e.src]) {
      adj[e.src].push(e.dst);
      indeg[e.dst]++;
    }
  });
  const layer = Object.fromEntries(acts.map(a => [a.id, null]));
  const queue = acts.filter(a => indeg[a.id] === 0).map(a => a.id);
  queue.forEach(id => (layer[id] = 0));
  // Kahn 分层；有环时剩余节点给递增层
  let guard = 0;
  while (queue.length && guard++ < 10000) {
    const id = queue.shift();
    for (const d of adj[id]) {
      indeg[d]--;
      layer[d] = Math.max(layer[d] ?? 0, (layer[id] ?? 0) + 1);
      if (indeg[d] === 0) queue.push(d);
    }
  }
  let fallback = 0;
  acts.forEach(a => {
    if (layer[a.id] === null) { layer[a.id] = fallback++ % acts.length; }
  });
  const byLayer = {};
  acts.forEach(a => (byLayer[layer[a.id]] ??= []).push(a.id));
  const positions = {};
  const W = 190, H = 70;
  Object.keys(byLayer).sort((x, y) => +x - +y).forEach((lyr, li) => {
    byLayer[lyr].forEach((id, ii) => {
      positions[id] = { x: 40 + li * 220, y: 30 + ii * 110, w: W, h: H };
    });
  });
  const maxL = Math.max(0, ...Object.values(layer));
  const maxRows = Math.max(1, ...Object.values(byLayer).map(g => g.length));
  return { positions, width: Math.max(900, 110 + (maxL + 1) * 220), height: Math.max(320, 70 + maxRows * 110) };
}

function drawGraph() {
  const canvas = $("#graphCanvas");
  const ctx = canvas.getContext("2d");
  const { positions, width, height } = layoutGraph();
  canvas.width = width;
  canvas.height = height;
  ctx.clearRect(0, 0, width, height);
  ctx.font = "12px sans-serif";

  // 边
  model.spec.edges.forEach(e => {
    const p = positions[e.src], q = positions[e.dst];
    if (!p || !q) return;
    const x1 = p.x + p.w, y1 = p.y + p.h / 2;
    const x2 = q.x, y2 = q.y + q.h / 2;
    ctx.strokeStyle = e.guard ? "#e8a83d" : "#4f8cff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    if (q.x > p.x) ctx.lineTo(x2 - 8, y2);
    else {
      ctx.bezierCurveTo(x1 + 40, y1, x2 - 40, y2, x2 - 8, y2);
    }
    ctx.stroke();
    // 箭头
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(x2, y2);
    ctx.lineTo(x2 - 9, y2 - 5);
    ctx.lineTo(x2 - 9, y2 + 5);
    ctx.fill();
    if (e.guard) {
      ctx.fillStyle = "#e8a83d";
      ctx.fillText("[" + e.guard + "]", (x1 + x2) / 2 - 14, (y1 + y2) / 2 - 5);
    }
  });

  // 节点
  model.spec.activities.forEach(a => {
    const p = positions[a.id];
    if (!p) return;
    const isStart = a.id === model.spec.start;
    ctx.fillStyle = a.post_crash || a.pre_crash ? "#2a2340" : "#1d2638";
    ctx.strokeStyle = isStart ? "#2fbf71" : "#2a3550";
    ctx.lineWidth = isStart ? 2 : 1;
    roundRect(ctx, p.x, p.y, p.w, p.h, 9);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "#e6ebf5";
    ctx.font = "bold 13px sans-serif";
    ctx.fillText(a.id + (isStart ? " ●起" : ""), p.x + 10, p.y + 22);
    ctx.font = "11px sans-serif";
    ctx.fillStyle = "#8b97b0";
    const meta = [];
    if (a.effect) meta.push("⚡" + a.effect);
    if (a.idempotent) meta.push("幂等");
    if (a.post_crash) meta.push("应答丢失");
    if (a.pre_crash) meta.push("崩溃");
    const groups = model.spec.concurrency?.groups || [];
    const gmap = Object.fromEntries(groups.map(g => [g.fork, "⑂fork:" + g.id]));
    const jmap = Object.fromEntries(groups.map(g => [g.join, "🔀join:" + g.id]));
    if (gmap[a.id]) meta.push(gmap[a.id]);
    if (jmap[a.id]) meta.push(jmap[a.id]);
    if ((a.writes || []).length) meta.push("写:" + a.writes.join(","));
    else if ((a.reads || []).length) meta.push("读:" + a.reads.join(","));
    if ((a.locks || []).length) meta.push("🔒" + a.locks.join(">"));
    ctx.fillText(meta.join(" · ") || a.name || "—", p.x + 10, p.y + 40);
    if (a.pre) {
      ctx.fillStyle = "#9cc0ff";
      ctx.fillText("pre: " + (a.pre.length > 22 ? a.pre.slice(0, 21) + "…" : a.pre), p.x + 10, p.y + 57);
    }
  });
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

// ---------------------------------------------------------------------------
// 检验 & 回放
// ---------------------------------------------------------------------------

async function doValidate(silent = false) {
  try {
    const r = await api("/api/validate", { method: "POST", body: { spec: model.spec } });
    $("#validateBox").innerHTML = "";
    const box = el("div", { class: "msg ok" },
      `✓ 模型校验通过：${r.activity_count} 个活动、${r.edge_count} 条边（前置条件已通过 Z3 可行性检查）`);
    $("#validateBox").appendChild(box);
    return true;
  } catch (ex) {
    showErrors(ex.message);
    if (!silent) toast("模型校验未通过", "error");
    return false;
  }
}

function showErrors(message) {
  const box = $("#validateBox");
  box.innerHTML = "";
  const errs = message.split("\n");
  box.appendChild(el("div", { class: "msg error" }, [
    "✗ 存在 " + errs.length + " 个问题：",
    el("ul", {}, errs.map(e => el("li", {}, e))),
  ]));
}

async function doCheck() {
  if (!(await doValidate(true))) {
    toast("请先修正模型错误", "error");
    return;
  }
  if (!currentModelId) {
    try {
      const saved = await api("/api/models", {
        method: "POST",
        body: { name: $("#modelName").value || model.name, spec: model.spec },
      });
      currentModelId = saved.id;
      await loadModels();
    } catch (ex) { toast(ex.message, "error"); return; }
  }
  const bound = Math.min(80, Math.max(1, +$("#boundInput").value || 16));
  $("#btnCheck").disabled = true;
  $("#btnCheck").textContent = "检验中…";
  try {
    checkResult = await api(`/api/models/${currentModelId}/check`, {
      method: "POST", body: { bound, persist: true },
    });
    forkCompare = null;
    viewMode = "original";
    renderVerdict(checkResult);
    if (checkResult.counterexample && checkResult.counterexample.steps.length) {
      setupReplay(checkResult.counterexample.steps);
    } else {
      $("#replayWrap").classList.add("hidden");
    }
    loadRuns();
  } catch (ex) {
    showErrors(ex.message);
    toast("检验失败", "error");
  } finally {
    $("#btnCheck").disabled = false;
    $("#btnCheck").textContent = "开始检验";
  }
}

function renderVerdict(result) {
  const box = $("#verdictBox");
  box.classList.remove("hidden", "violated", "exhausted", "bound_reached");
  box.classList.add(result.verdict);
  const badge = $("#verdictBadge");
  badge.className = "badge " + result.verdict;
  badge.textContent = {
    violated: "✗ 发现反例",
    exhausted: "✓ 边界内未违反",
    bound_reached: "⚠ 到达步数边界",
  }[result.verdict] || result.verdict;
  $("#verdictText").textContent = result.verdict_text;
  const s = result.stats;
  $("#verdictStats").innerHTML = "";
  const items = [
    ["步数边界", result.bound],
    ["访问不同状态", s.states_visited],
    ["展开转移", s.transitions_expanded],
    ["终态数", s.terminal_states],
    ["最大深度", s.max_depth_reached],
    ["是否截断", s.truncated ? "是" : "否"],
  ];
  if (result.counterexample) {
    items.unshift([result.verdict === "violated" ? "反例长度" : "代表轨迹长度",
                   result.counterexample.length]);
  }
  $("#verdictStats").append(...items.map(([k, v]) =>
    el("span", {}, [k, " ", el("b", {}, String(v))])));
}

function activeSteps() {
  if (appMode === "diff" && diffForkResult) return diffForkResult.counterexample.steps;
  if (viewMode === "fork" && forkCompare) return forkCompare.steps;
  if (appMode === "conc") return concReplaySteps || [];
  if (appMode === "risk") return riskReplaySteps || [];
  return checkResult.counterexample.steps;
}

function activeBound() {
  if (appMode === "diff") return diffResult?.bound || +$("#diffBoundInput").value || 16;
  if (appMode === "conc") return concResult?.bound || 24;
  if (appMode === "risk") return replayContext?.bound || riskResult?.bound || 24;
  return checkResult.bound;
}

function setupReplay(steps) {
  $("#replayWrap").classList.remove("hidden");
  replayIndex = 0;
  const slider = $("#stepSlider");
  slider.max = steps.length - 1;
  slider.value = 0;
  renderReplay();
}

function renderReplay() {
  const steps = activeSteps();
  const total = steps.length - 1;
  replayIndex = Math.max(0, Math.min(replayIndex, total));
  $("#stepIndicator").textContent = `${replayIndex} / ${total}`;
  $("#stepSlider").max = total;
  $("#stepSlider").value = replayIndex;

  // 事件链
  const line = $("#eventLine");
  line.innerHTML = "";
  steps.forEach((st, i) => {
    if (!st.event) return;
    const e = st.event;
    const kindClass = eventKindClass(e.type);
    const chip = el("div", {
      class: `ev-chip ev-kind-${kindClass}` +
        (i === replayIndex ? " current" : "") +
        (viewMode === "fork" && !st.shared_prefix ? " fork-diff" : ""),
      title: e.note || e.label,
      onclick: () => { replayIndex = i; renderReplay(); },
    }, [
      el("span", { class: "ev-type" }, `${i}. ${shortEventName(e.type, e)}`),
      el("span", { class: "ev-act" },
        [e.thread && appMode === "conc" ? e.thread : (e.activity || ""),
         e.attempt ? ` #${e.attempt}` : "",
        ].join("") + (e.race ? " 竞态!" : "") + (e.deadlock ? " 死锁!" : "")),
    ]);
    line.appendChild(chip);
  });
  line.scrollLeft = replayIndex * 80;

  renderSnapshot(steps[replayIndex], steps);
}

function eventKindClass(type) {
  if (type === "success" || type === "retry_timeout_clean") return "success";
  if (type === "retryable_error" || type === "comp_retryable_error") return "retry";
  if (type === "retry" || type === "comp_retry") return "retry";
  if (type === "fatal_error" || type === "attempts_exhausted") return "fatal";
  if (type === "comp_fatal_error" || type === "comp_attempts_exhausted") return "exhausted";
  if (type === "crash_before_ack") return "crash";
  if (type === "recover_pre_ack") return "retry";
  if (type === "recover_acknowledged") return "ack";
  if (type === "retry_timeout_duplicate") return "dup";
  if (type === "compensated") return "comp";
  if (type === "thread_schedule") return "thread";
  if (type === "lock_acquired" || type === "lock_wait") return "lock";
  if (type === "join_arrive" || type === "join_fire") return "join";
  if (type === "branch_ended_unjoinable") return "fatal";
  return "comp";
}

function shortEventName(type, ev) {
  const names = {
    invoke: "调用",
    success: "成功",
    retryable_error: "可重试错误",
    retry: ev && ev.ambiguous ? "超时重调" : "重新调用",
    fatal_error: "不可重试错误",
    attempts_exhausted: "尝试耗尽",
    crash_before_ack: "确认前崩溃",
    recover_pre_ack: "崩溃恢复:重调",
    recover_acknowledged: "恢复:已生效",
    retry_timeout_duplicate: "重调→重复!",
    retry_timeout_clean: "重调→干净",
    compensate: "补偿调用",
    compensated: ev && ev.activity ? "补偿成功" : "全部补偿完成",
    comp_retryable_error: "补偿错误",
    comp_retry: "补偿重调",
    comp_fatal_error: "补偿致命失败",
    comp_attempts_exhausted: "补偿耗尽",
    thread_schedule: "调度分支",
    lock_acquired: "获得锁",
    lock_wait: "等待锁",
    join_arrive: "到达汇合点",
    join_fire: "汇合!续跑",
    branch_ended_unjoinable: "分支提前结束·无法汇合",
  };
  return names[type] || type;
}

const STATUS_LABEL = {
  pending: "待执行", active: "执行中", retrying: "重试中", recovering: "恢复中",
  done: "已成功", failed: "已失败",
};

const CONC_STATUS_LABEL = {
  ready: "就绪", acquiring: "加锁中", active: "执行中", retrying: "重试中",
  recovering: "恢复中", waiting: "锁阻塞", joined: "已汇合",
  forked: "已分叉", ended: "汇合前已结束", cancelled: "已撤销",
};
const DEAD_THREAD = new Set(["joined", "forked", "ended", "cancelled"]);

function renderThreadLanes(snap) {
  const acts = $("#snapActivities");
  // 在线程区顶部插入泳道（每次重绘）
  let lanes = $("#threadLanes");
  if (!lanes) {
    lanes = el("div", { id: "threadLanes", class: "thread-lanes" });
    acts.parentElement.insertBefore(lanes, acts);
  }
  lanes.innerHTML = "";
  snap.threads.forEach(t => {
    const cls = DEAD_THREAD.has(t.status) ? "dead"
      : (t.status === "waiting" ? "waiting" : "running");
    const vc = Object.entries(t.clock).map(([k, v]) => `${k.replace("t000","t").replace("b_","")}:${v}`).join(" ");
    lanes.appendChild(el("div", { class: "thread-lane " + cls }, [
      el("span", { class: "tl-tid" }, t.tid),
      el("div", { class: "tl-body" }, [
        el("span", {}, `${CONC_STATUS_LABEL[t.status] || t.status}${t.activity_name ? " · " + t.activity_name : ""}`),
        t.tries ? el("span", { class: "si-meta" }, `#${t.tries}`) : null,
        t.waits_on ? el("span", { class: "tl-locks" }, `⛔等 ${t.waits_on}`) : null,
        t.have_locks.length ? el("span", { class: "tl-locks" }, "🔒 " + t.have_locks.join(",")) : null,
        t.groups.length ? el("span", { class: "si-meta" }, "组:" + t.groups.join(",")) : null,
        el("span", { class: "tl-clock" }, vc),
      ]),
    ]));
  });

  // 汇合令牌
  const joins = (snap.joins || []);
  if (joins.length) {
    const box = el("div", { class: "si-meta", style: "margin:4px 0" },
      joins.map(g => `汇合组 ${g.group}（${g.fork}→${g.join}）令牌 ${g.arrived.length}/${g.needed}`).join("；"));
    lanes.appendChild(box);
  }

  // 锁占用
  const heldLocks = (snap.locks || []).filter(l => l.owner);
  if (heldLocks.length) {
    const grid = el("div", { class: "lock-grid", style: "margin:4px 0" });
    snap.locks.forEach(l => {
      grid.appendChild(el("div", { class: "lock-cell" + (l.owner ? " held" : "") }, [
        "🔑 " + l.name + " ",
        l.owner ? el("span", { class: "lk-owner" }, "← " + l.owner) : el("span", { class: "si-meta" }, "空闲"),
      ]));
    });
    lanes.appendChild(grid);
  }

  // 资源访问史（向量时钟）
  const touched = (snap.resources || []).filter(r => r.history.length);
  if (touched.length) {
    const resBox = el("div", { style: "margin-top:4px" });
    touched.forEach(r => {
      resBox.appendChild(el("div", { class: "snap-item", style: "margin-bottom:3px" }, [
        el("span", { class: "si-name" }, "📄 " + r.key),
        el("span", { class: "si-meta" }, r.history.map(h =>
          `${h.mode === "w" ? "写" : "读"}@${h.tid}(${h.vc})${h.locks.length ? "🔒" + h.locks.join("") : ""}`).join(" → ")),
      ]));
    });
    lanes.appendChild(resBox);
  }
}

function renderSnapshot(step, steps) {
  const snap = step.snapshot;
  const concurrent = !!snap.concurrent;
  const phaseNames = { run: "正常执行", compensating: "补偿中（LIFO 撤销）" };
  $("#snapPhase").textContent = phaseNames[snap.phase] || snap.phase;
  const outcomeName = { succeeded: "成功完成", compensated: "已完全补偿",
                        failed: "失败", deadlocked: "锁顺序死锁",
                        unjoinable: "无法汇合" };
  $("#snapPhase").appendChild(el("span", { class: "si-meta", style: "margin-left:10px" },
    snap.outcome ? `终态：${outcomeName[snap.outcome] || snap.outcome}` : ""));

  // 并发模式：先渲染线程泳道；串行模式移除泳道容器
  let oldLanes = $("#threadLanes");
  if (oldLanes && !concurrent) oldLanes.remove();
  if (concurrent) renderThreadLanes(snap);

  // 活动
  const acts = $("#snapActivities");
  acts.innerHTML = "";
  if (concurrent) {
    acts.appendChild(el("div", { class: "si-meta", style: "margin-bottom:4px" },
      "各分支活动（按线程在上方泳道展示）"));
  }
  snap.activities.forEach(a => {
    acts.appendChild(el("div", { class: "snap-item" }, [
      el("span", { class: "si-name" }, `${a.name}（${a.id}）`),
      el("span", {}, [
        el("span", { class: "status-pill st-" + a.status }, STATUS_LABEL[a.status] || a.status),
        a.tries ? el("span", { class: "si-meta", style: "margin-left:6px" }, `尝试 ${a.tries}`) : null,
      ]),
    ]));
  });

  // 变量
  const vars = $("#snapVars");
  vars.innerHTML = "";
  const varEntries = Object.entries(snap.variables);
  if (!varEntries.length) vars.appendChild(el("div", { class: "si-meta" }, "（无变量）"));
  varEntries.forEach(([k, v]) => vars.appendChild(el("div", { class: "snap-item" }, [
    el("span", {}, k), el("b", {}, String(v)),
  ])));

  // 外部效果
  const eff = $("#snapEffects");
  eff.innerHTML = "";
  if (!snap.effects.length) eff.appendChild(el("div", { class: "si-meta" }, "（暂无外部效果）"));
  snap.effects.forEach((e) => {
    const inStack = snap.phase === "compensating" &&
      snap.comp_stack.includes(e.activity) && !e.undone;
    eff.appendChild(el("div", { class: "snap-item" }, [
      el("span", { class: "si-name" + (e.undone ? " effect-undone" : "") }, [
        "⚡ " + e.key,
        e.duplicate ? el("span", { class: "dup-flag" }, "重复!") : null,
        inStack ? el("span", { class: "stack-tag" }, "◄ 补偿栈") : null,
      ]),
      el("span", { class: "si-meta" }, [
        e.activity,
        e.undone ? " · 已撤销" : " · 生效中",
      ].join("")),
    ]));
  });
  if (snap.phase === "compensating") {
    const counts = snap.counts;
    eff.appendChild(el("div", { class: "si-meta", style: "margin-top:4px" },
      `生效中 ${Object.values(counts.effects).reduce((a, b) => a + b, 0)} 条 · 已撤销 ${Object.values(counts.undone).reduce((a, b) => a + b, 0)} 条`));
  }

  // 触发规则
  const rules = $("#snapRules");
  rules.innerHTML = "";
  const ev = step.event;
  if (ev) {
    rules.appendChild(el("div", { class: "snap-item" }, [
      el("div", { class: "rule-line" }, [
        el("span", { class: "rule-verdict taken" }, "事件"),
        el("span", {}, ev.label + (concurrent && ev.thread ? `（线程 ${ev.thread}`
          : ev.activity ? `（${ev.activity}` : "（")
          + (ev.activity && concurrent && ev.thread ? ` · ${ev.activity}` : "")
          + (ev.attempt ? `，第 ${ev.attempt} 次` : "")
          + ((concurrent && ev.thread) || ev.activity ? "）" : "")),
      ]),
      ev.note ? el("div", { class: "rule-expr", style: "margin-top:3px" }, ev.note) : null,
      ev.race ? el("div", { class: "rule-expr", style: "margin-top:3px;color:#ff8a96" },
        "⚠ " + ev.race.reason + "：资源 " + ev.race.resource) : null,
      ev.deadlock ? el("div", { class: "rule-expr", style: "margin-top:3px;color:#e8a83d" },
        "⚠ " + ev.deadlock.cycle.map(c => `${c.tid} 等 ${c.waits_lock}`).join(" → ")) : null,
      ev.lock ? el("div", { class: "rule-expr" },
        (ev.type === "lock_wait" ? "⛔ " : "🔑 ") + ev.lock
        + (ev.holder ? `（持有者 ${ev.holder}）` : "")) : null,
      ev.fork_group ? el("div", { class: "rule-expr" },
        "⑂ fork 汇合组 " + ev.fork_group + "，派生 " + (ev.branches || []).length + " 个分支："
        + (ev.branches || []).map(b => `${b.tid}→${b.target}`).join(", ")) : null,
      ev.join_group ? el("div", {
        class: "rule-expr" + (ev.type === "branch_ended_unjoinable"
          ? " conc-unjoinable-note" : ""),
      }, ev.type === "branch_ended_unjoinable"
        ? `⛔ 分支在汇合前结束：汇合组 ${ev.join_group} 的令牌无法交齐，永不汇合`
        : `🔀 汇合组 ${ev.join_group}：${ev.join_arrived}/${ev.join_needed}`) : null,
    ]));
    (ev.blocked || []).forEach(b => {
      rules.appendChild(el("div", { class: "snap-item" }, [
        el("div", { class: "rule-line" }, [
          el("span", { class: "rule-verdict blocked" },
            b.reason === "guard_false" ? "守卫为假" : "前置不成立"),
          el("span", {}, "→ " + b.target),
        ]),
        b.detail ? el("div", { class: "rule-expr" }, b.detail) : null,
      ]));
    });
    if (ev.target) {
      rules.appendChild(el("div", { class: "snap-item" }, [
        el("div", { class: "rule-line" }, [
          el("span", { class: "rule-verdict taken" }, "推进到"),
          el("span", {}, ev.target),
        ]),
      ]));
    }
  } else {
    rules.appendChild(el("div", { class: "si-meta" }, "初始状态：起始活动已入队"));
  }

  // 违反
  const vb = $("#violationBox");
  if (step.violations && step.violations.length) {
    vb.classList.remove("hidden");
    vb.innerHTML = "";
    vb.appendChild(el("div", { class: "v-title" },
      "✗ 破坏的不变量：" + step.violations.map(v => v.name).join("、")));
    step.violations.forEach(v =>
      vb.appendChild(el("div", { class: "v-detail" }, v.detail)));
  } else {
    vb.classList.add("hidden");
  }

  renderForkOptions(step);
}

function renderForkOptions(step) {
  const box = $("#forkOptions");
  box.innerHTML = "";
  const steps = activeSteps();
  const nextEvent = steps[replayIndex + 1]?.event;
  const alts = step.alternatives || [];
  if (!alts.length) {
    box.appendChild(el("div", { class: "si-meta" }, "当前为终态，没有可分叉的事件。"));
    return;
  }
  alts.forEach(a => {
    const sigStr = JSON.stringify(a.sig);
    const same = nextEvent && JSON.stringify(nextEvent.sig) === sigStr;
    const target = appMode === "conc"
      ? `${shortEventName(a.type, a)}${a.thread ? " · " + a.thread : ""}${a.activity ? " · " + a.activity : ""}${a.attempt ? " #" + a.attempt : ""}`
      : `${shortEventName(a.type, a)}${a.activity ? " · " + a.activity : ""}${a.attempt ? " #" + a.attempt : ""}`;
    box.appendChild(el("div", {
      class: "fork-opt" + (same ? " same" : ""),
      onclick: () => doFork(replayIndex, a.sig),
    }, [
      el("div", { class: "fo-main" }, [
        el("span", {}, target),
        el("span", { class: "fo-note" },
          (a.lock ? `锁 ${a.lock} · ` : "") + (a.note || a.label)),
      ]),
      el("span", { class: "fo-go" }, same ? "（轨迹当前选择）" : "改选并续跑 ►"),
    ]));
  });
}

async function doFork(prefixLen, sig) {
  const steps = activeSteps();
  const prefix = steps.slice(1, prefixLen + 1).map(s => s.event);
  const isRisk = appMode === "risk";
  const isConc = appMode === "conc";
  const isDiff = appMode === "diff";
  if (isDiff) return doDiffFork(prefix, sig);
  const forkPath = isConc ? "concurrency/fork" : isRisk ? "risk/fork" : "fork";
  const originalSteps = isConc ? concReplaySteps
    : isRisk ? riskReplaySteps : checkResult.counterexample.steps;
  try {
    const result = await api(`/api/models/${currentModelId}/${forkPath}`, {
      method: "POST",
      body: { prefix, sig, bound: activeBound(), persist: false },
    });
    forkCompare = { result, steps: result.counterexample.steps, originalSteps };
    viewMode = "fork";
    renderForkResult(result, prefixLen);
    setupReplay(forkCompare.steps);
    replayIndex = prefixLen + 1;
    renderReplay();
  } catch (ex) {
    toast(ex.message, "error");
  }
}

function renderForkResult(result, forkPoint) {
  const box = $("#forkResult");
  box.classList.remove("hidden");
  box.innerHTML = "";
  const badgeCls = result.verdict;
  box.appendChild(el("div", { class: "verdict-box " + badgeCls, style: "margin-bottom:10px" }, [
    el("div", { class: "verdict-row" }, [
      el("span", { class: "badge " + badgeCls },
        { violated: "✗ 分叉后违反", exhausted: "✓ 分叉后未违反", bound_reached: "⚠ 到达边界",
          conc_unsafe: "✗ 分叉后发现并发问题" }[result.verdict] || result.verdict),
      el("span", { class: "verdict-text" },
        `分叉点：第 ${forkPoint} 步后，改选「${shortEventName(result.chosen_event.type, result.chosen_event)}${result.chosen_event.thread ? " · " + result.chosen_event.thread : ""}${result.chosen_event.activity ? " · " + result.chosen_event.activity : ""}」。${result.verdict_text}`),
    ]),
  ]));
  box.appendChild(el("div", { class: "compare-toggle" }, [
    el("button", {
      class: "btn btn-small" + (viewMode === "original" ? " active" : ""),
      onclick: () => {
        viewMode = "original";
        setupReplay(forkCompare.originalSteps);
        replayIndex = Math.min(forkPoint, forkCompare.originalSteps.length - 1);
        renderReplay(); renderForkResult(result, forkPoint);
      },
    }, "查看原轨迹"),
    el("button", {
      class: "btn btn-small" + (viewMode === "fork" ? " active" : ""),
      onclick: () => { viewMode = "fork"; setupReplay(forkCompare.steps);
        replayIndex = forkPoint + 1; renderReplay(); renderForkResult(result, forkPoint); },
    }, "查看分叉轨迹"),
  ]));
}

// ---------------------------------------------------------------------------
// 随机故障风险分析：执行 / 报告 / 轨迹 / 回放
// ---------------------------------------------------------------------------

let riskReplaySteps = null;      // 当前风险轨迹回放步骤（含快照）

async function ensureModelSaved() {
  if (currentModelId) return true;
  if (!(await doValidate(true))) { toast("请先修正模型错误", "error"); return false; }
  try {
    const saved = await api("/api/models", {
      method: "POST",
      body: { name: $("#modelName").value || model.name, spec: model.spec },
    });
    currentModelId = saved.id;
    await loadModels();
    return true;
  } catch (ex) { toast(ex.message, "error"); return false; }
}

async function doRiskValidateSilent() {
  try {
    const v = await api("/api/risk/validate", {
      method: "POST",
      body: { spec: model.spec, activities: riskConfigPayload() },
    });
    renderRiskValidation(v.ok ? [] : v.errors);
    return v.ok;
  } catch (ex) {
    renderRiskValidation(ex.message.split("\n"));
    return false;
  }
}

function renderRiskValidation(errors) {
  const box = $("#riskValidationBox");
  box.innerHTML = "";
  if (!errors || !errors.length) return;
  box.appendChild(el("div", { class: "msg error" }, [
    "⚠ 概率校验错误（" + errors.length + "）：同一调用/补偿分支概率之和必须为 1，"
    + "且未启用结果必须为 0。已阻止分析：",
    el("ul", {}, errors.map(e => el("li", {}, e))),
  ]));
}

async function doRiskRun() {
  if (!(await ensureModelSaved())) return;
  if (!(await doRiskValidateSilent())) {
    toast("概率配置未通过校验", "error");
    return;
  }
  const bound = Math.min(80, Math.max(1, +$("#riskBoundInput").value || 24));
  const topN = Math.min(50, Math.max(1, +$("#riskTopNInput").value || 10));
  const btn = $("#btnRiskRun");
  btn.disabled = true; btn.textContent = "分析中…";
  try {
    riskResult = await api(`/api/models/${currentModelId}/risk`, {
      method: "POST",
      body: { activities: riskConfigPayload(), bound, top_n: topN, persist: true },
    });
    hideRiskReplay();
    renderRiskReport(riskResult);
    loadRiskRuns();
  } catch (ex) {
    renderRiskValidation(ex.message.split("\n"));
    toast("风险分析失败", "error");
  } finally {
    btn.disabled = false; btn.textContent = "执行风险分析";
  }
}

const RISK_CAT_META = {
  succeeded: ["成功完成", "r-succ"],
  compensated: ["补偿完成", "r-comp"],
  failed: ["补偿失败", "r-fail"],
  truncated: ["边界截断", "r-trunc"],
  violation: ["不变量违反", "r-viol"],
};

function pct(x) { return (100 * x).toFixed(2) + "%"; }

function renderRiskReport(r) {
  const report = $("#riskReport");
  report.classList.remove("hidden");
  if (r.verdict !== "risk_ok") {
    $("#riskBadge").className = "badge bound_reached";
    $("#riskBadge").textContent = "⚠ 概率校验未通过";
  } else {
    const truncated = r.stats.truncated;
    $("#riskBadge").className = "badge " + (truncated ? "bound_reached" : "exhausted");
    $("#riskBadge").textContent = truncated ? "⚠ 分析完成（存在截断）" : "✓ 分析完成";
  }
  $("#riskVerdictText").textContent = r.verdict_text;

  // 分布
  const d = r.distribution || {};
  const dist = $("#riskDistribution");
  dist.innerHTML = "";
  const rows = [
    ["succeeded", d.succeeded], ["compensated", d.compensated],
    ["failed", d.failed], ["truncated", d.truncated],
  ];
  rows.forEach(([cat, value]) => {
    const [label, cls] = RISK_CAT_META[cat];
    dist.appendChild(el("div", { class: "risk-bar-row" }, [
      el("span", { class: "risk-bar-label" }, label),
      el("div", { class: "risk-bar-track" }, [
        el("div", { class: "risk-bar " + cls, style: `width:${Math.max(value * 100, 0.4)}%` }),
      ]),
      el("span", { class: "risk-bar-val" }, pct(value || 0)),
    ]));
  });

  // 不变量违反
  const inv = $("#riskInvariants");
  inv.innerHTML = "";
  if (d.violation_any > 0) {
    inv.appendChild(el("div", { class: "risk-inv-any" },
      `任一不变量被违反的概率：${pct(d.violation_any)}` +
      (d.succeeded > 0 ? "（与结局分布正交：同一轨迹可能既成功又违反，如重复生效）" : "")));
  }
  (d.invariants || []).forEach(v => {
    inv.appendChild(el("div", { class: "risk-inv-row" }, [
      el("span", { class: "risk-bar-label" }, "✗ " + v.name),
      el("div", { class: "risk-bar-track" }, [
        el("div", { class: "risk-bar r-viol", style: `width:${Math.max(v.probability * 100, 0.4)}%` }),
      ]),
      el("span", { class: "risk-bar-val" }, pct(v.probability)),
    ]));
  });

  // 期望未撤销效果
  const eo = r.expected_outstanding || {};
  const ex = $("#riskExpected");
  ex.innerHTML = "";
  ex.appendChild(el("div", { class: "risk-expected-main" + ((eo.failed || 0) > 0 ? " danger" : "") },
    `补偿失败永久残留的期望效果数：${(eo.failed || 0).toFixed(4)}`));
  if (eo.truncated > 0) {
    ex.appendChild(el("div", { class: "risk-expected-sub" },
      `⚠ 截断部分在边界处仍携带的未撤销效果期望：${eo.truncated.toFixed(4)}（结论不完整）`));
  }
  (eo.by_key_failed || []).forEach(k => {
    ex.appendChild(el("div", { class: "risk-expected-sub" },
      `⚡ ${k.key}：残留期望 ${k.expected.toFixed(4)}`));
  });

  renderRiskTraces(r.top_traces || []);

  const s = r.stats;
  $("#riskStats").innerHTML = "";
  $("#riskStats").append(...[
    ["步数上限", r.bound],
    ["合并状态数", s.states],
    ["展开转移", s.transitions_expanded],
    ["最大深度", s.max_depth_reached],
    ["截断概率", pct(s.truncated_probability || 0)],
    ["概率残差", (s.probability_residual || 0).toExponential(1)],
  ].map(([k, v]) => el("span", {}, [k, " ", el("b", {}, String(v))])));
  $("#riskStats").appendChild(el("span", { class: "hint", title:
    `每个状态桶保留贡献最大的 ${s.trace_keep_per_state} 条前缀，概率低于 ${s.trace_min_probability} 的前缀不参与排序；概率分布本身精确。` },
    "轨迹归因：每状态保留前 " + s.trace_keep_per_state + " 条代表前缀（分布精确，轨迹为近似召回）"));
}

function traceEventSummary(row) {
  return row.events.map(e =>
    shortEventName(e.type, e) + (e.activity ? `·${e.activity}` : "")).join(" → ");
}

function renderRiskTraces(traces) {
  const body = $("#riskTraceBody");
  body.innerHTML = "";
  const sorted = [...traces].sort((a, b) => {
    const k = riskSort.key;
    const va = a[k], vb = b[k];
    const cmp = typeof va === "number" ? va - vb : String(va).localeCompare(String(vb));
    return riskSort.desc ? -cmp : cmp;
  });
  sorted.forEach(row => {
    const meta = RISK_CAT_META[row.category] || [row.category_label, ""];
    const catLabel = row.category === "violation"
      ? row.violations.map(v => v.name).join("、")
      : meta[0];
    const tr = el("tr", {
      class: "risk-trace-row cat-" + row.category + (row.truncated ? " truncated" : ""),
      onclick: () => replayRiskTrace(row),
      title: "点击回放该轨迹（可在任意节点分叉）",
    }, [
      el("td", { class: "num" }, String(row.rank)),
      el("td", { class: "num" }, pct(row.probability)),
      el("td", {}, el("span", { class: "risk-cat-pill " + meta[1] },
        catLabel + (row.truncated ? " ⚠" : ""))),
      el("td", { class: "num" }, String(row.length)),
      el("td", { class: "trace-events" }, traceEventSummary(row)),
    ]);
    body.appendChild(tr);
  });
  $$(".risk-table th[data-sort]").forEach(th => {
    th.classList.toggle("sorted", th.dataset.sort === riskSort.key);
  });
}

async function replayRiskTrace(row) {
  if (!currentModelId) return;
  try {
    const rep = await api(`/api/models/${currentModelId}/risk/replay`, {
      method: "POST",
      body: { events: row.events },
    });
    appMode = "risk";
    replayContext = { mode: "risk", bound: riskResult.bound, row };
    forkCompare = null;
    viewMode = "original";
    riskReplaySteps = rep.counterexample.steps;
    $("#checkView").classList.add("hidden");
    $("#riskView").classList.remove("hidden");
    $("#riskReport").classList.add("hidden");
    const bar = $("#riskReplayBar");
    bar.classList.remove("hidden");
    $("#riskReplayTitle").textContent =
      `轨迹回放：${RISK_CAT_META[row.category]?.[0] || row.category_label} · 概率 ${pct(row.probability)}` +
      (row.truncated ? " · 边界截断轨迹" : "");
    $("#forkResult").classList.add("hidden");
    setupReplay(riskReplaySteps);
    replayIndex = riskReplaySteps.length - 1;
    renderReplay();
    $("#replayWrap").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (ex) {
    toast("轨迹回放失败：" + ex.message, "error");
  }
}

function hideRiskReplay() {
  riskReplaySteps = null;
  $("#riskReplayBar").classList.add("hidden");
  $("#replayWrap").classList.add("hidden");
  $("#forkResult").classList.add("hidden");
  $("#riskReport")?.classList.remove("hidden");
}

async function loadRiskRuns() {
  if (!currentModelId) return;
  const runs = await api(`/api/models/${currentModelId}/risk/runs`);
  const box = $("#riskRunHistory");
  box.innerHTML = "";
  if (!runs.length) {
    box.appendChild(el("div", { class: "si-meta" }, "暂无记录"));
    return;
  }
  runs.forEach(r => {
    box.appendChild(el("div", {
      class: "run-row",
      onclick: async () => {
        const full = await api(`/api/risk/runs/${r.id}`);
        riskResult = full.result;
        hideRiskReplay();
        renderRiskReport(riskResult);
      },
    }, [
      el("span", { class: "mini-badge exhausted" }, "风险"),
      el("span", {}, `边界 ${r.bound} · 前 ${r.top_n} 条`),
      el("span", { class: "run-time" }, new Date(r.created_at * 1000).toLocaleTimeString()),
    ]));
  });
}

// ---------------------------------------------------------------------------
// 并发调度安全分析：执行 / 报告 / 见证回放 / 分叉
// ---------------------------------------------------------------------------

const CONC_KIND_META = {
  data_race: ["数据竞态", "f-data_race"],
  deadlock: ["锁顺序死锁", "f-deadlock"],
  unjoinable: ["无法汇合的分支", "f-unjoinable"],
  invariant: ["不变量被破坏", "f-invariant"],
};

async function doConcRun() {
  if (!(await ensureModelSaved())) return;
  if (!model.spec.concurrency?.groups?.length) {
    toast("请先在「并发调度」标签中声明至少一个 fork/join 汇合组", "error");
    return;
  }
  if (!(await doValidate(true))) { toast("请先修正模型错误", "error"); return; }
  const bound = Math.min(80, Math.max(1, +$("#concBoundInput").value || 24));
  const btn = $("#btnConcRun");
  btn.disabled = true; btn.textContent = "分析中…";
  try {
    concResult = await api(`/api/models/${currentModelId}/concurrency`, {
      method: "POST", body: { bound, persist: true },
    });
    hideConcReplay();
    renderConcReport(concResult);
    loadConcRuns();
  } catch (ex) {
    const box = $("#concValidationBox");
    box.innerHTML = "";
    box.appendChild(el("div", { class: "msg error" }, String(ex.message).split("\n")
      .map(m => el("div", {}, m))));
    toast("并发分析失败", "error");
  } finally {
    btn.disabled = false; btn.textContent = "并发调度安全分析";
  }
}

function renderConcReport(r) {
  const report = $("#concReport");
  report.classList.remove("hidden");
  const badge = $("#concBadge");
  badge.className = "badge " + r.verdict;
  badge.textContent = {
    conc_unsafe: "✗ 发现并发安全问题",
    exhausted: "✓ 边界内安全",
    bound_reached: "⚠ 到达步数边界",
  }[r.verdict] || r.verdict;
  $("#concVerdictText").textContent = r.verdict_text;

  // 问题类别切换
  const kinds = Object.keys(r.findings || {});
  concActiveKind = r.primary_kind || kinds[0] || null;
  const tabs = $("#concFindingTabs");
  tabs.innerHTML = "";
  if (!kinds.length) {
    tabs.appendChild(el("span", { class: "hint" },
      "未发现数据竞态、锁顺序死锁或无法汇合的分支。下方统计后可在见证轨迹中单步回放代表调度。"));
  }
  kinds.forEach(k => {
    tabs.appendChild(el("button", {
      class: "finding-tab " + CONC_KIND_META[k][1] + (k === concActiveKind ? " active" : ""),
      onclick: () => { concActiveKind = k; renderConcReport(r); },
    }, CONC_KIND_META[k][0] + " (" + r.findings[k].length + " 步)"));
  });
  renderConcFinding(r);

  const s = r.stats;
  $("#concStats").innerHTML = "";
  $("#concStats").append(...[
    ["步数边界", r.bound],
    ["合并指纹状态", s.states_visited],
    ["展开转移", s.transitions_expanded],
    ["最大深度", s.max_depth_reached],
    ["是否截断", s.truncated ? "是" : "否"],
  ].map(([k, v]) => el("span", {}, [k, " ", el("b", {}, String(v))])));

  // 默认回放主见证（或代表轨迹）
  const steps = concActiveKind ? r.findings[concActiveKind].steps
                               : r.counterexample.steps;
  concReplaySteps = steps;
  $("#concReplayBar").classList.remove("hidden");
  $("#concReplayTitle").textContent =
    concActiveKind ? `见证轨迹回放：${CONC_KIND_META[concActiveKind][0]} · ${r.findings[concActiveKind].length} 步`
                   : "代表调度回放";
  setupReplay(steps);
  $("#replayWrap").classList.remove("hidden");
}

function renderConcFinding(r) {
  const box = $("#concFindingCard");
  box.innerHTML = "";
  if (!concActiveKind || !r.findings[concActiveKind]) {
    box.appendChild(el("div", { class: "finding-card" }, [
      el("div", { class: "finding-detail" },
        "下方轨迹区给出一条按稳定事件序选择的代表调度，可单步回放并从任意节点分叉。"),
    ]));
    return;
  }
  const w = r.findings[concActiveKind];
  const card = el("div", { class: "finding-card " + CONC_KIND_META[concActiveKind][1] });
  card.appendChild(el("div", { class: "finding-head" }, [
    el("span", { class: "finding-title" }, CONC_KIND_META[concActiveKind][0]),
    el("span", { class: "hint" }, `最短调度 ${w.length} 步（按稳定事件序选择）`),
  ]));
  card.appendChild(el("div", { class: "finding-detail" }, w.detail));

  const meta = el("div", { class: "finding-meta" });
  if (w.resources?.length) {
    meta.appendChild(el("div", { class: "fm-row" }, [
      "涉及资源/锁：",
      ...w.resources.map(x => el("span", { class: "res-chip" }, x)),
    ]));
  }
  if (w.accesses?.length) {
    meta.appendChild(el("div", { class: "fm-row" },
      "冲突访问：" + w.accesses.map(a =>
        `${a.tid} 在 ${a.activity} 上${a.mode === "w" ? "写" : "读"}`
        + (a.locks?.length ? "（持锁 " + a.locks.join(",") + "）" : "（无锁）")).join("　↔　")));
  }
  if (w.blocking?.length) {
    w.blocking.forEach(b => meta.appendChild(el("div", { class: "fm-row" },
      "阻塞：" + b.tid + (b.activity ? `（${b.activity}）` : "") + " — " + b.reason)));
  }
  if (w.groups?.length) {
    w.groups.forEach(g => meta.appendChild(el("div", { class: "fm-row" },
      `汇合组 ${g.group}（${g.fork}→${g.join}）：已到 ${g.arrived.length}/${g.needed}，缺 ${g.missing} 个分支`
      + (g.branches?.length ? "；未到：" + g.branches.map(b =>
          `${b.tid}@${b.activity}${b.waits_on ? " 等锁" + b.waits_on : ""}`).join("，") : ""))));
  }
  if (w.violations?.length) {
    w.violations.forEach(v => meta.appendChild(el("div", { class: "fm-row" },
      "不变量：" + v.name)));
  }
  card.appendChild(meta);
  box.appendChild(card);
}

function hideConcReplay() {
  concReplaySteps = null;
  $("#concReplayBar").classList.add("hidden");
  $("#replayWrap").classList.add("hidden");
  $("#forkResult").classList.add("hidden");
  $("#concReport")?.classList.remove("hidden");
}

async function loadConcRuns() {
  if (!currentModelId) return;
  const runs = await api(`/api/models/${currentModelId}/concurrency/runs`);
  const box = $("#concRunHistory");
  box.innerHTML = "";
  if (!runs.length) {
    box.appendChild(el("div", { class: "si-meta" }, "暂无记录"));
    return;
  }
  runs.forEach(r => {
    box.appendChild(el("div", {
      class: "run-row",
      onclick: async () => {
        const full = await api(`/api/concurrency/runs/${r.id}`);
        concResult = full.result;
        hideConcReplay();
        renderConcReport(concResult);
      },
    }, [
      el("span", { class: "mini-badge " + r.verdict },
        { conc_unsafe: "并发", exhausted: "安全", bound_reached: "截断" }[r.verdict] || r.verdict),
      el("span", {}, `边界 ${r.bound}`),
      el("span", { class: "run-time" }, new Date(r.created_at * 1000).toLocaleTimeString()),
    ]));
  });
}

// ---------------------------------------------------------------------------
// 检验记录
// ---------------------------------------------------------------------------

async function loadRuns() {
  if (!currentModelId) return;
  const runs = await api(`/api/models/${currentModelId}/runs`);
  const box = $("#runHistory");
  box.innerHTML = "";
  if (!runs.length) {
    box.appendChild(el("div", { class: "si-meta" }, "暂无记录"));
    return;
  }
  runs.forEach(r => {
    box.appendChild(el("div", {
      class: "run-row",
      onclick: async () => {
        const full = await api(`/api/runs/${r.id}`);
        checkResult = full.result;
        forkCompare = null;
        viewMode = "original";
        $("#forkResult").classList.add("hidden");
        renderVerdict(checkResult);
        if (checkResult.counterexample) setupReplay(checkResult.counterexample.steps);
        else $("#replayWrap").classList.add("hidden");
      },
    }, [
      el("span", { class: "mini-badge " + r.verdict },
        { violated: "反例", exhausted: "干净", bound_reached: "截断" }[r.verdict] || r.verdict),
      el("span", {}, `边界 ${r.bound}`),
      el("span", { class: "run-time" }, new Date(r.created_at * 1000).toLocaleTimeString()),
    ]));
  });
}

// ---------------------------------------------------------------------------
// 模型持久化
// ---------------------------------------------------------------------------

async function loadModels(selectId = null) {
  const models = await api("/api/models");
  window.__modelsCache = models;
  renderModelSelect(models);
  if (selectId) {
    currentModelId = selectId;
    $("#modelSelect").value = selectId;
  }
  if (appMode === "diff") populateDiffModelSelects();
}

async function saveModel() {
  if (!(await doValidate(true))) { toast("模型存在错误，未保存", "error"); return; }
  const name = $("#modelName").value || "未命名模型";
  try {
    if (currentModelId) {
      await api(`/api/models/${currentModelId}`, { method: "PUT", body: { name, spec: model.spec } });
      toast("已保存修改", "ok");
    } else {
      const saved = await api("/api/models", { method: "POST", body: { name, spec: model.spec } });
      currentModelId = saved.id;
      toast("已保存新模型", "ok");
    }
    await loadModels(currentModelId);
  } catch (ex) { toast(ex.message, "error"); }
}

function openModel(m) {
  currentModelId = m.id;
  model = { name: m.name, spec: m.spec };
  $("#modelName").value = m.name;
  $("#modelSelect").value = m.id;
  checkResult = null;
  forkCompare = null;
  riskConfig = {};
  riskResult = null;
  riskReplaySteps = null;
  replayContext = null;
  concResult = null;
  concReplaySteps = null;
  concActiveKind = null;
  diffResult = null;
  diffWitness = null;
  diffRunRecord = null;
  diffForkResult = null;
  $("#verdictBox").classList.add("hidden");
  $("#replayWrap").classList.add("hidden");
  $("#forkResult").classList.add("hidden");
  $("#riskReport").classList.add("hidden");
  $("#riskReplayBar").classList.add("hidden");
  $("#riskValidationBox").innerHTML = "";
  $("#concReport").classList.add("hidden");
  $("#concReplayBar").classList.add("hidden");
  $("#concValidationBox").innerHTML = "";
  $("#diffReport").classList.add("hidden");
  $("#diffReplayWrap").classList.add("hidden");
  $("#diffReplayBar").classList.add("hidden");
  $("#diffForkResult").classList.add("hidden");
  $("#diffValidationBox").innerHTML = "";
  switchAppMode("check");
  renderAll();
  loadRuns();
  loadRiskRuns();
  loadConcRuns();
  loadDiffRuns();
}

async function loadExamplesMenu() {
  const examples = await api("/api/examples");
  const menu = $("#exampleMenu");
  menu.innerHTML = "";
  examples.forEach((ex, i) => {
    menu.appendChild(el("button", {
      onclick: () => {
        model = { name: ex.name, spec: withDefaults(ex.spec) };
        currentModelId = null;
        riskConfig = {};
        riskResult = null;
        riskReplaySteps = null;
        replayContext = null;
        concResult = null;
        concReplaySteps = null;
        concActiveKind = null;
        diffResult = null;
        diffWitness = null;
        diffRunRecord = null;
        diffForkResult = null;
        $("#modelName").value = ex.name;
        $("#modelSelect").value = "";
        $("#verdictBox").classList.add("hidden");
        $("#replayWrap").classList.add("hidden");
        $("#forkResult").classList.add("hidden");
        $("#riskReport").classList.add("hidden");
        $("#riskReplayBar").classList.add("hidden");
        $("#riskValidationBox").innerHTML = "";
        $("#concReport").classList.add("hidden");
        $("#concReplayBar").classList.add("hidden");
        $("#concValidationBox").innerHTML = "";
        $("#diffReport").classList.add("hidden");
        $("#diffReplayWrap").classList.add("hidden");
        $("#diffReplayBar").classList.add("hidden");
        $("#diffForkResult").classList.add("hidden");
        $("#diffValidationBox").innerHTML = "";
        checkResult = null;
        switchAppMode("check");
        renderAll();
        menu.classList.add("hidden");
      },
    }, `${i + 1}. ${ex.name}`));
  });
}

// ---------------------------------------------------------------------------
// 版本行为对比：执行 / 报告 / 双列对齐回放 / 从差异节点分叉复检
// ---------------------------------------------------------------------------

const DIFF_KIND_META = {
  terminal: ["终态变化", "f-terminal"],
  variables: ["变量变化", "f-variables"],
  effects: ["外部效果变化", "f-effects"],
  invariant: ["不变量结论变化", "f-invariant"],
  divergence: ["轨迹分叉", "f-divergence"],
};
const DIFF_VERDICT_META = {
  diff_found: ["✗ 发现版本行为差异", "diff_found"],
  diff_found_incomplete: ["⚠ 发现差异（搜索未完备）", "diff_found_incomplete"],
  inconclusive: ["⚠ 未发现差异但搜索未完备", "inconclusive"],
  identical: ["✓ 边界内两版本行为一致", "identical"],
};
const DIFF_OUTCOME_NAME = {
  succeeded: "成功完成", compensated: "已完全补偿", failed: "失败（效果残留）",
};

function populateDiffModelSelects() {
  const fill = (selId, keep) => {
    const sel = $(selId);
    const prev = sel.value || keep;
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "" }, "— 选择已保存模型 —"));
    window.__modelsCache?.forEach(m => sel.appendChild(el("option", {
      value: m.id, ...(m.id === prev ? { selected: true } : {}),
    }, m.name)));
  };
  fill("#diffBaseSelect");
  fill("#diffCandSelect");
}

async function doDiffRun() {
  const baselineId = $("#diffBaseSelect").value;
  const candidateId = $("#diffCandSelect").value;
  const box = $("#diffValidationBox");
  box.innerHTML = "";
  if (!baselineId || !candidateId) {
    box.appendChild(el("div", { class: "msg error" }, "请先分别选择基线与候选模型（需先在顶部保存模型）。"));
    return;
  }
  if (baselineId === candidateId) {
    box.appendChild(el("div", { class: "msg error" }, "基线模型与候选模型不能是同一个模型。"));
    return;
  }
  const bound = Math.min(80, Math.max(1, +$("#diffBoundInput").value || 16));
  const btn = $("#btnDiffRun");
  btn.disabled = true; btn.textContent = "版本对比中…";
  try {
    diffResult = await api("/api/diff", {
      method: "POST",
      body: { baseline_id: baselineId, candidate_id: candidateId, bound, persist: true },
    });
    diffRunRecord = null;
    diffForkResult = null;
    $("#diffForkResult").classList.add("hidden");
    hideDiffReplay();
    renderDiffReport(diffResult);
    loadDiffRuns();
  } catch (ex) {
    box.innerHTML = "";
    box.appendChild(el("div", { class: "msg error" },
      String(ex.message).split("\n").map(m => el("div", {}, m))));
    toast("版本对比失败", "error");
  } finally {
    btn.disabled = false; btn.textContent = "执行版本对比";
  }
}

function diffAllWitnesses(r) {
  const map = {};
  for (const [k, w] of Object.entries(r.witnesses || {})) map[k] = w;
  if (r.divergence) map.divergence = r.divergence;
  return map;
}

function renderDiffReport(r) {
  const report = $("#diffReport");
  report.classList.remove("hidden");
  const meta = DIFF_VERDICT_META[r.verdict] || [r.verdict, ""];
  const badge = $("#diffBadge");
  badge.className = "badge " + meta[1];
  badge.textContent = meta[0];
  $("#diffVerdictText").textContent = r.verdict_text;

  const inc = $("#diffIncomplete");
  inc.innerHTML = "";
  if (r.incompleteness && r.incompleteness.length) {
    inc.classList.remove("hidden");
    inc.appendChild(el("strong", {}, "⚠ 结果未完备："));
    r.incompleteness.forEach(t => inc.appendChild(el("div", {}, "• " + t)));
  } else {
    inc.classList.add("hidden");
  }

  // 结构 / 不变量配置差异提示
  const sb = $("#diffStructure");
  sb.innerHTML = "";
  const st = r.structure || {};
  if (st.activities_only_baseline?.length || st.activities_only_candidate?.length) {
    const row = el("div", { class: "ds-row" });
    if (st.activities_only_baseline.length)
      row.appendChild(el("span", {}, ["仅基线有活动：", el("b", {}, st.activities_only_baseline.join(", "))]));
    if (st.activities_only_candidate.length)
      row.appendChild(el("span", {}, (st.activities_only_baseline.length ? "　｜　" : ""),
        ["仅候选有活动：", el("b", {}, st.activities_only_candidate.join(", "))]));
    sb.appendChild(row);
  }
  const icd = r.invariant_config_diff || {};
  if (icd.only_baseline?.length || icd.only_candidate?.length) {
    const row = el("div", { class: "ds-row" });
    if (icd.only_baseline.length)
      row.appendChild(el("span", {}, ["仅基线启用的不变量：", el("b", {}, icd.only_baseline.join("、"))]));
    if (icd.only_candidate.length)
      row.appendChild(el("span", {}, (icd.only_baseline.length ? "　｜　" : ""),
        ["仅候选启用的不变量：", el("b", {}, icd.only_candidate.join("、"))]));
    sb.appendChild(row);
  }
  sb.appendChild(el("div", { class: "ds-row hint" }, r.semantics_note));

  // 见证页签（四类状态差异 + 轨迹分叉，按稳定顺序）
  const witnesses = diffAllWitnesses(r);
  const order = ["invariant", "terminal", "effects", "variables", "divergence"];
  const kinds = order.filter(k => witnesses[k]);
  const preferred = r.primary_kind || (kinds.includes("divergence") ? "divergence" : null);
  diffWitnessKind = (preferred && kinds.includes(preferred)) ? preferred : (kinds[0] || null);
  const tabs = $("#diffWitnessTabs");
  tabs.innerHTML = "";
  if (!kinds.length) {
    tabs.appendChild(el("span", { class: "hint" },
      r.stats.truncated ? "未发现对齐差异，但结果未完备（见上方警告）。"
                        : "在该步数边界内未发现任何行为差异。"));
  }
  kinds.forEach(k => {
    const len = witnesses[k].length ?? (witnesses[k].diverge_index);
    tabs.appendChild(el("button", {
      class: "finding-tab " + DIFF_KIND_META[k][1] + (k === diffWitnessKind ? " active" : ""),
      onclick: () => { diffWitnessKind = k; renderDiffReport(r); },
    }, DIFF_KIND_META[k][0] + " (" + len + " 步)"));
  });
  renderDiffWitnessCard(r, witnesses);

  const s = r.stats;
  $("#diffStats").innerHTML = "";
  $("#diffStats").append(...[
    ["步数边界", r.bound],
    ["对齐状态对", s.pairs_visited],
    ["对齐展开", s.aligned_expansions],
    ["轨迹分叉点", s.divergence_points],
    ["单侧续跑起点", s.continuation_points ?? 0],
    ["单侧续跑状态", s.continuation_states ?? 0],
    ["对齐终态对", s.terminal_pairs],
    ["最大对齐深度", s.max_depth_reached],
    ["是否完备", s.truncated ? "否（触及边界）" : "是（含分叉后续跑）"],
  ].map(([k, v]) => el("span", {}, [k, " ", el("b", {}, String(v))])));

  // 默认打开主见证的双列回放
  if (diffWitnessKind) {
    openDiffWitness(witnesses[diffWitnessKind], diffWitnessKind);
  } else {
    hideDiffReplay();
  }
}

function renderDiffWitnessCard(r, witnesses) {
  const box = $("#diffWitnessCard");
  box.innerHTML = "";
  if (!diffWitnessKind) return;
  const w = witnesses[diffWitnessKind];
  const cls = DIFF_KIND_META[diffWitnessKind][1];
  const card = el("div", { class: "finding-card " + cls });
  card.appendChild(el("div", { class: "finding-head" }, [
    el("span", { class: "finding-title" }, DIFF_KIND_META[diffWitnessKind][0]),
    el("span", { class: "hint" },
      `最短对齐轨迹 ${w.length} 步（按稳定事件签名对齐、字典序最小）`),
    el("button", { class: "btn btn-small", style: "margin-left:auto",
      onclick: () => openDiffWitness(w, diffWitnessKind) }, "▶ 回放该见证"),
  ]));

  if (diffWitnessKind === "divergence") {
    card.appendChild(el("div", { class: "finding-detail" }, w.detail));
    const sideZh = w.side === "baseline" ? "基线" : "候选";
    const otherZh = w.side === "baseline" ? "候选" : "基线";
    card.appendChild(el("div", { class: "diff-change-list" }, [
      el("div", { class: "diff-change-item" }, [
        el("span", { class: "dc-tag divergence" }, "独有事件"),
        el("span", {}, `${sideZh}在第 ${w.diverge_index} 步可触发「${w.event.label}」
          （${w.event.activity || "—"}${w.event.attempt ? " #" + w.event.attempt : ""}），
          ${otherZh}无此交错；回放中可在该节点直接对${sideZh}改选复检。`)]),
      el("div", { class: "diff-change-item" }, [
        el("span", { class: "dc-tag divergence" }, "分叉后终态"),
        el("span", {}, `${sideZh}执行后：${DIFF_OUTCOME_NAME[w.diverging_terminal] || "非终态"}；
          ${otherZh}停留：${DIFF_OUTCOME_NAME[w.held_terminal] || "非终态"}`)])]));
  } else {
    const list = el("div", { class: "diff-change-list" });
    (w.changes || []).forEach(ch => {
      list.appendChild(el("div", { class: "diff-change-item" }, [
        el("span", { class: "dc-tag " + ch.kind }, DIFF_KIND_META[ch.kind]?.[0] || ch.kind),
        el("span", {}, ch.detail),
      ]));
    });
    card.appendChild(list);
    if (w.via_divergence) {
      const exhausted = !r.stats?.truncated;
      const contNote = w.continuation
        ? `该差异在独有事件之后的单侧续跑中成立：对齐到第 ${w.diverge_index - 1} 步后，
           ${w.side === "baseline" ? "基线" : "候选"}独有的「${w.event.label}
           （${w.event.activity || "—"}）」触发轨迹分叉，该侧继续独立推进
           ${w.continuation_steps} 步（另一侧在分叉点持留）后出现上述变化。`
        : `⚠ 该差异随轨迹分叉产生：对齐到第 ${w.length - 1} 步后，
           ${w.side === "baseline" ? "基线" : "候选"}独有的「${w.event.label}
           （${w.event.activity || "—"}）」造成上述变化，另一侧在该步保持。`;
      const tailNote = exhausted
        ? "分叉后的单侧可达状态已在步数上限内枚举完毕（按状态指纹去重）。"
        : "注意：单侧续跑触及步数/状态上限，分叉之后仍可能有未发现的差异（见上方未完备标注）。";
      card.appendChild(el("div", { class: "finding-detail", style: "margin-top:8px;color:#e8c884" },
        contNote + tailNote));
    }
    card.appendChild(el("div", { class: "finding-detail", style: "margin-top:8px" },
      "下方双列回放逐对齐步展示两侧快照；差异节点高亮，并可对任意一侧「改选并续跑」做分叉复检。"));
  }
  box.appendChild(card);
}

function openDiffWitness(w, kind) {
  diffWitness = w;
  diffWitnessKind = kind;
  $("#diffReplayBar").classList.remove("hidden");
  const via = w.via_divergence ? " · 随轨迹分叉产生" : "";
  $("#diffReplayTitle").textContent =
    `差异见证回放：${DIFF_KIND_META[kind][0]} · 对齐长度 ${w.length}${via}`;
  $("#diffReplayWrap").classList.remove("hidden");
  $("#diffReport").classList.remove("hidden");
  const maxI = Math.max(w.baseline_steps.length, w.candidate_steps.length) - 1;
  // 分叉类见证（含随分叉产生的别名差异）定位到独有事件步
  diffIndex = (w.diverge_index != null) ? w.diverge_index : w.length;
  $("#diffStepSlider").max = maxI;
  renderDiffReplay();
  $("#diffReplayWrap").scrollIntoView({ behavior: "smooth", block: "start" });
}

function hideDiffReplay() {
  $("#diffReplayBar").classList.add("hidden");
  $("#diffReplayWrap").classList.add("hidden");
}

function renderDiffReplay() {
  const w = diffWitness;
  if (!w) return;
  const bSteps = w.baseline_steps;
  const cSteps = w.candidate_steps;
  const maxI = Math.max(bSteps.length, cSteps.length) - 1;
  diffIndex = Math.max(0, Math.min(diffIndex, maxI));
  $("#diffStepIndicator").textContent = `${diffIndex} / ${maxI}`;
  $("#diffStepSlider").max = maxI;
  $("#diffStepSlider").value = diffIndex;

  const names = diffSideNames();
  $("#diffBaseHead").innerHTML = "";
  $("#diffBaseHead").append(
    el("span", { class: "side-pill" }, "基线"),
    el("span", { class: "model-name" }, names.base));
  $("#diffCandHead").innerHTML = "";
  $("#diffCandHead").append(
    el("span", { class: "side-pill" }, "候选"),
    el("span", { class: "model-name" }, names.cand));

  renderDiffEventLine(bSteps, cSteps, w);
  renderDiffColumn("#diffSnapBase", bSteps[diffIndex], cSteps[diffIndex], "baseline", w);
  renderDiffColumn("#diffSnapCand", cSteps[diffIndex], bSteps[diffIndex], "candidate", w);
  renderDiffChangeAt(bSteps, cSteps);
}

function diffSideNames() {
  const params = diffRunRecord?.params;
  if (params) return { base: params.baseline_name, cand: params.candidate_name };
  const bs = $("#diffBaseSelect");
  const cs = $("#diffCandSelect");
  return {
    base: bs.options[bs.selectedIndex]?.textContent?.replace(/^— .* —$/, "") || "基线",
    cand: cs.options[cs.selectedIndex]?.textContent?.replace(/^— .* —$/, "") || "候选",
  };
}

function renderDiffEventLine(bSteps, cSteps, w) {
  const wrap = $("#diffEventLine");
  wrap.innerHTML = "";
  const makeSide = (steps, side) => {
    const box = el("div", { class: "diff-event-side" },
      el("span", { class: "side-label" }, side === "baseline" ? "基线" : "候选"));
    steps.forEach((st, i) => {
      if (i === 0) return;
      const e = st.event;
      const held = !!st.hold;
      const isDivergeStep = w.diverge_index != null && i === w.diverge_index;
      const holdText = isDivergeStep
        ? "（另一侧独有事件）"
        : "（分叉后该侧继续，此侧持留）";
      const chip = el("div", {
        class: "ev-chip" + (i === diffIndex ? " current" : "")
          + (st.diff_point ? " fork-diff" : "") + (held ? " held-chip" : ""),
        title: held ? holdText : (e?.note || e?.label || ""),
        onclick: () => { diffIndex = i; renderDiffReplay(); },
      }, held
        ? [el("span", { class: "ev-type" }, `${i}. ⏸ 保持`),
           el("span", { class: "ev-act" }, holdText)]
        : [el("span", { class: "ev-type" },
              `${i}. ${shortEventName(e.type, e)}${st.divergence ? " ⤴" : ""}`),
           el("span", { class: "ev-act" },
             [e.activity || "", e.attempt ? ` #${e.attempt}` : ""].join(""))]);
      box.appendChild(chip);
    });
    return box;
  };
  wrap.appendChild(makeSide(bSteps, "baseline"));
  wrap.appendChild(makeSide(cSteps, "candidate"));
}

function renderDiffColumn(sel, step, otherStep, side, w) {
  const box = $(sel);
  box.innerHTML = "";
  if (!step) {
    box.appendChild(el("div", { class: "si-meta" }, "该侧在此对齐步无状态（轨迹分叉）"));
    return;
  }
  const snap = step.snapshot;
  const otherVars = otherStep?.snapshot?.variables || {};
  const held = !!step.hold;
  const diverges = !!step.divergence;

  if (held || diverges) {
    const head = $("#" + (side === "baseline" ? "diffBaseHead" : "diffCandHead"));
    head.appendChild(held
      ? el("span", { class: "hold-tag" },
        (w.diverge_index != null && diffIndex > w.diverge_index)
          ? "⏸ 持留（另一侧续跑中）"
          : "⏸ 保持（另一侧独有事件）")
      : el("span", { class: "div-tag" }, "⤴ 独有事件在此触发"));
  }

  // 阶段 / 终态
  const outcomeOther = otherStep?.snapshot?.outcome;
  const outDiff = snap.outcome !== outcomeOther;
  box.appendChild(el("div", { class: "ds-block" }, [
    el("h4", {}, "阶段 / 终态"),
    el("div", { class: "ds-row2" + (outDiff ? " changed" : "") }, [
      el("span", {}, snap.phase === "run" ? "正常执行" : "补偿中"),
      el("b", { class: outDiff ? "v-bad" : "" },
        snap.outcome ? DIFF_OUTCOME_NAME[snap.outcome] || snap.outcome : "非终态"),
    ]),
  ]));

  // 变量（与另一侧同对齐步比较，差异高亮）
  box.appendChild(el("div", { class: "ds-block" }, [
    el("h4", {}, "变量"),
    ...(Object.keys(snap.variables).length
      ? Object.entries(snap.variables).map(([k, v]) => {
          const otherHas = k in otherVars;
          const changed = otherHas && otherVars[k] !== v;
          return el("div", { class: "ds-row2" + (changed ? " changed" : "") }, [
            el("span", {}, k),
            changed
              ? el("span", {}, [el("span", { class: "v-old" }, String(otherVars[k])), " → ",
                                el("b", { class: "v-new" }, String(v))])
              : el("b", {}, String(v) + (otherHas ? "" : " (新增)")),
          ]);
        })
      : [el("div", { class: "si-meta" }, "（无变量）")]),
  ]));

  // 活动（简表）
  box.appendChild(el("div", { class: "ds-block" }, [
    el("h4", {}, "活动状态"),
    el("div", { class: "ds-rows" }, snap.activities
      .filter(a => a.status !== "pending")
      .map(a => el("div", { class: "ds-row2" }, [
        el("span", {}, `${a.name}（${a.id}）`),
        el("span", {}, [
          el("span", { class: "status-pill st-" + a.status }, STATUS_LABEL[a.status] || a.status),
          a.tries ? el("span", { class: "si-meta", style: "margin-left:6px" }, `#${a.tries}`) : null,
        ]),
      ]))),
  ]));

  // 外部效果
  box.appendChild(el("div", { class: "ds-block" }, [
    el("h4", {}, "外部效果"),
    ...(snap.effects.length
      ? snap.effects.map(e => el("div", { class: "ds-row2" }, [
          el("span", { class: e.undone ? "effect-undone" : "" },
            "⚡ " + e.key + (e.duplicate ? " 重复!" : "")),
          el("span", { class: "si-meta" },
            [e.activity, e.undone ? " · 已撤销" : " · 生效中"].join("")),
        ]))
      : [el("div", { class: "si-meta" }, "（暂无外部效果）")]),
  ]));

  // 不变量
  if (step.violations?.length) {
    box.appendChild(el("div", { class: "ds-block" },
      step.violations.map(v => el("div", { class: "ds-viol" }, "✗ " + v.name + "：" + v.detail))));
  }

  // 分叉复检：在当前对齐节点对该侧改选另一事件（hold 步该侧未推进，不提供）
  const sideSteps = side === "baseline" ? w.baseline_steps : w.candidate_steps;
  if (!held && step.alternatives?.length && diffIndex < sideSteps.length) {
    const nextEv = (side === "baseline" ? bStepAt(w, diffIndex + 1) : cStepAt(w, diffIndex + 1));
    const row = el("div", { class: "ds-block" }, [el("h4", {}, "在该节点改选并续跑（分叉复检）")]);
    step.alternatives.forEach(a => {
      const isNext = nextEv && JSON.stringify(nextEv.event?.sig) === JSON.stringify(a.sig);
      row.appendChild(el("div", { class: "ds-row2" }, [
        el("span", {}, `${shortEventName(a.type, a)}${a.activity ? " · " + a.activity : ""}${a.attempt ? " #" + a.attempt : ""}`),
        el("button", {
          class: "ds-forkbtn",
          onclick: () => doDiffSideFork(side, step, a),
        }, isNext ? "（当前选择）" : "改选复检 ►"),
      ]));
    });
    box.appendChild(row);
  }
}

function bStepAt(w, i) { return w.baseline_steps[i] || null; }
function cStepAt(w, i) { return w.candidate_steps[i] || null; }

function renderDiffChangeAt(bSteps, cSteps) {
  const box = $("#diffChangeBox");
  box.innerHTML = "";
  const sb = bSteps[diffIndex]?.snapshot;
  const sc = cSteps[diffIndex]?.snapshot;
  if (!sb || !sc) return;
  const items = [];
  if (sb.outcome !== sc.outcome) {
    items.push(["terminal",
      `终态：基线「${DIFF_OUTCOME_NAME[sb.outcome] || "非终态"}」 ⇄ 候选「${DIFF_OUTCOME_NAME[sc.outcome] || "非终态"}」`]);
  }
  for (const k of new Set([...Object.keys(sb.variables), ...Object.keys(sc.variables)])) {
    const vb = sb.variables[k], vc = sc.variables[k];
    if (vb !== vc) items.push(["variables", `变量 ${k}：${vb ?? "—"} → ${vc ?? "—"}`]);
  }
  const eb = effectCountMap(sb), ec = effectCountMap(sc);
  for (const key of new Set([...Object.keys(eb), ...Object.keys(ec)])) {
    const x = eb[key] || { live: 0, undone: 0 }, y = ec[key] || { live: 0, undone: 0 };
    if (x.live !== y.live || x.undone !== y.undone) {
      items.push(["effects", `外部效果「${key}」生效中 ${x.live}→${y.live}，已撤销 ${x.undone}→${y.undone}`]);
    }
  }
  const vb2 = new Set((bSteps[diffIndex].violations || []).map(v => v.id));
  const vc2 = new Set((cSteps[diffIndex].violations || []).map(v => v.id));
  const vmap = {};
  (bSteps[diffIndex].violations || []).forEach(v => { vmap[v.id] = v.name; });
  (cSteps[diffIndex].violations || []).forEach(v => { vmap[v.id] = v.name; });
  for (const id of new Set([...vb2, ...vc2])) {
    if (vb2.has(id) !== vc2.has(id)) {
      items.push(["invariant",
        `不变量「${vmap[id]}」：基线${vb2.has(id) ? "被破坏" : "成立"} ⇄ 候选${vc2.has(id) ? "被破坏" : "成立"}`]);
    }
  }
  const w = diffWitness;
  if (w.diverge_index != null && diffIndex === w.diverge_index) {
    items.push(["divergence",
      w.kind === "divergence" ? w.detail
        : `${w.side === "baseline" ? "基线" : "候选"}独有事件「${w.event.label}」在此触发，轨迹分叉`]);
  }
  if (!items.length) {
    box.appendChild(el("div", { class: "si-meta" }, "该对齐步两侧状态一致。"));
    return;
  }
  const list = el("div", { class: "diff-change-list" });
  items.forEach(([k, text]) => list.appendChild(el("div", { class: "diff-change-item" }, [
    el("span", { class: "dc-tag " + k }, DIFF_KIND_META[k]?.[0] || k),
    el("span", {}, text),
  ])));
  box.appendChild(list);
}

function effectCountMap(snap) {
  const m = {};
  for (const e of snap.effects) {
    const r = m[e.key] || (m[e.key] = { live: 0, undone: 0 });
    if (e.undone) r.undone++; else r.live++;
  }
  return m;
}

// -- 从差异节点分叉复检 ----------------------------------------------------

async function doDiffFork(prefix, sig) {
  // 共享回放区在 diff 模式下的「再分叉」：沿当前复检轨迹前缀，在同一侧继续改选
  const cur = diffForkResult;
  if (!cur) {
    toast("请在双列见证中对具体一侧发起分叉复检", "");
    return;
  }
  const side = cur.diff_side;
  try {
    const result = await api("/api/diff/fork", {
      method: "POST",
      body: {
        side, prefix, sig, bound: cur.bound,
        diff_run_id: cur.diff_context?.diff_run_id || (diffRunRecord?.id) || null,
      },
    });
    diffForkResult = result;
    renderDiffForkResult(result, side, diffSideNames(), result.chosen_event || { sig });
    setupReplay(result.counterexample.steps);
    replayIndex = prefix.length + 1;
    renderReplay();
  } catch (ex) {
    toast("分叉复检失败：" + ex.message, "error");
  }
}

async function doDiffSideFork(side, step, alt) {
  const w = diffWitness;
  const steps = side === "baseline" ? w.baseline_steps : w.candidate_steps;
  // 该侧在当前对齐步（含）之前的真实事件前缀（去掉 hold 步）
  const prefix = steps.slice(1, diffIndex + 1)
    .filter(s => s.event && !s.hold).map(s => s.event);
  const names = diffSideNames();
  try {
    const body = {
      side, prefix, sig: alt.sig, bound: diffResult.bound,
      diff_run_id: diffResult.run_id || null,
    };
    if (!body.diff_run_id && diffRunRecord) body.diff_run_id = diffRunRecord.id;
    const result = await api("/api/diff/fork", { method: "POST", body });
    diffForkResult = result;
    $("#diffForkResult").classList.remove("hidden");
    renderDiffForkResult(result, side, names, alt);
    // 用共享回放组件展示该侧的复检轨迹
    appMode = "diff";
    setupReplay(result.counterexample.steps);
    replayIndex = diffIndex + 1;
    renderReplay();
    $("#replayWrap").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (ex) {
    toast("分叉复检失败：" + ex.message, "error");
  }
}

function renderDiffForkResult(result, side, names, alt) {
  const box = $("#diffForkResult");
  box.innerHTML = "";
  const sideZh = side === "baseline" ? "基线" : "候选";
  const modelName = side === "baseline" ? names.base : names.cand;
  box.appendChild(el("h3", {}, `④ 从差异节点分叉复检（${sideZh}：${modelName}）`));
  const badgeCls = result.verdict;
  box.appendChild(el("div", { class: "verdict-box " + badgeCls, style: "margin:8px 0" }, [
    el("div", { class: "verdict-row" }, [
      el("span", { class: "badge " + badgeCls },
        { violated: "✗ 复检发现反例", exhausted: "✓ 复检边界内未违反",
          bound_reached: "⚠ 复检到达边界" }[result.verdict] || result.verdict),
      el("span", { class: "verdict-text" },
        `在差异节点（第 ${result.fork_point} 步后）对${sideZh}改选「
        ${shortEventName(alt.type, alt)}${alt.activity ? " · " + alt.activity : ""}
        ${alt.attempt ? " #" + alt.attempt : ""}」，沿用同一有界 BFS 语义续跑。
        ${result.verdict_text}`),
    ]),
  ]));
  box.appendChild(el("div", { class: "hint" },
    "下方为该侧复检的单模型轨迹（与有界检验完全相同的回放/再分叉能力）；点击「返回双列见证」可继续查看版本对比。"));
  box.appendChild(el("button", {
    class: "btn btn-small", style: "margin-top:8px",
    onclick: () => {
      diffForkResult = null;
      $("#diffForkResult").classList.add("hidden");
      $("#replayWrap").classList.add("hidden");
      if (diffWitness) { $("#diffReplayWrap").classList.remove("hidden"); renderDiffReplay(); }
    },
  }, "◀ 返回双列见证"));
}

// -- 历史记录 -------------------------------------------------------------

async function loadDiffRuns() {
  const runs = await api("/api/diff/runs");
  const box = $("#diffRunHistory");
  box.innerHTML = "";
  if (!runs.length) {
    box.appendChild(el("div", { class: "si-meta" }, "暂无记录"));
    return;
  }
  runs.forEach(r => {
    box.appendChild(el("div", {
      class: "run-row",
      onclick: () => openDiffRun(r.id),
    }, [
      el("span", { class: "mini-badge " + r.verdict },
        { diff_found: "差异", diff_found_incomplete: "差异·截断",
          inconclusive: "未完备", identical: "一致" }[r.verdict] || r.verdict),
      el("span", {}, `${r.baseline_name} ⇄ ${r.candidate_name}`),
      el("span", { class: "si-meta" }, `边界 ${r.bound}`),
      el("span", { class: "run-time" }, new Date(r.created_at * 1000).toLocaleTimeString()),
    ]));
  });
}

async function openDiffRun(rid) {
  const full = await api(`/api/diff/runs/${rid}`);
  diffResult = full.result;
  diffResult.run_id = full.id;
  diffRunRecord = full;
  diffForkResult = null;
  $("#diffForkResult").classList.add("hidden");
  $("#replayWrap").classList.add("hidden");
  $("#diffBoundInput").value = full.bound;
  populateDiffModelSelects();
  // 即使两侧模型已被删除，也在下拉框中以快照项标注（复检只依赖记录内快照）
  const fillFromRecord = (selId, id, name) => {
    const sel = $(selId);
    if (id && !sel.querySelector(`option[value="${id}"]`)) {
      sel.appendChild(el("option", { value: id, selected: true }, `${name}（快照·模型已删除）`));
    } else if (id) {
      sel.value = id;
    }
  };
  fillFromRecord("#diffBaseSelect", full.baseline_id, full.baseline_name);
  fillFromRecord("#diffCandSelect", full.candidate_id, full.candidate_name);
  renderDiffReport(diffResult);
  toast(`已打开对比记录：${full.baseline_name} ⇄ ${full.candidate_name}（含模型快照）`, "ok");
}

// ---------------------------------------------------------------------------
// 事件绑定与启动
// ---------------------------------------------------------------------------

function switchAppMode(mode) {
  appMode = mode;
  const isRisk = mode === "risk";
  const isConc = mode === "conc";
  const isDiff = mode === "diff";
  $("#modeCheckBtn").classList.toggle("active", mode === "check");
  $("#modeConcBtn").classList.toggle("active", isConc);
  $("#modeRiskBtn").classList.toggle("active", isRisk);
  $("#modeDiffBtn").classList.toggle("active", isDiff);
  $("#checkView").classList.toggle("hidden", mode !== "check");
  $("#concView").classList.toggle("hidden", !isConc);
  $("#riskView").classList.remove("hidden");
  $("#riskView").classList.toggle("hidden", !isRisk);
  $("#diffView").classList.toggle("hidden", !isDiff);
  $("#modeTitle").textContent =
    isRisk ? "随机故障风险分析"
      : isConc ? "并发调度安全分析与调度回放"
      : isDiff ? "版本行为对比（基线 ⇄ 候选）"
      : "检验与轨迹回放";
  viewMode = "original";
  forkCompare = null;
  $("#forkResult").classList.add("hidden");
  if (isDiff) {
    // 版本对比使用专用双列回放；共享回放区仅在差异节点分叉复检时出现
    $("#replayWrap").classList.toggle("hidden", !diffForkResult);
    populateDiffModelSelects();
  } else {
    $("#diffForkResult").classList.add("hidden");
    diffForkResult = null;
    // 共享回放区只在对应模式且确有轨迹时保留
    if (isRisk) {
      $("#replayWrap").classList.toggle("hidden", !riskReplaySteps);
      renderRiskConfig();
    } else if (isConc) {
      $("#replayWrap").classList.toggle("hidden", !concReplaySteps);
    } else {
      $("#replayWrap").classList.toggle("hidden",
        !(checkResult && checkResult.counterexample && checkResult.counterexample.steps.length));
    }
  }
}

function bind() {
  $("#btnAddActivity").addEventListener("click", () => {
    let n = model.spec.activities.length;
    let id;
    do { id = "n" + ++n; } while (model.spec.activities.some(a => a.id === id));
    model.spec.activities.push({
      id, name: "新活动", pre: "", assign: [], effect: "", idempotent: false,
      can_fail: true, retryable: true, max_attempts: 3,
      pre_crash: false, post_crash: false,
      comp_can_fail: true, comp_retryable: true, comp_max_attempts: 3,
      fork_group: "", join_group: "", locks: [], reads: [], writes: [],
    });
    if (!model.spec.start) model.spec.start = id;
    renderAll();
  });
  $("#btnAddEdge").addEventListener("click", () => {
    const ids = model.spec.activities.map(a => a.id);
    if (ids.length < 1) return toast("请先创建活动");
    model.spec.edges.push({ src: ids[0], dst: ids[ids.length - 1] || ids[0], guard: "" });
    renderAll();
  });
  $("#btnAddVar").addEventListener("click", () => {
    let n = model.spec.variables.length, name;
    do { name = "v" + ++n; } while (model.spec.variables.some(v => v.name === name));
    model.spec.variables.push({ name, type: "int", initial: 0 });
    renderAll();
  });
  $("#btnAddInv").addEventListener("click", () => {
    model.spec.invariants.custom.push({ name: "新不变量", expr: "true" });
    renderInvariants();
  });

  $("#startSelect").addEventListener("change", (e) => {
    model.spec.start = e.target.value;
    drawGraph();
  });
  $("#invOnce").addEventListener("change", (e) => { model.spec.invariants.at_most_once = e.target.checked; });
  $("#invUndo").addEventListener("change", (e) => { model.spec.invariants.undo_on_failure = e.target.checked; });

  $$(".tab").forEach(t => t.addEventListener("click", () => {
    if (t.dataset.tab === "json") {
      // 切到 JSON 前同步
      $("#jsonEditor").value = JSON.stringify(model.spec, null, 2);
    }
    $$(".tab").forEach(x => x.classList.toggle("active", x === t));
    $$(".tab-body").forEach(b => b.classList.add("hidden"));
    $("#tab-" + t.dataset.tab).classList.remove("hidden");
    if (t.dataset.tab === "graph") drawGraph();
  }));

  $("#jsonEditor").addEventListener("blur", () => {
    try {
      model.spec = withDefaults(JSON.parse($("#jsonEditor").value));
      renderAll();
      toast("JSON 已解析", "ok");
    } catch (ex) {
      toast("JSON 解析失败：" + ex.message, "error");
    }
  });

  $("#btnValidate").addEventListener("click", () => doValidate());
  $("#btnCheck").addEventListener("click", doCheck);
  $("#btnRiskRun").addEventListener("click", doRiskRun);
  $("#btnRiskUniform").addEventListener("click", () => setRiskPreset("uniform"));
  $("#btnRiskDeterministic").addEventListener("click", () => setRiskPreset("deterministic"));
  $("#modeCheckBtn").addEventListener("click", () => switchAppMode("check"));
  $("#modeConcBtn").addEventListener("click", () => switchAppMode("conc"));
  $("#modeRiskBtn").addEventListener("click", () => switchAppMode("risk"));
  $("#modeDiffBtn").addEventListener("click", () => switchAppMode("diff"));
  $("#btnDiffRun").addEventListener("click", doDiffRun);
  $("#btnDiffBack").addEventListener("click", () => {
    if (diffResult) { hideDiffReplay(); renderDiffReport(diffResult); }
  });
  $("#btnDiffFirst").addEventListener("click", () => { diffIndex = 0; renderDiffReplay(); });
  $("#btnDiffPrev").addEventListener("click", () => { diffIndex--; renderDiffReplay(); });
  $("#btnDiffNext").addEventListener("click", () => { diffIndex++; renderDiffReplay(); });
  $("#btnDiffLast").addEventListener("click", () => {
    const w = diffWitness;
    diffIndex = Math.max(w.baseline_steps.length, w.candidate_steps.length) - 1;
    renderDiffReplay();
  });
  $("#diffStepSlider").addEventListener("input", (e) => {
    diffIndex = +e.target.value; renderDiffReplay();
  });
  $("#btnConcRun").addEventListener("click", doConcRun);
  $("#btnConcBack").addEventListener("click", () => {
    hideConcReplay();
    if (concResult) { $("#concReport").classList.remove("hidden"); renderConcReport(concResult); }
  });
  $("#btnAddConcGroup").addEventListener("click", () => {
    const conc = concConfig();
    if (model.spec.activities.length < 2) return toast("汇合组至少需要 fork 与 join 两个活动");
    let n = conc.groups.length;
    let gid;
    do { gid = "g" + ++n; } while (conc.groups.some(g => g.id === gid));
    conc.groups.push({ id: gid, fork: model.spec.activities[0].id,
                       join: model.spec.activities[model.spec.activities.length - 1].id });
    renderAll();
  });
  $("#btnAddResource").addEventListener("click", () => {
    const conc = concConfig();
    let n = conc.resources.length;
    let name;
    do { name = "resource" + ++n; } while (conc.resources.some(r => r.name === name));
    conc.resources.push({ name });
    renderAll();
  });
  $("#btnAddLock").addEventListener("click", () => {
    const conc = concConfig();
    let n = conc.locks.length;
    let name;
    do { name = "lock" + ++n; } while (conc.locks.some(l => l.name === name));
    conc.locks.push({ name });
    renderAll();
  });
  $("#btnRiskBack").addEventListener("click", () => {
    hideRiskReplay();
    if (riskResult) $("#riskReport").classList.remove("hidden");
  });
  $$(".risk-table th[data-sort]").forEach(th => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (riskSort.key === key) riskSort.desc = !riskSort.desc;
    else riskSort = { key, desc: key === "probability" || key === "length" };
    if (riskResult) renderRiskTraces(riskResult.top_traces || []);
  }));
  $("#btnSave").addEventListener("click", saveModel);
  $("#btnNew").addEventListener("click", () => {
    model = structuredClone(EMPTY_MODEL);
    currentModelId = null;
    riskConfig = {};
    riskResult = null;
    riskReplaySteps = null;
    replayContext = null;
    concResult = null;
    concReplaySteps = null;
    concActiveKind = null;
    $("#modelName").value = model.name;
    $("#modelSelect").value = "";
    $("#verdictBox").classList.add("hidden");
    $("#replayWrap").classList.add("hidden");
    $("#forkResult").classList.add("hidden");
    $("#riskReport").classList.add("hidden");
    $("#riskReplayBar").classList.add("hidden");
    $("#concReport").classList.add("hidden");
    $("#concReplayBar").classList.add("hidden");
    $("#concValidationBox").innerHTML = "";
    $("#riskValidationBox").innerHTML = "";
    $("#diffReport").classList.add("hidden");
    $("#diffReplayWrap").classList.add("hidden");
    $("#diffReplayBar").classList.add("hidden");
    $("#diffForkResult").classList.add("hidden");
    $("#diffValidationBox").innerHTML = "";
    switchAppMode("check");
    renderAll();
  });
  $("#btnExample").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#exampleMenu").classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#btnExample") && !e.target.closest("#exampleMenu")) {
      $("#exampleMenu").classList.add("hidden");
    }
  });
  $("#modelSelect").addEventListener("change", async (e) => {
    if (!e.target.value) return;
    const m = await api("/api/models/" + e.target.value);
    openModel(m);
  });

  // 回放控制
  $("#btnFirst").addEventListener("click", () => { replayIndex = 0; renderReplay(); });
  $("#btnPrev").addEventListener("click", () => { replayIndex--; renderReplay(); });
  $("#btnNext").addEventListener("click", () => { replayIndex++; renderReplay(); });
  $("#btnLast").addEventListener("click", () => {
    replayIndex = activeSteps().length - 1; renderReplay();
  });
  $("#stepSlider").addEventListener("input", (e) => {
    replayIndex = +e.target.value; renderReplay();
  });
  $("#autoPlay").addEventListener("change", (e) => {
    clearInterval(autoTimer);
    if (e.target.checked) {
      autoTimer = setInterval(() => {
        if (replayIndex >= activeSteps().length - 1) {
          replayIndex = 0;
        } else replayIndex++;
        renderReplay();
      }, 1100);
    }
  });

  window.addEventListener("resize", drawGraph);
}

bind();
renderAll();
loadExamplesMenu();
loadModels().catch(() => toast("无法连接后端 API"));
