import asyncpg
from .config import get_settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        # v3_documents 마이그레이션
        await conn.execute(
            """ALTER TABLE v3_documents
               ADD COLUMN IF NOT EXISTS token_usage JSONB NOT NULL DEFAULT '{}'"""
        )
        # users 테이블 — 일본어명·부서·권한 컬럼 추가
        for col_ddl in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name_ja TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS department_ko TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS department_ja TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS category TEXT",
        ]:
            await conn.execute(col_ddl)
        # v3_documents — confirmed_at 컬럼 추가
        await conn.execute(
            "ALTER TABLE v3_documents ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ"
        )
        # v3_reviews 테이블 — 소매처 그룹 단위 1차/2차 검토 체크
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS v3_reviews (
                id          SERIAL PRIMARY KEY,
                doc_id      TEXT        NOT NULL,
                retailer_code TEXT      NOT NULL,
                review_type TEXT        NOT NULL CHECK (review_type IN ('1차', '2차')),
                reviewer_id INTEGER     NOT NULL,
                reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (doc_id, retailer_code, review_type)
            )
            """
        )
        # form_edit_logs 테이블 — form 변경 이력 + 충돌 감지
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS form_edit_logs (
                id             SERIAL PRIMARY KEY,
                form_id        TEXT        NOT NULL,
                user_id        INTEGER     NOT NULL,
                display_name   TEXT        NOT NULL,
                saved_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                content_hash   TEXT        NOT NULL,
                content_before TEXT        NOT NULL,
                content_after  TEXT        NOT NULL
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS form_edit_logs_form_id_idx ON form_edit_logs(form_id, saved_at DESC)"
        )
        # v3_documents — 분석 실행 ID 컬럼 추가 (resume_phase4 연결용)
        await conn.execute(
            "ALTER TABLE v3_documents ADD COLUMN IF NOT EXISTS current_run_id TEXT"
        )
        # v3_documents — 현재 분석 시작 시각 (재분석 시 리셋, 경과시간 표시용)
        await conn.execute(
            "ALTER TABLE v3_documents ADD COLUMN IF NOT EXISTS analysis_started_at TIMESTAMPTZ"
        )
        # v3_usage_log 테이블 — 분석 실행 이력 (재분석 포함)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS v3_usage_log (
                id          BIGSERIAL    PRIMARY KEY,
                doc_id      TEXT         NOT NULL,
                run_id      TEXT         NOT NULL,
                run_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                phase       TEXT         NOT NULL,
                model       TEXT         NOT NULL,
                input_tok   INT          NOT NULL DEFAULT 0,
                output_tok  INT          NOT NULL DEFAULT 0,
                cache_read  INT          NOT NULL DEFAULT 0,
                cache_write INT          NOT NULL DEFAULT 0
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS v3_usage_log_doc_run_idx ON v3_usage_log(doc_id, run_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS v3_usage_log_run_at_idx ON v3_usage_log(run_at DESC)"
        )
        # 잘못 생성된 legacy 엔트리 정리:
        # UUID 로그가 이미 있는 문서에 대해 백필이 중복 삽입한 legacy_ 행 제거
        await conn.execute(
            """
            DELETE FROM v3_usage_log
            WHERE run_id LIKE 'legacy_%'
              AND doc_id IN (
                  SELECT doc_id FROM v3_usage_log
                  WHERE run_id NOT LIKE 'legacy_%'
              )
            """
        )

        # 기존 v3_documents.token_usage → v3_usage_log 백필
        # 조건: 해당 doc_id에 대한 로그가 아예 없을 때만 삽입 (legacy·UUID 불문)
        await conn.execute(
            """
            INSERT INTO v3_usage_log (doc_id, run_id, run_at, phase, model, input_tok, output_tok, cache_read, cache_write)
            SELECT
                d.doc_id,
                'legacy_' || d.doc_id          AS run_id,
                d.created_at                   AS run_at,
                ph.key                         AS phase,
                ph.value->>'model'             AS model,
                COALESCE((ph.value->>'input')::int,  0) AS input_tok,
                COALESCE((ph.value->>'output')::int, 0) AS output_tok,
                COALESCE((ph.value->>'cache_read')::int,     0) AS cache_read,
                COALESCE((ph.value->>'cache_creation')::int, 0) AS cache_write
            FROM v3_documents d,
                 jsonb_each(d.token_usage) AS ph
            WHERE d.token_usage IS NOT NULL
              AND d.token_usage != '{}'::jsonb
              AND NOT EXISTS (
                  SELECT 1 FROM v3_usage_log l
                  WHERE l.doc_id = d.doc_id
              )
            """
        )


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    return _pool
