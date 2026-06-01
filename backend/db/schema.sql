-- Phase B 전체 스키마
-- rebate_db_v2에 신규 생성. 기존 테이블(documents, items 등)은 건드리지 않음.

CREATE TABLE IF NOT EXISTS users (
    user_id                 SERIAL PRIMARY KEY,
    username                VARCHAR(100) UNIQUE NOT NULL,
    display_name            VARCHAR(200),
    password_hash           VARCHAR(200) NOT NULL,
    is_admin                BOOLEAN NOT NULL DEFAULT FALSE,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    force_password_change   BOOLEAN NOT NULL DEFAULT FALSE,
    login_count             INTEGER NOT NULL DEFAULT 0,
    last_login_at           TIMESTAMPTZ,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_sessions (
    session_id  VARCHAR(36) PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    ip_address  VARCHAR(50),
    user_agent  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions(expires_at);

-- ───────────────────────────────────────────────────────────────
-- Phase B 파이프라인 전용 테이블

CREATE TABLE IF NOT EXISTS v3_documents (
    doc_id          VARCHAR(255) PRIMARY KEY,
    pdf_filename    VARCHAR(255) NOT NULL,
    form_id         VARCHAR(50),
    status          VARCHAR(50) NOT NULL DEFAULT 'ocr',
    -- status: ocr | analyzing | pending | done | error
    error_type      VARCHAR(50),   -- unknown_form | technical
    error_phase     VARCHAR(50),   -- Phase 1 | Phase 2 | Phase 3
    error_message   TEXT,
    -- {"phase1":{"input":N,"output":N,"model":"..."},"phase2":{...},"phase3":{...}}
    token_usage     JSONB NOT NULL DEFAULT '{}',
    pages_count     INTEGER,               -- OCR 완료 후 저장. 20 초과 시 다중 번들 경고
    hatsu_month     VARCHAR(7),                    -- 청구연월 YYYY.MM (사용자 입력, 발생月 W열)
    uploaded_by     INTEGER REFERENCES users(user_id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Phase 3에서 저신뢰도 항목 — 사용자 확인 대기
CREATE TABLE IF NOT EXISTS v3_mappings (
    id              SERIAL PRIMARY KEY,
    doc_id          VARCHAR(255) REFERENCES v3_documents(doc_id) ON DELETE CASCADE,
    mapping_type    VARCHAR(20) NOT NULL,   -- retailer | product
    ocr_name        VARCHAR(500) NOT NULL,
    candidates      JSONB NOT NULL DEFAULT '[]',
    page_number     INTEGER,               -- 해당 OCR 명칭이 처음 등장한 페이지
    confirmed_code  VARCHAR(100),
    confirmed_name  VARCHAR(500),
    confirmed_by    INTEGER REFERENCES users(user_id),
    confirmed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- mapping_type: retailer | product | dist (판매처 1:N 확인용)

-- 기존 DB 마이그레이션:
-- ALTER TABLE v3_mappings ADD COLUMN IF NOT EXISTS page_number INTEGER;
-- ALTER TABLE v3_documents ADD COLUMN IF NOT EXISTS pages_count INTEGER;
-- ALTER TABLE v3_documents ADD COLUMN IF NOT EXISTS hatsu_month VARCHAR(7);  -- 청구연월 YYYY.MM (사용자 업로드 시 입력)
-- CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_mappings_doc_type_ocr ON v3_mappings(doc_id, mapping_type, ocr_name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_mappings_doc_type_ocr ON v3_mappings(doc_id, mapping_type, ocr_name);
CREATE INDEX IF NOT EXISTS idx_v3_mappings_doc_id ON v3_mappings(doc_id);
CREATE INDEX IF NOT EXISTS idx_v3_documents_status ON v3_documents(status);
CREATE INDEX IF NOT EXISTS idx_v3_documents_uploaded_by ON v3_documents(uploaded_by);
