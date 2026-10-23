"""SCRUM-42: 템플릿 민원 문장을 Batch API로 자연스럽게 다시 쓴다 (text_source: template → batch).

  python eval/rewrite_texts_batch.py submit   # 배치 제출 → results/batch_rewrite.json에 batch_id 저장
  python eval/rewrite_texts_batch.py status   # 진행 상태
  python eval/rewrite_texts_batch.py merge    # 끝났으면 결과를 eval/cases.jsonl에 반영 (원본은 eval/cases.template.jsonl로 백업)

- 대상: kind=TAX·REG 중 style≠typo. typo 변형은 모델이 오타를 고쳐 버리므로 템플릿 그대로 둔다. OOS 20건도 그대로.
- 1차 실행(10/24) 교훈: '나무'를 증권사로 못 알아보고 '당사/다른 증권사'로 바꾼 6건 → 템플릿으로 되돌림. 증권사명은 '나무증권'처럼 풀어 쓰는 게 안전
- 말투(style)·사실(증권사·종목·숫자·질문 의도)은 유지. 고객 식별값은 애초에 문장에 없다.
- Batch API는 실시간 대비 50% 할인. 180건 × ~400토큰 → Haiku 기준 몇 센트.
- merge 후 반드시 `python eval/validate_cases.py` (expected는 안 바뀌므로 통과해야 정상).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "eval" / "cases.jsonl"
BACKUP = ROOT / "eval" / "cases.template.jsonl"
STATE = ROOT / "results" / "batch_rewrite.json"

MODEL = os.getenv("REWRITE_MODEL", "claude-haiku-5-5")

SYSTEM = """너는 증권사 고객센터에 들어오는 해외주식 양도소득세 민원 문장을 다듬는 사람이다.
주어진 문장을 같은 뜻, 같은 말투로 더 자연스러운 한국어 민원 한 문단(1~3문장)으로 다시 쓴다.
규칙:
- 사실을 바꾸지 않는다: 증권사 이름(나무=NH투자증권의 나무증권, 반드시 '나무' 또는 '나무증권'으로 유지), 종목 이름, 숫자, 질문의 핵심은 그대로.
- style이 formal이면 존댓말·격식체, casual이면 구어체 존댓말, typo면 띄어쓰기 오류·오타 2~3개를 자연스럽게 섞는다(의미는 알아볼 수 있게).
- 새로운 사실(계좌번호, 이름, 금액)을 지어내지 않는다. 답을 쓰지 않는다. 문장만 출력한다."""


def load():
    return [json.loads(l) for l in CASES.read_text(encoding="utf-8").splitlines() if l.strip()]


def client():
    from anthropic import Anthropic
    return Anthropic()


def cmd_submit():
    cases = [c for c in load() if c["kind"] in ("TAX", "REG") and c["text_source"] == "template" and c["style"] != "typo"]  # typo는 템플릿의 의도적 오타를 그대로 둔다 (모델이 고쳐 버림)
    reqs = [{"custom_id": c["case_id"],
             "params": {"model": MODEL, "max_tokens": 300, "system": SYSTEM,
                        "messages": [{"role": "user", "content": f"style: {c['style'] or 'casual'}\n문장: {c['text']}"}]}}
            for c in cases]
    b = client().messages.batches.create(requests=reqs)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"batch_id": b.id, "model": MODEL, "n": len(reqs)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"제출됨 batch_id={b.id}  {len(reqs)}건  model={MODEL}  → 몇 분 뒤 `status`, 끝나면 `merge`")


def cmd_status():
    st = json.loads(STATE.read_text(encoding="utf-8"))
    b = client().messages.batches.retrieve(st["batch_id"])
    print(f"{b.id}: {b.processing_status}  counts={b.request_counts}")


def cmd_merge():
    st = json.loads(STATE.read_text(encoding="utf-8"))
    cl = client()
    b = cl.messages.batches.retrieve(st["batch_id"])
    if b.processing_status != "ended":
        print(f"아직 {b.processing_status}. 잠시 뒤 다시.")
        return 1
    texts, failed, tin, tout = {}, [], 0, 0
    for r in cl.messages.batches.results(st["batch_id"]):
        if r.result.type == "succeeded":
            msg = r.result.message
            texts[r.custom_id] = "".join(bl.text for bl in msg.content if bl.type == "text").strip().strip('"')
            tin += msg.usage.input_tokens; tout += msg.usage.output_tokens
        else:
            failed.append((r.custom_id, r.result.type))
    cases = load()
    if not BACKUP.exists():
        BACKUP.write_text(CASES.read_text(encoding="utf-8"), encoding="utf-8")
    n = 0
    for c in cases:
        t = texts.get(c["case_id"])
        if t and 5 <= len(t) <= 400:
            c["text"], c["text_source"] = t, "batch"; n += 1
    with CASES.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"반영 {n}건  실패 {len(failed)}건 {failed[:5]}  토큰 입력 {tin:,} / 출력 {tout:,}  (원본 백업: {BACKUP.name})")
    print("다음: python eval/validate_cases.py")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY가 없습니다 (.env 확인)"); sys.exit(2)
    sys.exit({"submit": cmd_submit, "status": cmd_status, "merge": cmd_merge}.get(cmd, lambda: print(__doc__))() or 0)
