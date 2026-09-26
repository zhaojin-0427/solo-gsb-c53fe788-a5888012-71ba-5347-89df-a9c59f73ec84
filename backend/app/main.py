"""FastAPI 入口：模型 CRUD、校验、检验、分叉，以及前端静态文件。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import checker, concurrency, db, diff, examples, risk
from .schemas import (CheckIn, ConcCheckIn, ConcForkIn, ConcReplayIn, DiffForkIn,
                      DiffIn, ForkIn,
                      ModelIn, ModelPatch, RiskAnalyzeIn, RiskForkIn,
                      RiskReplayIn, RiskValidateIn, ValidateIn)
from .workflow import WorkflowError, compile_workflow

FRONTEND_DIR = os.environ.get("CW_FRONTEND_DIR", "/app/frontend")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="补偿工作流模型检验台", version="1.0.0", lifespan=lifespan)


def _compile_or_400(spec: dict):
    try:
        return compile_workflow(spec)
    except WorkflowError as ex:
        raise HTTPException(status_code=400, detail={"errors": str(ex).split("；")})
    except Exception as ex:  # 表达式层其他错误
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})


def _get_model_or_404(mid: str) -> dict:
    m = db.get_model(mid)
    if m is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    return m


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/examples")
def get_examples() -> list[dict]:
    return examples.EXAMPLES


@app.post("/api/validate")
def validate(body: ValidateIn) -> dict:
    spec = body.spec.model_dump()
    wf = _compile_or_400(spec)
    return {"ok": True, "activity_count": len(wf.activities),
            "edge_count": len(wf.edges)}


@app.get("/api/models")
def models_get() -> list[dict]:
    return db.list_models()


@app.post("/api/models", status_code=201)
def models_create(body: ModelIn) -> dict:
    spec = body.spec.model_dump()
    _compile_or_400(spec)
    return db.create_model(body.name, spec)


@app.get("/api/models/{mid}")
def model_get(mid: str) -> dict:
    return _get_model_or_404(mid)


@app.put("/api/models/{mid}")
def model_update(mid: str, body: ModelPatch) -> dict:
    _get_model_or_404(mid)
    name = body.name
    spec = body.spec.model_dump() if body.spec is not None else None
    if spec is not None:
        _compile_or_400(spec)
    if spec is None:
        raise HTTPException(status_code=400, detail={"errors": ["spec 不能为空"]})
    updated = db.update_model(mid, name, spec)
    return updated


@app.delete("/api/models/{mid}", status_code=204)
def model_delete(mid: str):
    if not db.delete_model(mid):
        raise HTTPException(status_code=404, detail="模型不存在")
    return JSONResponse(status_code=204, content=None)


def _initial_vars(spec: dict) -> dict[str, Any]:
    out = {}
    for v in spec.get("variables", []):
        if v.get("initial") is not None:
            out[v["name"]] = v["initial"]
    return out


@app.post("/api/models/{mid}/check")
def model_check(mid: str, body: CheckIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    try:
        result = checker.run_check(wf, bound=body.bound,
                                   init_vars=_initial_vars(model["spec"]))
    except checker.CheckError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})
    if body.persist:
        record = db.create_run(mid, body.bound, result)
        result["run_id"] = record["id"]
    return result


@app.post("/api/models/{mid}/fork")
def model_fork(mid: str, body: ForkIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    try:
        result = checker.run_fork(wf, body.prefix, body.sig, bound=body.bound)
    except checker.CheckError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})
    if body.persist:
        record = db.create_run(mid, body.bound, result)
        result["run_id"] = record["id"]
    return result


@app.get("/api/models/{mid}/runs")
def model_runs(mid: str) -> list[dict]:
    _get_model_or_404(mid)
    return db.list_runs(mid)


@app.get("/api/runs/{rid}")
def run_get(rid: str) -> dict:
    rec = db.get_run(rid)
    if rec is None:
        raise HTTPException(status_code=404, detail="检验记录不存在")
    return rec


# ---------------------------------------------------------------------------
# 随机故障风险分析（加性能力；不改变既有检验语义与 API）
# ---------------------------------------------------------------------------

def _risk_payloads(body) -> list[dict]:
    return [a.model_dump() for a in body.activities]


@app.post("/api/risk/validate")
def risk_validate(body: RiskValidateIn) -> dict:
    spec = body.spec.model_dump()
    wf = _compile_or_400(spec)
    errors, _cfg, branches = risk.validate_params(wf, _risk_payloads(body))
    return {"ok": not errors, "errors": errors, "branches": branches}


@app.post("/api/models/{mid}/risk")
def risk_analyze(mid: str, body: RiskAnalyzeIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    try:
        result = risk.run_risk_analysis(
            wf, _risk_payloads(body), bound=body.bound, top_n=body.top_n,
            init_vars=_initial_vars(model["spec"]))
    except risk.RiskError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})
    # 概率校验错误是结果的一部分（明确标注），不产生 4xx；仍可按需持久化
    if body.persist and result.get("verdict") == "risk_ok":
        record = db.create_risk_run(mid, result["bound"], result["top_n"],
                                    result["params"], result)
        result["run_id"] = record["id"]
    return result


@app.post("/api/models/{mid}/risk/replay")
def risk_replay(mid: str, body: RiskReplayIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    try:
        return risk.replay_trace(wf, body.events, _initial_vars(model["spec"]))
    except checker.CheckError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})


@app.post("/api/models/{mid}/risk/fork")
def risk_fork(mid: str, body: RiskForkIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    try:
        result = checker.run_fork(wf, body.prefix, body.sig, bound=body.bound)
    except checker.CheckError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})
    if body.persist:
        record = db.create_run(mid, body.bound, result)
        result["run_id"] = record["id"]
    return result


@app.get("/api/models/{mid}/risk/runs")
def risk_runs(mid: str) -> list[dict]:
    _get_model_or_404(mid)
    return db.list_risk_runs(mid)


@app.get("/api/risk/runs/{rid}")
def risk_run_get(rid: str) -> dict:
    rec = db.get_risk_run(rid)
    if rec is None:
        raise HTTPException(status_code=404, detail="风险分析记录不存在")
    return rec


# ---------------------------------------------------------------------------
# 并发调度安全分析（加性能力；不改变既有检验/风险 API 与串行语义）
# ---------------------------------------------------------------------------

@app.post("/api/models/{mid}/concurrency")
def concurrency_check(mid: str, body: ConcCheckIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    if not wf.concurrent:
        raise HTTPException(status_code=400, detail={
            "errors": ["当前模型未声明 fork/join 汇合组，请先在「并发调度」标签中配置"]})
    try:
        result = concurrency.run_check(
            wf, bound=body.bound, init_vars=_initial_vars(model["spec"]))
    except concurrency.ConcError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})
    if body.persist:
        record = db.create_conc_run(mid, body.bound, result)
        result["run_id"] = record["id"]
    return result


@app.post("/api/models/{mid}/concurrency/replay")
def concurrency_replay(mid: str, body: ConcReplayIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    try:
        return concurrency.replay(wf, body.events, _initial_vars(model["spec"]))
    except concurrency.ConcError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})


@app.post("/api/models/{mid}/concurrency/fork")
def concurrency_fork(mid: str, body: ConcForkIn) -> dict:
    model = _get_model_or_404(mid)
    wf = _compile_or_400(model["spec"])
    try:
        result = concurrency.run_fork(
            wf, body.prefix, body.sig, bound=body.bound,
            init_vars=_initial_vars(model["spec"]))
    except concurrency.ConcError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})
    if body.persist:
        record = db.create_conc_run(mid, body.bound, result)
        result["run_id"] = record["id"]
    return result


@app.get("/api/models/{mid}/concurrency/runs")
def concurrency_runs(mid: str) -> list[dict]:
    _get_model_or_404(mid)
    return db.list_conc_runs(mid)


@app.get("/api/concurrency/runs/{rid}")
def concurrency_run_get(rid: str) -> dict:
    rec = db.get_conc_run(rid)
    if rec is None:
        raise HTTPException(status_code=404, detail="并发分析记录不存在")
    return rec


# ---------------------------------------------------------------------------
# 版本行为对比（加性能力；不改变既有检验/风险/并发 API 与串行语义）
# ---------------------------------------------------------------------------

@app.post("/api/diff")
def diff_run(body: DiffIn) -> dict:
    if body.baseline_id == body.candidate_id:
        raise HTTPException(status_code=400,
                            detail={"errors": ["基线模型与候选模型不能是同一个模型"]})
    base_model = _get_model_or_404(body.baseline_id)
    cand_model = _get_model_or_404(body.candidate_id)
    wf_b = _compile_or_400(base_model["spec"])
    wf_c = _compile_or_400(cand_model["spec"])
    result = diff.run_diff(
        wf_b, wf_c, bound=body.bound,
        init_vars_b=_initial_vars(base_model["spec"]),
        init_vars_c=_initial_vars(cand_model["spec"]))
    params = {"baseline_id": body.baseline_id, "candidate_id": body.candidate_id,
              "baseline_name": base_model["name"], "candidate_name": cand_model["name"],
              "bound": body.bound}
    if body.persist:
        record = db.create_diff_run(
            body.baseline_id, body.candidate_id, base_model["name"], cand_model["name"],
            body.bound, params, base_model["spec"], cand_model["spec"], result)
        result["run_id"] = record["id"]
    return result


def _resolve_diff_sides(body: DiffForkIn) -> tuple[dict, dict, str | None, str | None]:
    """解析分叉复检两侧模型（优先持久化记录中的快照，其次显式快照，最后按 id 读取）。"""
    base_spec = cand_spec = None
    base_id = cand_id = None
    if body.diff_run_id:
        rec = db.get_diff_run(body.diff_run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="版本对比记录不存在")
        base_spec, cand_spec = rec["baseline_spec"], rec["candidate_spec"]
        base_id, cand_id = rec["baseline_id"], rec["candidate_id"]
    if body.baseline_spec is not None:
        base_spec = body.baseline_spec.model_dump()
        base_id = body.baseline_id
    if body.candidate_spec is not None:
        cand_spec = body.candidate_spec.model_dump()
        cand_id = body.candidate_id
    if base_spec is None and body.baseline_id:
        m = _get_model_or_404(body.baseline_id)
        base_spec, base_id = m["spec"], m["id"]
    if cand_spec is None and body.candidate_id:
        m = _get_model_or_404(body.candidate_id)
        cand_spec, cand_id = m["spec"], m["id"]
    if base_spec is None or cand_spec is None:
        raise HTTPException(status_code=400,
                            detail={"errors": ["缺少对比两侧的模型（需要 diff_run_id 或两侧模型 id/快照）"]})
    return base_spec, cand_spec, base_id, cand_id


@app.post("/api/diff/fork")
def diff_fork(body: DiffForkIn) -> dict:
    if body.side not in ("baseline", "candidate"):
        raise HTTPException(status_code=400, detail={"errors": ["side 必须是 baseline 或 candidate"]})
    base_spec, cand_spec, base_id, cand_id = _resolve_diff_sides(body)
    wf_b = _compile_or_400(base_spec)
    wf_c = _compile_or_400(cand_spec)
    side_wf = wf_b if body.side == "baseline" else wf_c
    side_spec = base_spec if body.side == "baseline" else cand_spec
    try:
        result = checker.run_fork(
            side_wf, body.prefix, body.sig, bound=body.bound,
            init_vars=_initial_vars(side_spec))
    except checker.CheckError as ex:
        raise HTTPException(status_code=400, detail={"errors": [str(ex)]})
    result["diff_side"] = body.side
    result["diff_context"] = {
        "baseline_id": base_id, "candidate_id": cand_id,
        "diff_run_id": body.diff_run_id,
    }
    return result


@app.get("/api/diff/runs")
def diff_runs(model_id: str | None = None) -> list[dict]:
    return db.list_diff_runs(model_id=model_id)


@app.get("/api/diff/runs/{rid}")
def diff_run_get(rid: str) -> dict:
    rec = db.get_diff_run(rid)
    if rec is None:
        raise HTTPException(status_code=404, detail="版本对比记录不存在")
    return rec


# ---------------------------------------------------------------------------
# 前端静态文件（必须放在 API 路由之后）
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    # 任何非 /api 路径都回退到单页入口
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404)
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
