-- Mooring Point 경영지원 대시보드 스키마
-- 핵심 목적: 2단 손익 구조(공헌이익 → 진짜 영업이익) 산출
-- 금액 단위: 원(KRW), 정수 저장 원칙

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
-- projects: 수주 프로젝트 마스터
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    client          TEXT,                       -- 발주처 (B2G: 해수부, 지자체, 항만공사 등)
    start_date      TEXT,                       -- ISO8601 'YYYY-MM-DD'
    end_date        TEXT,
    contract_amount INTEGER NOT NULL DEFAULT 0, -- 계약금액(원)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(name);

-- ---------------------------------------------------------------
-- transactions: 매입/경비/매출 거래 원장 (엑셀 업로드로 적재)
--   cost_behavior 가 이 시스템의 심장.
--   ERP가 못 하는 변동비/고정비 구분을 여기서 확정한다.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT    NOT NULL,        -- ISO8601 'YYYY-MM-DD'
    project_id         INTEGER,                 -- NULL 허용: 공통비(프로젝트 미귀속)
    vendor             TEXT,                    -- 거래처
    description        TEXT,                    -- 적요
    account            TEXT,                    -- 계정과목 (외주비, 재료비, 임차료 ...)
    tx_type            TEXT    NOT NULL
                       CHECK (tx_type IN ('매입', '경비', '매출')),
    amount             INTEGER NOT NULL,        -- 공급가액(원). 손익 계산의 기준.
    amount_incl_vat    INTEGER,                 -- 부가세 포함 총액. 자금흐름 확인용.
                                                -- 원본에 VAT 정보가 없으면 NULL.
    cost_behavior      TEXT    NOT NULL DEFAULT '해당없음'
                       CHECK (cost_behavior IN ('변동', '고정', '해당없음')),
    source_file        TEXT,                    -- 원본 엑셀 파일명 (추적성)
    is_manual_override INTEGER NOT NULL DEFAULT 0
                       CHECK (is_manual_override IN (0, 1)),
                       -- 1이면 자동분류 결과를 사람이 수정한 것 → 재분류 시 보존
    is_duplicate_suspect INTEGER NOT NULL DEFAULT 0
                       CHECK (is_duplicate_suspect IN (0, 1)),
                       -- 같은 (날짜+거래처+금액)이 2회 이상 나타난 행의 2번째부터.
                       -- 자동 삭제하지 않는다 — 실제로 같은 날 같은 금액을 두 번
                       -- 지급하는 거래가 존재하므로 판단은 사람이 한다.
    created_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_project  ON transactions(project_id);
CREATE INDEX IF NOT EXISTS idx_tx_date     ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_behavior ON transactions(cost_behavior);
CREATE INDEX IF NOT EXISTS idx_tx_type     ON transactions(tx_type);

-- ---------------------------------------------------------------
-- loans: 차입금 (이자비용 산출용)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,               -- 예: '기업은행 운전자금'
    principal   INTEGER NOT NULL,               -- 원금(원)
    annual_rate REAL    NOT NULL,               -- 연이율, 소수 표기 (0.045 = 4.5%)
    start_date  TEXT,
    end_date    TEXT
);

-- ---------------------------------------------------------------
-- fixed_costs: 월 고정비 마스터 (임차료, 보험료, 관리직 급여 등)
--   프로젝트에 직접 귀속되지 않으며 배부기준에 따라 나눠 붙인다.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fixed_costs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,            -- 예: '본사 임차료'
    monthly_amount INTEGER NOT NULL,            -- 월 금액(원)
    category       TEXT                         -- 임차료 / 보험 / 통신 / 관리인건비 ...
);

-- ---------------------------------------------------------------
-- mandays: 설계 인력 투입 맨데이
--   ERP에 원가로 안 잡히는 부분. 이걸 빼면 이익률이 과대표시된다.
--   인건비 = headcount * days * daily_rate
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mandays (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    role       TEXT,                            -- 예: '설계팀장', '구조설계', 'CAD'
    headcount  INTEGER NOT NULL DEFAULT 1,      -- 투입 인원
    days       REAL    NOT NULL DEFAULT 0,      -- 투입 일수 (0.5일 단위 허용)
    daily_rate INTEGER NOT NULL DEFAULT 0,      -- 1인 1일 단가(원)
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mandays_project ON mandays(project_id);

-- ---------------------------------------------------------------
-- settings: 전역 설정 (고정비 배부기준 등)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 기본 설정값
INSERT OR IGNORE INTO settings (key, value) VALUES
    -- 고정비 배부기준: revenue | variable_cost | manday | duration | equal
    ('allocation_basis', 'revenue'),
    -- 이자 출처: loans(자동계산) | transactions(원장의 이자비용 계정)
    -- 둘 다 더하면 이자가 이중계상된다. 반드시 택일. (src/finance.py)
    ('interest_source', 'loans'),
    ('fiscal_year_start', '01-01'),
    ('currency', 'KRW');
