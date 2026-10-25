"""F07-5 유형별 설명 골격 + 코드가 끼워 넣는 고정 문장 (SCRUM-35).

왜 골격인가 (10/25 절제 실험, Notion "평가 결과 1차"):
  프롬프트만 바꿔 문장 모드로 돌리면 가독성은 1.2→3.4로 오르지만, 20건 중 2건이 "방식 미확정" 상태를 빼고 단정했고
  7건이 검색하지 않은 조문을 인용했다. → 설명 순서는 골격으로 고정하고, 상태 문장·숫자·인용은 LLM이 아니라 코드가 넣는다.

골격 = 설명 순서(LLM이 채움). 슬롯 = 코드가 넣는 문장(STATUS_SENTENCES, numbers_sentence). LLM은 슬롯 내용을 다시 쓰지 않는다.
"""
from __future__ import annotations

BROKER_KO = {  # broker_code → 고객에게 읽어 줄 이름
    "MIRAE": "미래에셋증권", "KIWOOM": "키움증권", "KB": "KB증권", "KAKAOPAY": "카카오페이증권", "SHINHAN": "신한투자증권",
    "SAMSUNG": "삼성증권", "KIS": "한국투자증권", "DAISHIN": "대신증권", "TOSS": "토스증권", "NAMUH": "NH투자증권(나무)",
}
METHOD_KO = {"FIFO": "먼저 산 주식부터 판 것으로 보는 방식(선입선출)", "AVG": "평균 매수 단가로 보는 방식(이동평균)"}

# 유형별 설명 순서. 명세서 F07-5 표와 같아야 한다 (test_skeleton_matches_spec).
SKELETONS: dict[str, list[str]] = {
    "TRANSFER_BASIS": [
        "다른 증권사에서 옮겨 온(대체입고) 주식이 포함되어 있다는 사실",
        "옮겨 온 주식은 원래 매수한 증권사의 취득가액이 그대로 적용된다는 설명",
        "그래서 지금 받은 증권사의 계산서와 금액이 다를 수 있다는 설명",
        "[숫자]",
        "신고할 때 어느 금액을 써야 하는지",
    ],
    "MULTI_BROKER": [
        "증권사마다 취득가액 산정 방식이 다를 수 있다는 설명(계좌별 방식)",
        "각 증권사 계산서는 그 계좌 안에서만 계산한 숫자라는 설명",
        "양도소득세는 모든 계좌를 합산해 한 번에 신고한다는 절차",
        "[숫자]",
    ],
    "TAX_BASIS": [
        "세액이 나오는 구조(양도차익 합계에서 기본공제를 빼고 세율을 곱한다)",
        "[숫자]",
        "고객이 예상한 금액과 다를 수 있는 이유(취득가액 방식·환율·수수료)",
    ],
    "FX_BASIS": [
        "양도가액과 취득가액 모두 결제일의 환율로 원화 환산한다는 설명",
        "그래서 달러로는 이익인데 원화로 손실이거나 그 반대가 생길 수 있다는 설명",
        "[숫자]",
    ],
    "OFFSET": [
        "같은 해 해외주식 손실과 이익은 서로 상계된다는 설명",
        "상계 후 남은 금액에서 기본공제를 뺀다는 절차",
        "[숫자]",
    ],
    "FILING": [
        "신고 대상인지와 신고 기간(다음 해 5월 확정신고)",
        "신고 방법(홈택스 또는 증권사 신고대행)",
        "[숫자]",
    ],
}
DEFAULT_SKELETON = ["민원에 대한 직접 답", "[숫자]"]

# 코드가 넣는 상태 문장. LLM은 이 내용을 쓰지 않는다 (중복 방지). {broker}는 조립 시 치환.
STATUS_SENTENCES = {
    "METHOD_UNCONFIRMED": "{broker} 계좌는 취득가액 산정 방식을 고객님께서 선택하시는 증권사라서, 어떤 방식으로 계산할지 확인한 뒤에 정확한 금액을 안내드릴 수 있습니다.",
    "TRANSFER_REVIEW": "{origin}에서 옮겨 온 주식이 포함되어 있어 {origin}에서 매수한 취득가액으로 계산했고, {dest} 계산서와 금액이 다를 수 있습니다. 신고 전에 상담원이 한 번 더 확인해 드리겠습니다.",
    "SHORT": "일부 매도 주식의 매수 내역이 확인되지 않아 정확한 금액 계산이 보류되었습니다. 거래 내역을 확인한 뒤 다시 안내드리겠습니다.",
    "AGENT_ABORTED": "규정 설명은 상담원이 확인한 뒤 안내드리겠습니다.",
}

MAX_CUSTOMER_TEXT = 600  # F07-8
MAX_LLM_PART = 350  # 설명 부분 상한. 숫자·상태·근거 줄을 더해도 600 안에 들도록


def won(n: int | None) -> str | None:
    return None if n is None else f"{int(n):,}원"


def numbers_sentence(figures: dict, tax_year: int) -> str | None:
    """코드가 쓰는 숫자 문장. 천 단위 콤마, 원 단위, 반올림 없음. figures 없으면 None."""
    if not figures or figures.get("tax_krw") is None:
        return None
    gain, ded, taxable, tax = (won(figures.get(k)) for k in ("total_gain_krw", "deduction_krw", "taxable_krw", "tax_krw"))
    if figures.get("total_gain_krw", 0) <= 0:
        return f"{tax_year}년 해외주식 양도차익 합계는 {gain}으로 이익이 없어 납부할 양도소득세는 {tax}입니다."
    if figures.get("taxable_krw", 0) == 0:  # 공제 한도 안 — "공제 1,027,468원을 뺀 과세표준 0원"은 귀로 듣기 어색하다
        return f"{tax_year}년 해외주식 양도차익 합계는 {gain}으로 기본공제 한도 안이라 납부할 양도소득세는 {tax}입니다."
    return (f"{tax_year}년 해외주식 양도차익 합계는 {gain}이고, 기본공제 {ded}을 뺀 과세표준 {taxable}에 "
            f"세율 22%(지방소득세 포함)를 적용한 양도소득세는 {tax}입니다.")


def transfer_pairs(figures: dict) -> tuple[str, str]:
    """이체 lot이 섞인 매도에서 (원 매수 증권사들, 매도 증권사들) 이름. 없으면 일반 표현."""
    origins, dests = set(), set()
    for a in (figures or {}).get("by_account") or []:
        for s in a.get("sales") or []:
            if s.get("has_transferred_lot"):
                dests.add(BROKER_KO.get(a["broker_code"], a["broker_code"]))
                for u in s.get("lots_used") or []:
                    if u.get("transferred"):
                        origins.add(BROKER_KO.get(u["origin_broker_code"], u["origin_broker_code"]))
    return ("·".join(sorted(origins)) or "다른 증권사", "·".join(sorted(dests)) or "지금 보고 계신")


def by_account_sentence(figures: dict) -> str | None:
    """계좌가 2개 이상일 때 계산서별 숫자 한 문장."""
    accts = (figures or {}).get("by_account") or []
    if len(accts) < 2:
        return None
    parts = [f"{BROKER_KO.get(a['broker_code'], a['broker_code'])} {won(a['gain_krw'])}" for a in accts]
    return "증권사별 양도차익은 " + ", ".join(parts) + "입니다."
