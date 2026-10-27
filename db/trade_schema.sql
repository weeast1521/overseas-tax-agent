-- F04 거래 DB (SCRUM-26). PostgreSQL DDL. S1 로컬은 같은 모양의 SQLite(app/tools/sql_exec.py가 생성), S2에서 Oracle(SCRUM-28·48).
-- 실데이터(data/mine)는 맥의 로컬 DB에만. 합성 데이터(data/synthetic)로 scripts/load_trade_db.py 가 적재한다.

CREATE TABLE customer (
  customer_id   INTEGER PRIMARY KEY,
  display_name  VARCHAR(40)          -- 합성: 가짜 이름. 답변·로그에 쓰지 않는다
);

CREATE TABLE account (
  account_id    VARCHAR(40) PRIMARY KEY,   -- 예: MIRAE-2001
  customer_id   INTEGER NOT NULL REFERENCES customer(customer_id),
  broker_code   VARCHAR(16) NOT NULL       -- MIRAE·NAMUH·KIWOOM… (app/repo.BROKER_METHOD의 키)
);

CREATE TABLE trade (
  trade_id      VARCHAR(40) PRIMARY KEY,
  customer_id   INTEGER NOT NULL REFERENCES customer(customer_id),  -- 비정규화: 모든 조회의 필수 필터 (행 단위 격리)
  account_id    VARCHAR(40) NOT NULL REFERENCES account(account_id),
  broker_code   VARCHAR(16) NOT NULL,
  ticker        VARCHAR(16) NOT NULL,
  side          VARCHAR(16) NOT NULL CHECK (side IN ('BUY','SELL','TRANSFER_IN','TRANSFER_OUT')),
  qty           NUMERIC(18,6) NOT NULL,
  price_usd     NUMERIC(18,6) NOT NULL DEFAULT 0,   -- TRANSFER는 0
  fee_usd       NUMERIC(18,6) NOT NULL DEFAULT 0,
  trade_date    DATE NOT NULL,                      -- 체결일
  settle_date   DATE NOT NULL,                      -- 결제일 (세법상 양도·취득 시기, 환율 기준일)
  fx_settle     NUMERIC(12,4),                      -- 결제일 매매기준율 (fx_rate 조인 결과를 적재 시 복사)
  transfer_id   VARCHAR(40)                         -- TRANSFER_OUT/IN 짝
);
CREATE INDEX ix_trade_customer ON trade (customer_id, settle_date);
CREATE INDEX ix_trade_customer_ticker ON trade (customer_id, ticker);

CREATE TABLE fx_rate (
  rate_date  DATE PRIMARY KEY,
  usd_krw    NUMERIC(12,4) NOT NULL   -- 서울외국환중개 매매기준율
);

-- 일배치 집계 (SCRUM-39 PL/SQL이 채움). S1에서는 비어 있어도 된다.
CREATE TABLE settlement_daily (
  customer_id   INTEGER NOT NULL,
  settle_date   DATE NOT NULL,
  account_id    VARCHAR(40) NOT NULL,
  buy_krw       NUMERIC(18,0) NOT NULL DEFAULT 0,
  sell_krw      NUMERIC(18,0) NOT NULL DEFAULT 0,
  fee_krw       NUMERIC(18,0) NOT NULL DEFAULT 0,
  PRIMARY KEY (customer_id, settle_date, account_id)
);
