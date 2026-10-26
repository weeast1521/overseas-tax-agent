"""SCRUM-31 코퍼스 수집·청킹 (F06-1, F06-2). 출력: data/corpus/v1/*.json — repo.search_corpus와 RAG 적재(SCRUM-32)가 읽는다.

원칙: 법령 원문은 사람이 검증할 수 있는 출처에서만. 기억이나 요약으로 채우지 않는다. 모든 chunk에 source_url·effective_date.

  # 1) 법령: 법제처 Open API (open.law.go.kr에서 OC 발급 — 신청 시 쓴 이메일의 @ 앞부분)
  LAW_OC=<내 OC> python scripts/build_corpus.py --fetch
  # 2) 국세청 안내·해석례·증권사 FAQ: 브라우저에서 복사해 data/corpus/raw/<파일>.txt 로 저장 (형식은 아래)
  # 3) 청킹·검사·출력 (API 없이도 raw 파일만으로 실행 가능)
  python scripts/build_corpus.py --build
  python scripts/build_corpus.py --check        # ★ 자리표시·출처 누락·필수 질문 3개 검사

raw 텍스트 형식 (data/corpus/raw/*.txt):
  # source_url: https://...
  # effective_date: 2025-05-01        (문서 게시일·귀속연도 기준일)
  # tax_year_scope: 2024 귀속          (선택)
  ## 신고 대상                          ← '## 제목' 하나가 article 하나 (조문이면 '## 제118조의2(국외자산 양도소득의 범위)')
  본문 …

청킹: 법령은 조 → 항 단위, 항이 800자를 넘으면 문장 경계에서 나누고 part 번호. 조 머리(제목·본문)는 각 항 chunk 앞에 붙여 맥락 유지.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "corpus"
RAW = CORPUS / "raw"
OUT = CORPUS / "v1"
MAX_CHARS = 800
API = "https://www.law.go.kr/DRF"

# 질문에 자주 나오는 말 → keywords (키워드 검색 스텁·하이브리드 검색의 BM25 보조)
KEYWORDS = ["국외자산", "해외주식", "양도가액", "취득가액", "필요경비", "기준환율", "환율", "결제일", "수령일", "지급일", "기본공제",
            "250만", "세율", "20", "지방소득세", "선입선출", "먼저 취득", "이동평균", "총평균", "통산", "손익", "결손금", "합산",
            "예정신고", "확정신고", "신고기한", "5월", "분할납부", "대체입고", "이체", "외국납부세액", "가산세", "기한후신고", "무신고"]


# ---------------- 법제처 Open API ----------------

def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (고정된 공공 API)
        return json.loads(r.read().decode("utf-8"))


def find_law(name: str, oc: str) -> dict:
    """법령명 → {mst, id, 시행일자}. 이름이 정확히 같은 현행 법령."""
    q = urllib.parse.urlencode({"OC": oc, "target": "law", "type": "JSON", "query": name, "display": 20})
    res = _get(f"{API}/lawSearch.do?{q}")
    laws = res.get("LawSearch", {}).get("law", [])
    laws = laws if isinstance(laws, list) else [laws]
    for l in laws:
        if l.get("법령명한글") == name:
            return {"mst": l.get("법령일련번호"), "id": l.get("법령ID"), "ef": l.get("시행일자")}
    raise SystemExit(f"법령 '{name}'을 찾지 못함 (검색 결과 {[l.get('법령명한글') for l in laws][:5]})")


def fetch_law(name: str, oc: str, ef_yd: str | None) -> dict:
    """법령 본문 JSON. ef_yd(YYYYMMDD)를 주면 그 날짜에 시행 중이던 버전을 요청한다(2025 귀속 → 20251231)."""
    info = find_law(name, oc)
    params = {"OC": oc, "target": "law", "type": "JSON", "MST": info["mst"]}
    if ef_yd:
        params["efYd"] = ef_yd
    return _get(f"{API}/lawService.do?{urllib.parse.urlencode(params)}")


def _as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _text(x) -> str:
    """API 값은 문자열 또는 문자열 배열(여러 줄)."""
    if isinstance(x, list):
        return "\n".join(_text(i) for i in x)
    return str(x or "").strip()


def article_key(no: str, branch: str | None) -> str:
    return f"제{int(no)}조" + (f"의{int(branch)}" if branch and str(branch) not in ("0", "") else "")


def parse_law_json(doc: dict, law_name: str, want: set[str] | None) -> tuple[dict, list[dict]]:
    """API JSON → (기본정보, [{article, title, head, clauses:[(번호, 본문)]}])."""
    law = doc.get("법령", doc)
    base = law.get("기본정보", {})
    out = []
    for u in _as_list(law.get("조문", {}).get("조문단위")):
        if u.get("조문여부") and u.get("조문여부") != "조문":  # 전문(장·절 제목) 제외
            continue
        key = article_key(u.get("조문번호"), u.get("조문가지번호"))
        if want and key not in want:
            continue
        head = _text(u.get("조문내용"))
        clauses = []
        for h in _as_list(u.get("항")):
            body = [_text(h.get("항내용"))]
            for ho in _as_list(h.get("호")):
                body.append(_text(ho.get("호내용")))
                for mok in _as_list(ho.get("목")):
                    body.append(_text(mok.get("목내용")))
            clauses.append((_text(h.get("항번호")), "\n".join(b for b in body if b)))
        if not clauses:  # 항 없이 호만 있는 조문
            hos = [_text(ho.get("호내용")) for h in [u] for ho in _as_list(h.get("호"))]
            if hos:
                clauses.append(("", "\n".join(hos)))
        out.append({"article": key, "title": _text(u.get("조문제목")), "head": head, "clauses": clauses,
                    "ef": _text(u.get("조문시행일자")) or None})
    return base, out


# ---------------- 청킹 ----------------

def split_sentences(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for s in re.split(r"(?<=[다\.])\s+|\n", text):
        if not s:
            continue
        if cur and len(cur) + len(s) + 1 > limit:
            parts.append(cur.strip()); cur = ""
        while len(s) > limit:  # 한 문장이 상한보다 긴 경우(드묾)
            parts.append(s[:limit]); s = s[limit:]
        cur += (" " if cur else "") + s
    if cur.strip():
        parts.append(cur.strip())
    return parts


def keywords_of(text: str) -> list[str]:
    return [k for k in KEYWORDS if k in text]


def _ymd(s: str | None) -> str | None:
    s = (s or "").replace(".", "").replace("-", "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else (s or None)


_AMEND = re.compile(r"<(?:개정|신설|전문개정|본조신설|제목개정)\s*([^>]*)>")
_DATE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})")


def last_amended(text: str) -> str | None:
    """원문 속 <개정 2001.12.31, 2005.2.19> 표시에서 가장 늦은 날짜. 없으면 None."""
    dates = [f"{y}-{int(m):02d}-{int(d):02d}" for g in _AMEND.findall(text) for y, m, d in _DATE.findall(g)]
    return max(dates) if dates else None


def scope_of(amended: str | None, tax_year: int) -> str:
    """2025 귀속에 이 문구가 그대로 적용됐는지. 현행 버전만 받을 수 있으므로(시행일 법령 API 없음) 개정 표시로 판단한다.
    그 해 1월 1일 이후 개정이면 귀속연도 버전과 다를 수 있어 사람이 확인해야 한다. (공포일 기준이라 보수적)"""
    if amended is None:
        return f"{tax_year} 귀속 확인 필요(개정 표시 없음)"
    return f"{tax_year} 귀속 동일(최종 개정 {amended})" if amended < f"{tax_year}-01-01" else f"{tax_year} 귀속 확인 필요({amended} 개정)"


def law_chunks(law_name: str, doc_type: str, base: dict, arts: list[dict], source_url: str, tax_year: int = 2025) -> list[dict]:
    ef = _ymd(base.get("시행일자"))
    chunks = []
    for a in arts:
        head = a["head"] if a["head"] else f"{a['article']}({a['title']})"
        items = a["clauses"] or [("", "")]
        for clause, body in items:
            prefix = head if not body.startswith(head[:20]) else ""
            for i, part in enumerate(split_sentences(body or head, MAX_CHARS - min(len(prefix), 200)), 1):
                text = (prefix[:200] + "\n" + part).strip() if prefix and body else part
                cid = f"{law_name}|{a['article']}|{clause}|{i}"
                chunks.append({
                    "chunk_id": "c_" + hashlib.sha1(cid.encode()).hexdigest()[:10],
                    "law": law_name, "article": a["article"], "clause": clause, "part": i, "title": a["title"],
                    "doc_type": doc_type, "effective_date": _ymd(a.get("ef")) or ef,
                    "source_url": source_url + "/" + urllib.parse.quote(a["article"]),
                    "amended_last": last_amended(text), "tax_year_scope": scope_of(last_amended(text), tax_year),
                    "keywords": keywords_of(text), "text": text,
                })
    return chunks


def raw_chunks(path: Path, law: str | None = None, doc_type: str | None = None) -> list[dict]:
    """붙여 넣기 텍스트 → chunk. law·doc_type은 sources.json manual 항목에서, 없으면 파일 머리의 '# law:' '# doc_type:'에서."""
    meta, sections, cur = {}, [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#\s*(source_url|effective_date|tax_year_scope|law|doc_type)\s*:\s*(.+)$", line)
        if m and cur is None:
            meta[m.group(1)] = m.group(2).strip(); continue
        if line.startswith("## "):
            cur = {"title": line[3:].strip(), "body": []}; sections.append(cur); continue
        if cur is not None:
            cur["body"].append(line)
    if not meta.get("source_url") or not meta.get("effective_date"):
        raise SystemExit(f"{path.name}: 맨 위에 '# source_url:'과 '# effective_date:'가 필요함")
    law = law or meta.get("law")
    doc_type = doc_type or meta.get("doc_type")
    if not law or not doc_type:
        raise SystemExit(f"{path.name}: sources.json manual에 없는 파일은 '# law:'와 '# doc_type:'이 필요함")
    chunks = []
    for s in sections:
        body = "\n".join(s["body"]).strip()
        if not body:
            continue
        m = re.match(r"(제\d+조(?:의\d+)?)", s["title"])
        article = m.group(1) if m else s["title"][:40]
        for i, part in enumerate(split_sentences(body, MAX_CHARS), 1):
            cid = f"{law}|{article}|{i}"
            chunks.append({"chunk_id": "c_" + hashlib.sha1(cid.encode()).hexdigest()[:10], "law": law, "article": article,
                           "clause": "", "part": i, "title": s["title"], "doc_type": doc_type,
                           "effective_date": _ymd(meta["effective_date"]), "source_url": meta["source_url"],
                           "tax_year_scope": meta.get("tax_year_scope"), "keywords": keywords_of(s["title"] + " " + part),
                           "text": f"{s['title']}\n{part}"})
    return chunks


# ---------------- 검사 ----------------

REQUIRED_QUESTIONS = {  # 수용 조건: 이 세 질문에 답하는 chunk가 각각 있어야 한다
    "대체입고 취득가액": [["대체입고", "이체", "이관", "입고"], ["취득가액", "취득가", "매입가"]],
    # 법령 표현은 "수령하거나 지출한 날 현재 … 기준환율"(시행령 제178조의5) — 결제일이라는 말을 쓰지 않는다
    "결제일 환율": [["결제일", "수령일", "지급일", "수령한 날", "지급한 날", "지출한 날", "수령하거나"], ["환율", "기준환율", "환산"]],
    "250만원 공제": [["250만", "2백50만", "2,500,000"], ["공제"]],
}


def check(chunks: list[dict]) -> list[str]:
    errs = []
    star = [c["chunk_id"] for c in chunks if "★" in c["text"]]
    if star:
        errs.append(f"★ 자리표시 {len(star)}건: {star[:5]}")
    miss = [c["chunk_id"] for c in chunks if not c.get("source_url") or not c.get("effective_date")]
    if miss:
        errs.append(f"source_url·effective_date 누락 {len(miss)}건: {miss[:5]}")
    if len(chunks) < 60:
        errs.append(f"chunk {len(chunks)}개 < 60")
    long = [c["chunk_id"] for c in chunks if len(c["text"]) > MAX_CHARS + 210]
    if long:
        errs.append(f"800자 초과 {len(long)}건: {long[:5]}")
    for q, groups in REQUIRED_QUESTIONS.items():
        if not any(all(any(w in c["text"] for w in g) for g in groups) for c in chunks):
            errs.append(f"'{q}'에 답하는 chunk 없음")
    ids = [c["chunk_id"] for c in chunks]
    if len(ids) != len(set(ids)):
        errs.append("chunk_id 중복")
    return errs


def load_v1() -> list[dict]:
    return [c for p in sorted(OUT.glob("*.json")) for c in json.loads(p.read_text(encoding="utf-8"))]


# ---------------- main ----------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="법제처 Open API로 법령 원문을 data/corpus/raw/law_*.json에 저장 (LAW_OC 필요)")
    ap.add_argument("--build", action="store_true", help="raw → data/corpus/v1/*.json")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ef-yd", help="이 날짜에 시행 중이던 버전 (기본: sources.json as_of_date)")
    args = ap.parse_args()
    src = json.loads((CORPUS / "sources.json").read_text(encoding="utf-8"))
    RAW.mkdir(parents=True, exist_ok=True)

    if args.fetch:
        oc = os.getenv("LAW_OC")
        if not oc:
            print("LAW_OC 없음 — open.law.go.kr에서 OPEN API 신청 후 OC(이메일 @ 앞부분)를 .env에 LAW_OC=로"); return 2
        ef = args.ef_yd or src.get("as_of_date")
        for l in src["laws"]:
            doc = fetch_law(l["law"], oc, ef)
            p = RAW / f"law_{l['law'].replace(' ', '_')}.json"
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            base, arts = parse_law_json(doc, l["law"], set(l["articles"]))
            got = {a["article"] for a in arts}
            print(f"{l['law']}: 시행 {base.get('시행일자')} · 조문 {len(arts)}/{len(l['articles'])}"
                  + (f" · 없음 {sorted(set(l['articles']) - got)}" if set(l["articles"]) - got else ""))

    if args.build:
        chunks: list[dict] = []
        for l in src["laws"]:
            p = RAW / f"law_{l['law'].replace(' ', '_')}.json"
            if not p.exists():
                print(f"건너뜀: {p.name} 없음 (--fetch 먼저)"); continue
            base, arts = parse_law_json(json.loads(p.read_text(encoding="utf-8")), l["law"], set(l["articles"]))
            chunks += law_chunks(l["law"], l["doc_type"], base, arts, f"https://www.law.go.kr/법령/{l['law'].replace(' ', '')}")
        for m in src["manual"]:
            p = RAW / m["file"]
            if not p.exists():
                print(f"건너뜀: raw/{m['file']} 없음 — {m['what']}"); continue
            chunks += raw_chunks(p, m["law"], m["doc_type"])
        known = {m["file"] for m in src["manual"]}
        for p in sorted(RAW.glob("*.txt")):  # API 대신 법령을 붙여 넣은 파일 등 (머리에 # law: / # doc_type:)
            if p.name not in known:
                chunks += raw_chunks(p)
        if not chunks:
            print("만들 chunk가 없음 — 기존 v1을 지우지 않고 멈춤. raw 파일을 먼저 준비 (data/corpus/raw/README.md)"); return 1
        OUT.mkdir(exist_ok=True)
        by_type: dict[str, list] = {}
        for c in chunks:
            by_type.setdefault(c["doc_type"], []).append(c)
        for old in OUT.glob("*.json"):
            old.unlink()
        for t, cs in by_type.items():
            (OUT / f"{t}.json").write_text(json.dumps(cs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"chunk {len(chunks)}개 → {OUT} ({', '.join(f'{t} {len(v)}' for t, v in by_type.items())})")

    if args.check or args.build:
        v1 = load_v1()
        errs = check(v1)
        for e in errs:
            print("✗", e)
        laws = [c for c in v1 if c["doc_type"] in ("법령", "시행령")]
        review = sorted({(c["law"], c["article"], c.get("amended_last") or "") for c in laws if "확인 필요" in (c.get("tax_year_scope") or "")})
        print(f"2025 귀속 동일 {sum('동일' in (c.get('tax_year_scope') or '') for c in laws)}/{len(laws)} 조각 · "
              f"확인 필요 조문 {len(review)}개" + (": " + ", ".join(f"{l} {a}({d or '표시 없음'})" for l, a, d in review) if review else ""))
        print("검사 통과 ✓" if not errs else f"검사 실패 {len(errs)}건")
        return 1 if errs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
