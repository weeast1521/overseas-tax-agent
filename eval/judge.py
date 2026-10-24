"""SCRUM-44 (F10-8): LLM-as-judge — 답변 초안의 문장 품질을 3항목 1~5점으로 채점한다. 숫자의 맞고 틀림은 보지 않는다(코드가 본다).

  python -m eval.judge submit results/A_1024_r1.jsonl [results/A_1024_sonnet-5-5.jsonl ...]   # Batch 제출 (50% 할인)
  python -m eval.judge status
  python -m eval.judge merge            # 끝났으면 results/judge_scores_<name>.jsonl 저장 + 표 출력
  python -m eval.judge report results/judge_scores_A_1024_r1.jsonl [...]                        # 표만 다시

채점 항목 (각 1~5, 근거 한 줄):
  ① grounding  숫자 외 설명(차이 원인·절차·규정)이 인용 조문·figures·유형과 맞는가. 인용 없이 단정하면 감점
  ② readability 상담원이 고객에게 그대로 읽어 줄 수 있는 문장인가 (존댓말, 3문장 내외, 전문용어 풀이, JSON·코드 흔적 없음)
  ③ compliance 금지 표현 없음 — 투자 권유, 어느 방식이 유리하다는 판단, "약"·반올림 숫자, 단정적 세무 판단("반드시 ~입니다"), 고객 식별 정보
채점 모델: JUDGE_SCORER_MODEL (기본 claude-sonnet-5-5). 채점 대상 모델과 다르게 둔다(자기 채점 방지). Opus 결과도 Sonnet이 채점.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "results" / "judge_batch.json"
SCORER = os.getenv("JUDGE_SCORER_MODEL", "claude-sonnet-5-5")

SYSTEM = """너는 증권사 고객센터 상담 품질 평가자다. 해외주식 양도소득세 민원에 대해 AI가 만든 답변 초안을 채점한다.

중요: 숫자(세액·양도차익·금액)의 맞고 틀림은 절대 판단하지 마라. 숫자는 별도 코드가 검증한다. 너는 문장의 품질만 본다.

세 항목을 각각 1~5점으로 채점하고 근거를 한 줄씩 쓴다.
1. grounding — 숫자 외의 설명(왜 두 계산서가 다른지, 어떤 절차인지, 어떤 규정인지)이 제공된 인용 조문·figures·민원 유형과 맞는가. 인용 없이 규정을 단정하거나, 유형과 무관한 설명을 하면 감점. 5=설명 전부가 근거와 맞음, 3=일부 근거 없음, 1=틀리거나 무관.
2. readability — 상담원이 고객에게 전화로 그대로 읽어 줄 수 있는가. 존댓말, 핵심 3문장 내외, 전문용어는 풀어 씀, JSON·중괄호·코드·영문 키 같은 기계 흔적 없음. 5=그대로 읽을 수 있음, 3=손봐야 읽을 수 있음, 1=읽을 수 없음(JSON 그대로 등).
3. compliance — 금지 표현이 없는가: 투자 권유(사라/팔아라), 어느 산정 방식이 유리하다는 판단, "약"·"대략"·반올림한 금액, 단정적 세무 판단("반드시", "무조건"), 고객 이름·계좌번호. 5=전혀 없음, 3=경미한 표현 1개, 1=명백한 위반.

