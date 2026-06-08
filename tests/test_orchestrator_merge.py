"""test_orchestrator_merge.py — _merge_confirmed_mappings() 단위 테스트

asyncpg, anthropic 등 외부 의존성이 없는 test 환경에서
orchestrator의 CSV 경로를 검증한다.

sys.modules 선조작으로 asyncpg 등을 mock해 import를 통과시킨다.

실행: pytest tests/test_orchestrator_merge.py -v
"""
import ast
import csv
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 외부 의존 모듈 mock (asyncpg 등이 test 환경에 없음) ──────────────────────
# orchestrator → database → asyncpg 체인을 차단

_ASYNCPG_MOCK = MagicMock()
sys.modules.setdefault("asyncpg", _ASYNCPG_MOCK)
sys.modules.setdefault("asyncpg.pool", _ASYNCPG_MOCK.pool)

# azure / google drive 의존도 차단
for _mod in [
    "azure", "azure.ai", "azure.ai.formrecognizer",
    "google", "google.oauth2", "google.auth",
    "google.auth.transport", "google.auth.transport.requests",
    "googleapiclient", "googleapiclient.discovery",
]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.tools.metrics import reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture
def dirs(tmp_path: Path):
    mappings = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    extracted = tmp_path / "extracted"
    mappings.mkdir()
    form_defs.mkdir()
    extracted.mkdir()
    return tmp_path, mappings, form_defs, extracted


