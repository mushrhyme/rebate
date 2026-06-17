"""자연어 → DSL 승인 게이트 API (P3 UI 연동).

CLI 프로토타입(scripts/dsl_apply.py)을 백엔드 라우트로 노출한다. 현업이 채팅/UI에서
자연어 규칙을 입력 → /preview(컴파일+게이트+승인요약) → /apply(동결)로 진행.

핵심 안전장치(CLI와 동일):
  - 동결 대상은 컴파일된 설정(자연어 아님 — 드리프트 방지)
  - 게이트 통과 + (게이트 비검증 필드 변경 시) confirm_display 명시해야 동결
  - 백업 → 쓰기 → 사후 재검증(제어문자/스키마) → 실패 시 롤백 + 변경이력 기록
  - /apply는 관리자만

NOTE: 컴파일/게이트 로직은 현재 scripts/에 있어 런타임 import한다(PoC). 운영 승격
시 backend/로 이동 권장.
"""
import datetime as _dt
import json
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.auth import get_current_user, require_admin
from ...core.config import get_settings

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts.compile_dsl_poc import compile_product_aggregate, _grounding  # noqa: E402
from scripts import validate_dsl_gate as gate                              # noqa: E402

router = APIRouter(prefix="/api/v3/dsl", tags=["dsl"])

_GATE_VALIDATED = {"base_condition", "qty_field", "amount_field"}


class PreviewBody(BaseModel):
    form_id: str
    doc_id: str
    rule: str


class ApplyBody(BaseModel):
    form_id: str
    doc_id: str
    rule: str
    config: dict
    confirm_display: bool = False


def _items(doc_id: str) -> list:
    p = get_settings().extracted_dir / doc_id / "phase3_output.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"phase3_output 없음: {doc_id}")
    return json.loads(p.read_text(encoding="utf-8")).get("items", [])


def _cfg_path() -> Path:
    return get_settings().workspace_root / "config" / "form_types.json"


def _form_cfg(form_id: str) -> tuple[dict, dict]:
    cfg = json.loads(_cfg_path().read_text(encoding="utf-8"))
    if form_id not in cfg:
        raise HTTPException(status_code=404, detail=f"form_types.json에 {form_id} 없음")
    return cfg, cfg[form_id]


def _diff(cur: dict, new: dict) -> list[dict]:
    out = []
    for k in sorted(set(cur) | set(new)):
        a, b = cur.get(k), new.get(k)
        if a != b:
            out.append({"field": k, "from": a, "to": b, "validated": k in _GATE_VALIDATED})
    return out


def _run_gates(form_id: str, form_cfg_proposed: dict, items: list):
    gates, out = [], None
    ok1, m1 = gate.gate_schema(form_id);                          gates.append({"name": "스키마+정규화", "ok": ok1, "msg": m1})
    ok2, m2, out = gate.gate_dryrun(form_cfg_proposed, items);    gates.append({"name": "dry-run+필드", "ok": ok2, "msg": m2})
    allok = ok1 and ok2
    if ok2:
        ok3, m3 = gate.gate_invariants(form_cfg_proposed, items, out); gates.append({"name": "불변식", "ok": ok3, "msg": m3})
        allok = allok and ok3
    return allok, gates, out


@router.post("/preview")
async def preview(body: PreviewBody, user: dict = Depends(get_current_user)):
    settings = get_settings()
    items = _items(body.doc_id)
    cols_l, conds_l = _grounding(items)
    config, reasoning = compile_product_aggregate(
        body.rule, cols_l, conds_l, api_key=settings.anthropic_api_key
    )
    _, form_cfg = _form_cfg(body.form_id)
    cur_pa = form_cfg.get("product_aggregate", {})
    proposed_pa = {**cur_pa, **config}
    proposed = {**form_cfg, "product_aggregate": proposed_pa}

    allok, gates, out = _run_gates(body.form_id, proposed, items)
    sample = []
    if out:
        for g in out.get("groups", [])[:3]:
            sample.append({
                "jisho": g.get("jisho"), "product": g.get("product_name"),
                "rows": [{"qty": r["qty"], "amount": r["amount"]} for r in g["rows"]],
                "total_amount": g.get("total_amount"),
            })
    diff = _diff(cur_pa, proposed_pa)
    return {
        "config": config, "reasoning": reasoning, "proposed": proposed_pa,
        "gates": gates, "allok": allok, "diff": diff,
        "has_review": any(not d["validated"] for d in diff),
        "sample": sample,
        "grounding": {"columns": cols_l, "conditions": conds_l},
    }


@router.post("/apply")
async def apply(body: ApplyBody, user: dict = Depends(require_admin)):
    settings = get_settings()
    cfg_path = _cfg_path()
    items = _items(body.doc_id)
    cfg_all, form_cfg = _form_cfg(body.form_id)
    cur_pa = form_cfg.get("product_aggregate", {})
    proposed_pa = {**cur_pa, **body.config}
    proposed = {**form_cfg, "product_aggregate": proposed_pa}

    allok, gates, _ = _run_gates(body.form_id, proposed, items)
    if not allok:
        raise HTTPException(status_code=400, detail={"msg": "게이트 미통과 — 동결 거부", "gates": gates})
    diff = _diff(cur_pa, proposed_pa)
    if any(not d["validated"] for d in diff) and not body.confirm_display:
        raise HTTPException(status_code=400, detail="게이트 비검증(표시) 필드 변경 — confirm_display 필요")

    ts = _dt.datetime.now().isoformat(timespec="seconds")
    bdir = settings.workspace_root / "config" / ".form_types_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"form_types.{ts.replace(':', '')}.json"
    backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")

    cfg_all[body.form_id]["product_aggregate"] = proposed_pa
    cfg_path.write_text(json.dumps(cfg_all, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ok_post, m_post = gate.gate_schema(body.form_id)
    if not ok_post:
        cfg_path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        raise HTTPException(status_code=500, detail=f"사후 검증 실패 — 롤백함: {m_post}")

    changelog = settings.workspace_root / "config" / "form_types_changelog.jsonl"
    entry = {
        "ts": ts, "actor": user.get("username"), "form": body.form_id,
        "field": "product_aggregate", "rule": body.rule, "compiled": body.config,
        "frozen": proposed_pa, "gate": "passed",
        "backup": str(backup.relative_to(settings.workspace_root)),
    }
    with changelog.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"ok": True, "backup": backup.name, "frozen": proposed_pa,
            "message": "동결 완료 — 해당 문서 재분석 시 적용됩니다."}
