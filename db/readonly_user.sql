-- F04-4 읽기 전용 계정 (SCRUM-27). 파서(sql_guard)를 우회해도 DB가 막는 마지막 겹.
-- PostgreSQL. 애플리케이션의 sql_query 실행 커넥션은 이 계정만 쓴다 (PG_TRADE_RO_DSN).
CREATE ROLE tax_ro LOGIN PASSWORD 'change-me';
GRANT CONNECT ON DATABASE tax_trade TO tax_ro;
GRANT USAGE ON SCHEMA public TO tax_ro;
GRANT SELECT ON customer, account, trade, fx_rate, settlement_daily TO tax_ro;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM tax_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO tax_ro;
ALTER ROLE tax_ro SET statement_timeout = '5s';          -- 6초 걸리는 쿼리 → 취소 → E-SQL-EXEC
ALTER ROLE tax_ro SET default_transaction_read_only = on;
-- SQLite(S1 로컬)에서는 같은 효과를 파일 열기 모드(mode=ro)·PRAGMA query_only·progress_handler 타임아웃으로 낸다 (app/tools/sql_exec.py)
