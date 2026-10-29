-- F04-6 거래 DB Oracle 판 (SCRUM-28). db/trade_schema.sql(PostgreSQL)과 같은 모양, 타입만 Oracle.
-- 실 컨테이너(Oracle 23ai Free) 적재·검증은 SCRUM-48. 읽기 전용 계정 TAX_RO 가 세 겹 중 마지막 겹.
-- 실행: sqlplus system/<pw>@//localhost:1521/FREEPDB1 @db/trade_schema_oracle.sql

-- 소유 스키마 (적재용)
CREATE USER tax_owner IDENTIFIED BY "change-me" QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION, CREATE TABLE, CREATE PROCEDURE TO tax_owner;

CREATE TABLE tax_owner.customer (
  customer_id   NUMBER(12)     PRIMARY KEY,
  display_name  VARCHAR2(40)
);

CREATE TABLE tax_owner.account (
  account_id    VARCHAR2(40)   PRIMARY KEY,
  customer_id   NUMBER(12)     NOT NULL REFERENCES tax_owner.customer(customer_id),
  broker_code   VARCHAR2(16)   NOT NULL
);

CREATE TABLE tax_owner.trade (
  trade_id      VARCHAR2(40)   PRIMARY KEY,
  customer_id   NUMBER(12)     NOT NULL REFERENCES tax_owner.customer(customer_id),
  account_id    VARCHAR2(40)   NOT NULL REFERENCES tax_owner.account(account_id),
  broker_code   VARCHAR2(16)   NOT NULL,
  ticker        VARCHAR2(16)   NOT NULL,
  side          VARCHAR2(12)   NOT NULL CHECK (side IN ('BUY','SELL','TRANSFER_IN','TRANSFER_OUT')),
  qty           NUMBER(18,6)   NOT NULL,
  price_usd     NUMBER(18,6)   DEFAULT 0 NOT NULL,
  fee_usd       NUMBER(18,6)   DEFAULT 0 NOT NULL,
  trade_date    DATE           NOT NULL,
  settle_date   DATE           NOT NULL,          -- 세법상 양도·취득일. 환율 기준일. (PostgreSQL 판과 달리 DATE 타입 → few-shot 은 DATE 리터럴)
  fx_settle     NUMBER(12,4),
  transfer_id   VARCHAR2(40)
);
CREATE INDEX tax_owner.ix_trade_customer ON tax_owner.trade (customer_id, settle_date);

CREATE TABLE tax_owner.fx_rate (
  rate_date     DATE           PRIMARY KEY,
  usd_krw       NUMBER(12,4)   NOT NULL
);

CREATE TABLE tax_owner.settlement_daily (
  customer_id   NUMBER(12)     NOT NULL,
  settle_date   DATE           NOT NULL,
  account_id    VARCHAR2(40)   NOT NULL,
  buy_krw       NUMBER(18)     DEFAULT 0 NOT NULL,
  sell_krw      NUMBER(18)     DEFAULT 0 NOT NULL,
  fee_krw       NUMBER(18)     DEFAULT 0 NOT NULL,
  CONSTRAINT pk_settlement_daily PRIMARY KEY (customer_id, settle_date, account_id)
);

-- 읽기 전용 계정: sql_query 실행 커넥션은 이 계정만 (ORACLE_RO_USER). SELECT 외 권한 없음 → 파서를 우회한 INSERT 도 ORA-01031
CREATE USER tax_ro IDENTIFIED BY "change-me";
GRANT CREATE SESSION TO tax_ro;
GRANT SELECT ON tax_owner.customer         TO tax_ro;
GRANT SELECT ON tax_owner.account          TO tax_ro;
GRANT SELECT ON tax_owner.trade            TO tax_ro;
GRANT SELECT ON tax_owner.fx_rate          TO tax_ro;
GRANT SELECT ON tax_owner.settlement_daily TO tax_ro;
-- 스키마 접두사 없이 쓰도록 동의어 (LLM 이 만드는 SQL 은 테이블 이름만 쓴다)
CREATE SYNONYM tax_ro.customer         FOR tax_owner.customer;
CREATE SYNONYM tax_ro.account          FOR tax_owner.account;
CREATE SYNONYM tax_ro.trade            FOR tax_owner.trade;
CREATE SYNONYM tax_ro.fx_rate          FOR tax_owner.fx_rate;
CREATE SYNONYM tax_ro.settlement_daily FOR tax_owner.settlement_daily;
-- 쿼리 시간 상한은 세션(call_timeout, app/tools/sql_exec.OracleExecutor)과 리소스 프로파일 둘 다로
CREATE PROFILE tax_ro_profile LIMIT CPU_PER_CALL 500;   -- 1/100초 단위 = 5초
ALTER USER tax_ro PROFILE tax_ro_profile;