반드시 아래 JSON만 출력한다. 다른 글은 쓰지 않는다. reason은 각 40자 이내로 짧게.
{"grounding": {"score": 1-5, "reason": "..."}, "readability": {"score": 1-5, "reason": "..."}, "compliance": {"score": 1-5, "reason": "..."}}"""


def load(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def user_msg(r: dict) -> str:
    fig = r.get("figures") or {}
    fig_s = json.dumps({k: fig.get(k) for k in ("tax_krw", "total_gain_krw", "taxable_krw") if k in fig}, ensure_ascii=False)
    src = r.get("sources") or []
    src_s = "\n".join(f"- {s.get('law')} {s.get('article')}" for s in src) or "- (인용 없음)"
    return (f"민원 유형: {r.get('category')}\nstatus: {r.get('status')} / {r.get('review_reason') or ''}\n"
            f"코드가 계산한 figures(참고용, 판단 금지): {fig_s}\n인용 조문:\n{src_s}\n\n--- 답변 초안 ---\n{(r.get('draft') or '')[:3000]}")


def targets(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("draft") and r.get("status") not in ("REJECTED", "ERROR")]


def client():
    from anthropic import Anthropic
    return Anthropic()


def cmd_submit(paths: list[str]) -> int:
    reqs, index = [], {}
    for fi, p in enumerate(paths):
        name = Path(p).stem
        for r in targets(load(Path(p))):
            cid = re.sub(r"[^a-zA-Z0-9_-]", "-", f"f{fi}_{r['case_id']}")[:64]  # Batch custom_id 허용 문자: 영숫자 _ - (64자)
            index[cid] = {"file": name, "case_id": r["case_id"], "kind": r["kind"], "category": r.get("category"), "status": r.get("status")}
            reqs.append({"custom_id": cid, "params": {"model": SCORER, "max_tokens": 900, "system": SYSTEM,
                                                      "messages": [{"role": "user", "content": user_msg(r)}]}})
    if not reqs:
        print("채점 대상 없음"); return 1
    b = client().messages.batches.create(requests=reqs)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"batch_id": b.id, "scorer": SCORER, "n": len(reqs), "index": index}, ensure_ascii=False), encoding="utf-8")
    print(f"제출 batch_id={b.id}  {len(reqs)}건  scorer={SCORER}  (파일 {len(paths)}개)\n→ 몇 분 뒤 `python -m eval.judge status`, 끝나면 `merge`")
    return 0


def cmd_status() -> int:
    st = json.loads(STATE.read_text(encoding="utf-8"))
    b = client().messages.batches.retrieve(st["batch_id"])
    print(f"{b.id}: {b.processing_status}  {b.request_counts}")
    return 0


def parse_scores(text: str) -> dict | None:
    """완전한 JSON이면 그대로, 잘린 JSON이면 항목별 score/reason을 정규식으로 건진다 (1차 채점이 max_tokens에 잘렸던 사고 대응)."""
    full = _parse_full(text)
    if full:
        return full
    out = {}
    for k in ("grounding", "readability", "compliance"):
        m = re.search(rf'"{k}"\s*:\s*\{{\s*"score"\s*:\s*([1-5])\s*(?:,\s*"reason"\s*:\s*"([^"]*))?', text, re.S)
        if not m:
            return None
        out[k] = {"score": int(m.group(1)), "reason": (m.group(2) or "")[:200]}
    return out


def _parse_full(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
        out = {}
        for k in ("grounding", "readability", "compliance"):
            v = j[k]
            sc = int(v["score"]) if isinstance(v, dict) else int(v)
            if not 1 <= sc <= 5:
                return None
            out[k] = {"score": sc, "reason": (v.get("reason", "") if isinstance(v, dict) else "")[:200]}
        return out
    except Exception:
        return None


def cmd_merge() -> int:
    st = json.loads(STATE.read_text(encoding="utf-8"))
    cl = client()
    b = cl.messages.batches.retrieve(st["batch_id"])
    if b.processing_status != "ended":
        print(f"아직 {b.processing_status}"); return 1
    by_file: dict[str, list[dict]] = defaultdict(list)
    tin = tout = bad = 0
    for r in cl.messages.batches.results(st["batch_id"]):
        meta = st["index"].get(r.custom_id, {})
        if r.result.type != "succeeded":
            by_file[meta.get("file", "?")].append({**meta, "error": r.result.type}); bad += 1; continue
        msg = r.result.message
        tin += msg.usage.input_tokens; tout += msg.usage.output_tokens
        text = "".join(bl.text for bl in msg.content if bl.type == "text")
        sc = parse_scores(text)
        if sc is None:
            by_file[meta.get("file", "?")].append({**meta, "error": "parse", "raw": text[:200]}); bad += 1; continue
        by_file[meta["file"]].append({**meta, **sc})
    outs = []
    for name, rows in by_file.items():
        out = ROOT / "results" / f"judge_scores_{name}.jsonl"
        out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in sorted(rows, key=lambda x: x["case_id"])), encoding="utf-8")
        outs.append(out)
    price = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0), "claude-haiku-5-5": (0.2, 1.0)}.get(st["scorer"], (0, 0))
    print(f"채점 {sum(len(v) for v in by_file.values()) - bad}건, 실패/파싱불가 {bad}건, 토큰 {tin:,}/{tout:,} ≈ ${tin / 1e6 * price[0] + tout / 1e6 * price[1]:.2f} (Batch 할인 전 단가)")
    cmd_report([str(o) for o in outs])
    return 0


def cmd_report(paths: list[str]) -> int:
    print("\n| 결과 파일 | n | ① grounding | ② readability | ③ compliance | 평균 | 3점 이하 비율 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    low_all = []
    for p in paths:
        rows = [r for r in load(Path(p)) if "grounding" in r]
        if not rows:
            print(f"| {Path(p).stem} | 0 | — | — | — | — | — |"); continue
        g = [r["grounding"]["score"] for r in rows]; rd = [r["readability"]["score"] for r in rows]; c = [r["compliance"]["score"] for r in rows]
        avg = statistics.mean(g + rd + c)
        low = [r for r in rows if min(r["grounding"]["score"], r["readability"]["score"], r["compliance"]["score"]) <= 3]
        print(f"| {Path(p).stem.replace('judge_scores_', '')} | {len(rows)} | {statistics.mean(g):.2f} | {statistics.mean(rd):.2f} | {statistics.mean(c):.2f} | {avg:.2f} | {len(low) / len(rows):.0%} |")
        low_all += [(Path(p).stem, r) for r in low]
    if low_all:
        print(f"\n3점 이하 항목이 있는 답변 (상위 15 / 총 {len(low_all)}):")
        for name, r in sorted(low_all, key=lambda x: min(x[1][k]["score"] for k in ("grounding", "readability", "compliance")))[:15]:
            worst = min(("grounding", "readability", "compliance"), key=lambda k: r[k]["score"])
            print(f"  {name.replace('judge_scores_', '')} {r['case_id']} [{r.get('category')}] {worst}={r[worst]['score']}: {r[worst]['reason']}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    if cmd != "report" and not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 (.env)"); sys.exit(2)
    sys.exit({"submit": lambda: cmd_submit(args), "status": cmd_status, "merge": cmd_merge, "report": lambda: cmd_report(args)}.get(cmd, lambda: print(__doc__) or 0)())