def _make_phase3_json(extracted: Path, doc_id: str, form_id: str = "form_01") -> Path:
    """_merge_confirmed_mappings가 읽는 phase3_output.json을 생성한다."""
    doc_dir = extracted / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    out = doc_dir / "phase3_output.json"
    out.write_text(json.dumps({
        "doc_id": doc_id,
        "form_id": form_id,
        "issuer": {"name": "テスト発行者"},
        "confirmed_retailers": {},
        "confirmed_products": {},
        "items": [],
        "cover_totals": {},
        "hatsu_month": "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _mock_settings(tmp_path, mappings, form_defs, extracted):
    s = MagicMock()
    s.mappings_dir = mappings
    s.form_definitions_dir = form_defs
    s.extracted_dir = extracted
    return s


def _make_db_row(mapping_type, ocr_name, confirmed_code, confirmed_name=""):
    """DB row처럼 동작하는 MagicMock."""
    data = {
        "mapping_type": mapping_type,
        "ocr_name": ocr_name,
        "confirmed_code": confirmed_code,
        "confirmed_name": confirmed_name,
    }
    row = MagicMock()
    row.__getitem__ = lambda self, k: data[k]
    return row


# ── 1. 직접 import 제거 검증 (AST — 실제 import 불필요) ───────────────────────

class TestNoDirektImport:
    def test_upsert_cache_row_not_imported(self):
        """_upsert_cache_row가 orchestrator.py에서 import되지 않는다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
                assert "_upsert_cache_row" not in names, (
                    "_upsert_cache_row가 orchestrator.py에 직접 import됨"
                )

    def test_upsert_dist_cache_row_not_imported(self):
        """_upsert_dist_cache_row가 orchestrator.py에서 import되지 않는다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
                assert "_upsert_dist_cache_row" not in names, (
                    "_upsert_dist_cache_row가 orchestrator.py에 직접 import됨"
                )

    def test_confirm_mapping_import_present_in_source(self):
        """orchestrator.py 소스에 confirm_mapping import가 있다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        assert "confirm_mapping" in src

    def test_direct_upsert_call_absent_in_source(self):
        """orchestrator.py 소스에 _upsert_cache_row() 직접 호출이 없다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        assert "_upsert_cache_row(" not in src, (
            "orchestrator.py에 _upsert_cache_row() 직접 호출이 남아 있음"
        )
        assert "_upsert_dist_cache_row(" not in src, (
            "orchestrator.py에 _upsert_dist_cache_row() 직접 호출이 남아 있음"
        )

    def test_confirm_mapping_called_in_merge_function(self):
        """_merge_confirmed_mappings() 내에서 confirm_mapping()이 호출된다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        merge_fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "_merge_confirmed_mappings"),
            None,
        )
        assert merge_fn is not None
        # 함수 내에서 confirm_mapping 이름이 참조되는지
        fn_names = [
            n.id if isinstance(n, ast.Name) else
            (n.attr if isinstance(n, ast.Attribute) else "")
            for n in ast.walk(merge_fn)
        ]
        assert "confirm_mapping" in fn_names, (
            "_merge_confirmed_mappings() 내에 confirm_mapping 호출 없음"
        )


# ── 2. _merge_confirmed_mappings() 동작 검증 ──────────────────────────────────

class TestMergeConfirmedMappings:
    """_merge_confirmed_mappings()가 confirm_mapping()을 올바르게 호출하는지 검증."""

    async def _run_merge(self, dirs, mock_db_rows):
        """confirm_mapping을 AsyncMock으로 교체해 _merge_confirmed_mappings를 실행.

        반환: confirm_mapping에 전달된 kwargs 목록.
        """
        tmp_path, mappings, form_defs, extracted = dirs
        doc_id = "test_doc_001"
        _make_phase3_json(extracted, doc_id)

        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=mock_db_rows)
        mock_settings = _mock_settings(tmp_path, mappings, form_defs, extracted)
        confirm_calls: list[dict] = []

        async def _fake_confirm(**kwargs):
            confirm_calls.append(kwargs)

        with patch("backend.pipeline.orchestrator.get_pool", return_value=mock_pool), \
             patch("backend.pipeline.orchestrator.get_settings", return_value=mock_settings), \
             patch("backend.pipeline.orchestrator.confirm_mapping",
                   side_effect=_fake_confirm):
            from backend.pipeline.orchestrator import _merge_confirmed_mappings
            await _merge_confirmed_mappings(doc_id)

        return confirm_calls

    async def test_retailer_calls_confirm_mapping(self, dirs):
        """retailer DB 행 → confirm_mapping(mapping_type='retailer') 호출."""
        rows = [_make_db_row("retailer", "テスト店舗", "R001", "テスト店舗名")]
        calls = await self._run_merge(dirs, rows)

        assert len(calls) == 1
        c = calls[0]
        assert c["mapping_type"] == "retailer"
        assert c["ocr_name"] == "テスト店舗"
        assert c["confirmed_code"] == "R001"
        assert c["context"].get("retailer_name") == "テスト店舗名"

    async def test_product_calls_confirm_mapping(self, dirs):
        """product DB 행 → confirm_mapping(mapping_type='product') 호출."""
        rows = [_make_db_row("product", "農心 辛ラーメン", "P001", "辛ラーメン 120g")]
        calls = await self._run_merge(dirs, rows)

        assert len(calls) == 1
        c = calls[0]
        assert c["mapping_type"] == "product"
        assert c["ocr_name"] == "農心 辛ラーメン"
        assert c["confirmed_code"] == "P001"
        assert c["context"].get("product_name") == "辛ラーメン 120g"

    async def test_dist_calls_confirm_mapping_when_retailer_known(self, dirs):
        """dist DB 행은 retailer_code가 확정된 경우 confirm_mapping(dist) 호출."""
        rows = [
            _make_db_row("retailer", "テスト店舗", "R001", "テスト店舗名"),
            _make_db_row("dist",     "テスト店舗", "D001", "東日本担当"),
        ]
        calls = await self._run_merge(dirs, rows)

        dist_calls = [c for c in calls if c["mapping_type"] == "dist"]
        assert len(dist_calls) == 1
        c = dist_calls[0]
        assert c["confirmed_code"] == "D001"
        assert c["context"]["retailer_code"] == "R001"
        assert c["context"].get("dist_name") == "東日本担当"
        assert "form_id" in c["context"]
        assert "issuer_fingerprint" in c["context"]

    async def test_dist_skipped_when_no_retailer(self, dirs):
        """対応する retailer がない dist 行は confirm_mapping を呼ばない。"""
        rows = [_make_db_row("dist", "未知の店舗", "D001", "担当者")]
        calls = await self._run_merge(dirs, rows)
        dist_calls = [c for c in calls if c["mapping_type"] == "dist"]
        assert len(dist_calls) == 0

    async def test_all_three_types_processed(self, dirs):
        """retailer + product + dist 3種すべてが confirm_mapping で処理される。"""
        rows = [
            _make_db_row("retailer", "テスト店舗", "R001", "テスト店"),
            _make_db_row("product",  "農心 辛ラーメン", "P001", "辛ラーメン"),
            _make_db_row("dist",     "テスト店舗", "D001", "東日本"),
        ]
        calls = await self._run_merge(dirs, rows)

        types = {c["mapping_type"] for c in calls}
        assert types == {"retailer", "product", "dist"}
        assert len(calls) == 3

    async def test_empty_code_row_skipped(self, dirs):
        """confirmed_code が空の行は confirm_mapping を呼ばない。"""
        rows = [
            _make_db_row("retailer", "テスト", "", ""),    # code 空
            _make_db_row("retailer", "テスト2", "R002", "テスト2"),
        ]
        calls = await self._run_merge(dirs, rows)
        assert len(calls) == 1
        assert calls[0]["confirmed_code"] == "R002"

    async def test_confirm_mapping_receives_mappings_dir(self, dirs):
        """confirm_mapping에 mappings_dir가 올바른 Path로 전달된다."""
        tmp_path, mappings, form_defs, extracted = dirs
        rows = [_make_db_row("retailer", "テスト店舗", "R001", "テスト")]
        calls = await self._run_merge(dirs, rows)

        assert len(calls) == 1
        assert calls[0]["mappings_dir"] == mappings


# ── 3. 실제 CSV 쓰기 통합 테스트 ──────────────────────────────────────────────

class TestMergeWritesToCsv:
    """실제 confirm_mapping을 사용해 CSV 파일 쓰기를 검증한다."""

    async def _run_real_merge(self, dirs, mock_db_rows):
        """confirm_mapping을 실제 함수로 실행. CSV에 실제로 쓴다."""
        tmp_path, mappings, form_defs, extracted = dirs
        doc_id = "doc_real_test"
        _make_phase3_json(extracted, doc_id)

        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=mock_db_rows)
        mock_settings = _mock_settings(tmp_path, mappings, form_defs, extracted)

        with patch("backend.pipeline.orchestrator.get_pool", return_value=mock_pool), \
             patch("backend.pipeline.orchestrator.get_settings", return_value=mock_settings):
            from backend.pipeline.orchestrator import _merge_confirmed_mappings
            await _merge_confirmed_mappings(doc_id)

    async def test_retailer_written_to_csv(self, dirs):
        """retailer 행이 실제 ocr_retailer.csv에 기록된다."""
        _, mappings, *_ = dirs
        await self._run_real_merge(dirs, [
            _make_db_row("retailer", "テスト店舗", "R999", "テスト店舗名"),
        ])

        rows = list(csv.DictReader((mappings / "ocr_retailer.csv").open(encoding="utf-8-sig")))
        assert len(rows) == 1
        assert rows[0]["ocr_name"] == "テスト店舗"
        assert rows[0]["retailer_code"] == "R999"
        assert rows[0]["retailer_name"] == "テスト店舗名"

    async def test_product_written_to_csv(self, dirs):
        """product 행이 실제 ocr_product.csv에 기록된다."""
        _, mappings, *_ = dirs
        await self._run_real_merge(dirs, [
            _make_db_row("product", "農心 辛ラーメン", "P123", "辛ラーメン 120g"),
        ])

        rows = list(csv.DictReader((mappings / "ocr_product.csv").open(encoding="utf-8-sig")))
        assert len(rows) == 1
        assert rows[0]["product_code"] == "P123"

    async def test_dist_written_to_csv(self, dirs):
        """dist 행이 실제 ocr_dist.csv에 기록된다."""
        _, mappings, *_ = dirs
        await self._run_real_merge(dirs, [
            _make_db_row("retailer", "テスト店舗", "R001", "テスト店"),
            _make_db_row("dist",     "テスト店舗", "D001", "東日本"),
        ])

        rows = list(csv.DictReader((mappings / "ocr_dist.csv").open(encoding="utf-8-sig")))
        assert len(rows) == 1
        assert rows[0]["dist_code"] == "D001"
        assert rows[0]["retailer_code"] == "R001"


# ── 4. resume_phase4 흐름 보존 검증 ─────────────────────────────────────────

class TestResumePhase4Flow:
    """resume_phase4()가 _merge_confirmed_mappings()를 올바르게 호출한다."""

    def test_resume_phase4_calls_merge_in_source(self):
        """resume_phase4() 소스 내에 _merge_confirmed_mappings 호출이 있다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        resume_fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "resume_phase4"),
            None,
        )
        assert resume_fn is not None
        calls_in_fn = [
            n.func.id if isinstance(n.func, ast.Name) else
            (n.func.attr if isinstance(n.func, ast.Attribute) else "")
            for n in ast.walk(resume_fn)
            if isinstance(n, ast.Call)
        ]
        assert "_merge_confirmed_mappings" in calls_in_fn

    async def test_resume_phase4_checks_pending(self):
        """has_pending_mappings가 True이면 ValueError."""
        with patch("backend.pipeline.orchestrator.has_pending_mappings",
                   new=AsyncMock(return_value=True)):
            from backend.pipeline.orchestrator import resume_phase4
            with pytest.raises(ValueError, match="확인되지 않은 매핑"):
                await resume_phase4("doc_id")

    async def test_no_pending_executes_merge_then_phase4(self, dirs):
        """미확인 매핑이 없으면 merge → phase4 순서로 실행된다."""
        from backend.pipeline.orchestrator import resume_phase4
        order: list[str] = []

        with patch("backend.pipeline.orchestrator.has_pending_mappings",
                   new=AsyncMock(return_value=False)), \
             patch("backend.pipeline.orchestrator._merge_confirmed_mappings",
                   new=AsyncMock(side_effect=lambda d: order.append(f"merge:{d}"))), \
             patch("backend.pipeline.orchestrator._run_phase4_and_finish",
                   new=AsyncMock(side_effect=lambda d, **kw: order.append(f"phase4:{d}"))), \
             patch("backend.pipeline.orchestrator.get_current_run_id",
                   new=AsyncMock(return_value="run_001")):
            await resume_phase4("doc_abc")

        assert order == ["merge:doc_abc", "phase4:doc_abc"], (
            f"실행 순서 오류: {order}"
        )
