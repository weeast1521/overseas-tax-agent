-- 고객 산정 방식 (F11-2, SCRUM-47). 지금은 SQLite(app/method_store.py), S2에서 Oracle(SCRUM-48)로 옮길 때 이 정의를 쓴다.
-- 선택제 증권사(NH 나무) 계좌만 저장. 계좌·과세연도당 1행, 변경 규칙은 애플리케이션(METHOD_CHANGE_RULE)이 강제.
CREATE TABLE customer_method (
  customer_id   NUMBER(10)    NOT NULL,
  account_id    VARCHAR2(40)  NOT NULL,
  tax_year      NUMBER(4)     NOT NULL,
  method        VARCHAR2(4)   NOT NULL CONSTRAINT ck_customer_method CHECK (method IN ('FIFO', 'AVG')),
  changed_at    TIMESTAMP WITH TIME ZONE NOT NULL,
  confirmed_by  VARCHAR2(64)  NOT NULL,   -- 확인한 상담원 ID (S2: 게이트웨이 JWT subject)
  CONSTRAINT pk_customer_method PRIMARY KEY (customer_id, account_id, tax_year)
);
