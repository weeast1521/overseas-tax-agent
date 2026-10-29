"""F04-2·3 SQL 검사와 고객 바인드 (SCRUM-27). LLM이 만든 SQL은 제안일 뿐이다 — 실행 여부는 여기서 정한다.

세 겹 중 1·2겹:
  (1) 구조 검사(sqlglot): 단일 SELECT만, 허용 테이블만, DML/DDL/다중문/시스템 함수 거부, LIMIT 없으면 추가      → E-SQL-SCOPE
  (2) 고객 바인드: SQL 안의 customer_id 비교는 리터럴이면 거부하고, 어떤 경우든 **세션의 customer_id를 코드가 바인드**한다.
      LLM이 customer_id = 1042 라고 써도 세션이 1023이면 1023 데이터만 나온다 — 쿼리 전체를 customer_id 필터 서브쿼리로 감싸기 때문.
  (3)겹(DB 계정 읽기 전용)은 db/readonly_user.sql · sql_exec.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {"trade", "account", "customer", "fx_rate", "settlement_daily"}
CUSTOMER_TABLES = {"trade", "account", "customer", "settlement_daily"}  # customer_id 컬럼이 있는 테이블 — 감쌀 때 필터를 건다
MAX_ROWS = 500
PLACEHOLDER = ":customer_id"
_FORBIDDEN_FUNCS = {"pg_sleep", "pg_read_file", "load_extension", "readfile", "writefile", "dblink", "copy", "lo_import", "lo_export"}
# Oracle 패키지 함수 (DBMS_LOCK.SLEEP 처럼 패키지.함수 꼴) — 지연·파일·네트워크·스케줄러
_FORBIDDEN_PACKAGES = {"dbms_lock", "dbms_pipe", "dbms_scheduler", "dbms_java", "dbms_xmlgen", "dbms_ldap", "dbms_sql",
                       "utl_http", "utl_file", "utl_inaddr", "utl_tcp", "utl_smtp", "utl_mail"}
DIALECTS = ("sqlite", "postgres", "oracle")  # F04-6: 방언은 sqlglot 이 파싱·출력 둘 다 맡는다 (LIMIT ↔ FETCH FIRST, AS 생략 등)


class SqlScopeError(Exception):
    code = "E-SQL-SCOPE"

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}{(': ' + detail) if detail else ''}")
        self.reason, self.detail = reason, detail


@dataclass
class GuardResult:
    sql: str  # 실행할 SQL (customer_id 플레이스홀더 포함, LIMIT 보장)
    tables: set[str] = field(default_factory=set)
    limit_added: bool = False
    customer_filter_injected: bool = False


def _parse(sql: str, dialect: str) -> exp.Expression:
    stmts = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    if len(stmts) != 1:
        raise SqlScopeError("MULTI_STATEMENT", f"{len(stmts)}개 문장")
    return stmts[0]


def validate(sql: str, dialect: str = "postgres") -> GuardResult:
    """구조 검사. 통과하면 행 상한이 보장된 SQL과 참조 테이블을 돌려준다. dialect 는 파싱·출력 둘 다에 쓴다."""
    if dialect not in DIALECTS:
        raise SqlScopeError("DIALECT", dialect)
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlScopeError("EMPTY")
    try:
        tree = _parse(sql, dialect)
    except SqlScopeError:
        raise
    except Exception as e:  # sqlglot.errors.ParseError 등
        raise SqlScopeError("PARSE", str(e)[:160]) from e

    # 1) 최상위는 SELECT (UNION도 SELECT 계열로 허용)
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SqlScopeError("NOT_SELECT", type(tree).__name__)
    # 2) 어디에도 쓰기·DDL·위험 노드가 없어야 한다 (서브쿼리·CTE 포함)
    banned = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Merge, exp.Command, exp.TruncateTable,
              exp.Grant, exp.Lock, exp.Into, exp.Copy, exp.Set, exp.Transaction, exp.Commit, exp.Rollback)
    for node in tree.walk():
        if isinstance(node, banned):
            raise SqlScopeError("WRITE_OR_DDL", type(node).__name__)
        if isinstance(node, exp.Anonymous) and node.name.lower() in _FORBIDDEN_FUNCS:
            raise SqlScopeError("FORBIDDEN_FUNC", node.name)
        if isinstance(node, exp.Func) and node.sql_name().lower() in _FORBIDDEN_FUNCS:
            raise SqlScopeError("FORBIDDEN_FUNC", node.sql_name())
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func) and node.this.name.lower() in _FORBIDDEN_PACKAGES:
            raise SqlScopeError("FORBIDDEN_FUNC", f"{node.this.name}.{node.expression.name}")
    # 3) 테이블 허용 목록 (CTE 이름은 테이블이 아님)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = {t.name.lower() for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes}
    bad = tables - ALLOWED_TABLES
    if bad:
        raise SqlScopeError("TABLE_NOT_ALLOWED", ", ".join(sorted(bad)))
    if not tables & CUSTOMER_TABLES and tables != {"fx_rate"}:
        raise SqlScopeError("NO_CUSTOMER_TABLE", ", ".join(sorted(tables)) or "(없음)")
    # 4) customer_id 리터럴 비교 금지 (LLM이 고객을 지정하려 한 흔적)
    for eq in tree.find_all(exp.EQ, exp.NEQ, exp.In):
        cols = [c for c in eq.find_all(exp.Column) if c.name.lower() == "customer_id"]
        if cols and any(isinstance(x, exp.Literal) for x in eq.find_all(exp.Literal)):
            raise SqlScopeError("CUSTOMER_LITERAL", eq.sql(dialect=dialect)[:80])
    # 5) 행 상한 보장 — LIMIT(sqlite/postgres) 또는 FETCH FIRST n ROWS ONLY(oracle). sqlglot 이 방언에 맞게 써 준다
    limit_added = False
    outer = tree
    lim = outer.args.get("limit")
    if lim is None:
        outer = outer.limit(MAX_ROWS)
        limit_added = True
    else:
        node = lim.args.get("count") if isinstance(lim, exp.Fetch) else lim.expression  # Oracle FETCH 는 count, LIMIT 은 expression
        try:
            if int(node.name) > MAX_ROWS:
                outer.set("limit", None)
                outer = outer.limit(MAX_ROWS)
                limit_added = True
        except Exception:
            pass
    return GuardResult(sql=outer.sql(dialect=dialect), tables=tables, limit_added=limit_added)


def bind_customer(sql: str, dialect: str = "postgres") -> GuardResult:
    """검사 + 세션 고객 격리. 원 쿼리를 그대로 두지 않고, customer_id 컬럼이 있는 모든 테이블 참조를
    (SELECT * FROM <table> WHERE customer_id = :customer_id) 로 바꾼다 → LLM이 무엇을 썼든 다른 고객 행은 보이지 않는다.
    원 쿼리에 customer_id = :customer_id 플레이스홀더가 있으면 그대로 둔다(중복 필터는 무해)."""
    g = validate(sql, dialect)
    tree = _parse(g.sql, dialect)
    injected = False
    for t in list(tree.find_all(exp.Table)):
        if t.name.lower() in CUSTOMER_TABLES:
            alias = t.alias or t.name
            sub = exp.Subquery(this=sqlglot.parse_one(f"SELECT * FROM {t.name} WHERE customer_id = {PLACEHOLDER}", read=dialect), alias=alias)
            t.replace(sub)
            injected = True
    out = tree.sql(dialect=dialect)
    # sqlglot이 ':customer_id'를 플레이스홀더로 유지하는지 확인 — 깨졌으면 복구
    if PLACEHOLDER not in out:
        out = re.sub(r"customer_id\s*=\s*'?:customer_id'?", f"customer_id = {PLACEHOLDER}", out)
    return GuardResult(sql=out, tables=g.tables, limit_added=g.limit_added, customer_filter_injected=injected)


def transpile(sql: str, read: str, write: str) -> str:
    """방언 변환 (F04-6). 템플릿·few-shot 은 postgres 로 쓰고 Oracle 로는 여기서 바꾼다. 날짜 리터럴처럼 의미가 다른 건 Oracle 전용 예시로 따로 둔다."""
    return sqlglot.transpile(sql, read=read, write=write)[0]
