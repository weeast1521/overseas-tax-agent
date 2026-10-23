"""SCRUM-42 (F10-1~4): 평가셋 200건 생성 → eval/cases.jsonl

  python eval/build_cases.py [--out eval/cases.jsonl]   # 시드 고정, 같은 출력. ★Batch로 다듬은 cases.jsonl을 덮어쓰니 주의

구성 (200):
  세액 120  TRANSFER_BASIS 40 · MULTI_BROKER 40 · TAX_BASIS 20 · FX_BASIS 10 · OFFSET 10
            고객은 data/synthetic에서 시나리오별로 고르고, expected.figures는 expected.json(=tax_calc 정답)에서 가져온다.
            이 중 20개 기준 케이스는 3가지 말투(격식·구어·오타)로 변형해 variant_of로 묶는다 → 20그룹×3 = 60건이 120건 안에 포함.
  규정 60   질문 + expected.articles(법·조문). 조문은 REG_QUESTIONS에 손으로 적었다. ★전부 원문 확인 전(SCRUM-31)★
  범위 밖 20 투자 권유·국내주식·부동산·배당·잡담 → expected.status=REJECTED

케이스 스키마:
  {case_id, variant_of, kind: TAX|REG|OOS, style: formal|casual|typo|null, customer_id, text, text_source: template|batch,
   category, expected: {status, review_reason?, figures?: {tax_krw, total_gain_krw, by_account: [{account_id, gain_krw}]},
                        articles?: [[law, article], ...]}}
문장은 템플릿으로 먼저 만들고(text_source=template), eval/rewrite_texts_batch.py 가 Batch API로 자연스럽게 다시 쓴다(batch).
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYN = ROOT / "data" / "synthetic"
OUT = ROOT / "eval" / "cases.jsonl"

BROKER_KO = {"MIRAE": "미래에셋", "KIWOOM": "키움", "KB": "KB증권", "KAKAOPAY": "카카오페이증권", "SHINHAN": "신한투자증권",
             "SAMSUNG": "삼성증권", "KIS": "한국투자증권", "DAISHIN": "대신증권", "TOSS": "토스증권", "NAMUH": "나무"}
TICKER_KO = {"AAPL": "애플", "MSFT": "마이크로소프트", "NVDA": "엔비디아", "TSLA": "테슬라", "AMZN": "아마존", "GOOGL": "구글", "META": "메타",
             "AMD": "AMD", "PLTR": "팔란티어", "COIN": "코인베이스", "LCID": "루시드", "RIVN": "리비안", "INTC": "인텔", "SOXL": "SOXL", "SCHD": "SCHD"}

# 세액 문장 템플릿: {a}/{b} 증권사, {t} 종목. style별로 3개씩 → variant 생성에 그대로 쓴다
T = {
    "TRANSFER_BASIS": {
        "formal": ["{a}에서 매수한 {t}를 {b}로 대체입고한 뒤 매도했습니다. {b} 계산서의 취득가액이 실제 매수가와 달라 확인 부탁드립니다.",
                   "타사({a})에서 이관해 온 {t} 주식을 {b}에서 양도했는데 양도차익이 과대 계상된 것 같습니다. 취득가액 산정 기준을 알려주십시오.",
                   "{a}→{b}로 주식을 옮긴 후 매도한 건의 취득일과 취득가액이 어떻게 반영되는지, 올해 세액은 얼마인지 문의드립니다."],
        "casual": ["{a}에서 산 {t} {b}로 옮겨서 팔았는데요, {b} 계산서에 산 가격이 이상하게 찍혀 있어요. 왜 그래요?",
                   "{t} 다른 증권사({a})에서 넘겨받은 거 팔았더니 수익이 엄청 크게 잡혔어요. 원래 산 값은 어디 간 거예요?",
                   "{a}에서 {b}로 주식 넘기고 판 건데 세금 얼마 나와요? 계산서 취득가가 0원이던데"],
        "typo": ["{a}에서 산 {t} {b}로 옴겨서 팔았는데 계산서 취득가액이 이상해요 확인좀요",
                 "타사 대체입고한 {t} 매도했는데 양도차익이 넘 크게 잡혔어요 매수가 반영안된거 같은데",
                 "{a}->{b} 이관후 매도한거 취득가 어떻게 되나요 세금얼마에요"],
    },
    "MULTI_BROKER": {
        "formal": ["{a}과 {b} 두 곳에서 해외주식을 매도했습니다. 계산서가 각각 발급되었는데 합산 시 세액이 얼마인지 문의드립니다.",
                   "{a}, {b} 양사의 양도소득 계산서에 기본공제 250만 원이 각각 반영되어 있습니다. 이대로 신고해도 되는지 확인 부탁드립니다.",
                   "두 증권사({a}, {b}) 거래를 합산 신고해야 하는 것으로 알고 있습니다. 합산 기준 양도소득금액과 세액을 알려주십시오."],
        "casual": ["{a}이랑 {b} 두 군데서 팔았는데 계산서가 따로 와요. 합치면 세금 얼마예요?",
                   "{a} {b} 계산서 둘 다 250만원 공제돼 있던데 이거 그냥 내면 되나요?",
                   "증권사 두 곳에서 {t} 팔았는데 어디 걸로 신고해야 돼요? 둘 다 신고대행 신청해도 되나요"],
        "typo": ["{a}랑 {b} 두군데서 팔앗는데 계산서 따로따로 와요 합치면 세금 얼마에요",
                 "{a} {b} 둘다 250 공제되있는데 이대로 내도되나요",
                 "증권사 두곳 거래 합산 어떻게해요 세금 얼마나오는지도요"],
    },
    "TAX_BASIS": {
        "formal": ["{a} 계좌에서 {t}를 매도했습니다. 올해 귀속 해외주식 양도소득세가 얼마인지 산출 근거와 함께 알려주십시오.",
                   "앱에 표시된 수익과 계산서상 양도차익이 다릅니다. 선입선출 적용 등 계산 방식 설명과 세액을 부탁드립니다.",
                   "{t} 매도분에 대한 양도차익과 기본공제 적용 후 세액이 얼마인지 문의드립니다."],
        "casual": ["{a}에서 {t} 팔았는데 올해 세금 얼마 나와요?",
                   "앱에서 본 수익이랑 계산서 양도차익이 왜 달라요? 세금은 얼마예요",
                   "{t} 여러 번 나눠 샀다가 팔았는데 어떤 게 팔린 걸로 계산돼요? 세금도 알려주세요"],
        "typo": ["{a}에서 {t} 팔았는데 세금 얼마나와요",
                 "앱 수익이랑 계산서 양도차익 왜달라요 세금얼마에요",
                 "{t} 나눠산거 팔았는데 어떤게 팔린걸로 되나요 세금은요"],
    },
    "FX_BASIS": {
        "formal": ["{t}를 달러 기준으로는 손실 또는 소폭 이익인데 원화 환산 양도차익이 발생했다고 합니다. 적용 환율 기준일과 세액을 알려주십시오.",
                   "양도가액과 취득가액 환산에 어느 날짜의 환율이 적용되는지, 그에 따른 올해 세액은 얼마인지 문의드립니다.",
                   "환차익이 양도소득에 포함되는지와 {a} 계좌 매도분 세액을 확인 부탁드립니다."],
        "casual": ["{t} 달러로는 거의 본전인데 원화로 계산하니 이익이라고 세금 내래요. 환율 어느 날 걸 쓰는 거예요?",
                   "환율 때문에 세금이 늘었다는데 이해가 안 돼요. 올해 세금 얼마예요?",
                   "환전도 안 했는데 환차익이 세금에 들어가요? {a} 계좌 세금 얼마예요"],
        "typo": ["{t} 달러로는 본전인데 원화로 이익이라고 세금내래요 환율 언제꺼에요",
                 "환율때문에 세금 늘었다는데 이해안가요 세금 얼마에요",
                 "환전안했는데 환차익 세금들어가요 {a} 세금얼마에요"],
    },
    "OFFSET": {
        "formal": ["{t}에서 손실, 다른 종목에서 이익이 발생했습니다. 손익통산 후 양도소득금액과 세액이 얼마인지 알려주십시오.",
                   "올해 해외주식 매도분 중 손실 종목이 있습니다. 이익과 상계되는지, 상계 후 세액은 얼마인지 문의드립니다.",
                   "손실이 이익보다 큰 경우 세금이 없는지, 남은 손실은 내년으로 넘어가는지 확인 부탁드립니다."],
        "casual": ["{t}는 손해 보고 다른 건 이익인데 서로 빼고 세금 매기는 거 맞죠? 올해 세금 얼마예요?",
                   "손실 난 종목이랑 이익 난 종목 합쳐서 계산되나요? 그러면 세금 얼마예요",
                   "손실이 더 크면 세금 안 내도 돼요? 남은 손실은 내년에 써요?"],
        "typo": ["{t} 손해보고 다른건 이익인데 서로 빼고 세금매기는거 맞죠 세금얼마에요",
                 "손실난거랑 이익난거 합쳐서 계산되요 세금 얼마",
                 "손실이 더크면 세금안내도되요 남은손실 내년에 써요"],
    },
}

# 규정 질문 60건. articles = [(law, article)]. ★조문 번호는 SCRUM-31에서 원문으로 확정. 틀리면 여기와 corpus를 같이 고친다★
REG = [
    # 과세 대상·범위
    ("해외주식 양도차익은 어떤 세금 대상인가요? 국내주식이랑 다른가요", [("소득세법", "제118조의2")]),
    ("미국에 상장된 ETF를 팔아서 번 돈도 해외주식 양도세 대상인가요", [("소득세법", "제118조의2")]),
    ("해외주식은 소액주주여도 양도세를 내야 하나요? 국내주식은 안 내던데", [("소득세법", "제118조의2"), ("소득세법", "제94조")]),
    ("외국 기업이 국내 거래소에 상장한 주식(DR 포함)은 해외주식 양도세인가요", [("소득세법", "제118조의2")]),
    # 양도가액·취득가액·필요경비
    ("양도가액은 체결가인가요 실제 입금액인가요", [("소득세법", "제118조의3")]),
    ("매수 수수료랑 매도 수수료가 필요경비로 빠지나요", [("소득세법", "제118조의4"), ("소득세법", "제97조")]),
    ("취득가액은 어떻게 정해지나요? 평균단가인가요 실제 산 가격인가요", [("소득세법", "제118조의4")]),
    ("다른 증권사에서 옮겨 온 주식의 취득가액은 원래 산 가격인가요 옮긴 날 시가인가요", [("소득세법", "제118조의4"), ("국세청 해석", "서면-2022-국제세원-0764")]),
    ("증권사 계산서에 취득가액이 0원으로 나오는 건 왜 그런가요", [("증권사 신고대행 FAQ", "Q7"), ("소득세법", "제118조의4")]),
    ("미국 현지에서 낸 세금(SEC fee 등)도 경비로 인정되나요", [("소득세법", "제118조의4"), ("소득세법", "제97조")]),
    # 취득시기·선입선출
    ("같은 종목을 여러 번 샀을 때 어떤 것부터 팔린 걸로 보나요", [("소득세법 시행령", "제162조")]),
    ("선입선출법이 법에 정해진 건가요 증권사 마음대로인가요", [("소득세법 시행령", "제162조")]),
    ("이동평균법으로 계산해 주는 증권사도 있던데 뭐가 맞나요", [("소득세법 시행령", "제162조"), ("증권사 신고대행 FAQ", "Q7")]),
    ("양도일은 체결일인가요 결제일인가요", [("소득세법 시행령", "제162조")]),
    ("취득일이 불분명한 주식은 어떻게 처리하나요", [("소득세법 시행령", "제162조")]),
    # 환율
    ("양도가액을 원화로 바꿀 때 어느 날 환율을 쓰나요", [("소득세법 시행령", "제178조의5")]),
    ("취득가액 환율은 매수일인가요 결제일인가요", [("소득세법 시행령", "제178조의5")]),
    ("기준환율은 어디서 고시하는 걸 쓰나요", [("소득세법 시행령", "제178조의5")]),
    ("달러로는 손해인데 원화로 이익이면 세금을 내나요", [("소득세법 시행령", "제178조의5"), ("소득세법", "제118조의3")]),
    ("환전해서 생긴 환차익은 따로 과세되나요", [("소득세법 시행령", "제178조의5")]),
    # 통산·공제·세율
    ("해외주식 이익과 손실은 서로 상계되나요", [("소득세법", "제102조")]),
    ("국내주식 손실과 해외주식 이익을 합칠 수 있나요", [("소득세법", "제102조")]),
    ("올해 손실이 더 크면 내년 이익에서 빼 주나요", [("소득세법", "제102조")]),
    ("기본공제 250만 원은 1년에 한 번인가요 증권사마다인가요", [("소득세법", "제103조"), ("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3")]),
    ("기본공제는 국내주식과 해외주식 각각 받나요", [("소득세법", "제103조")]),
    ("해외주식 양도세율이 22%인 이유가 뭔가요", [("소득세법", "제104조"), ("지방세법", "제103조의3")]),
    ("양도차익이 250만 원 이하면 신고를 안 해도 되나요", [("소득세법", "제103조"), ("소득세법", "제110조")]),
    ("세액 계산 순서를 알려주세요. 공제 먼저인가요 세율 먼저인가요", [("소득세법", "제103조"), ("소득세법", "제104조")]),
    # 신고·납부
    ("해외주식 양도세는 언제 신고하나요", [("소득세법", "제110조")]),
    ("예정신고도 해야 하나요", [("소득세법", "제105조"), ("소득세법", "제110조")]),
    ("증권사 신고대행을 신청하면 제가 따로 할 게 없나요", [("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3"), ("증권사 신고대행 FAQ", "Q7")]),
    ("증권사 두 곳에서 거래했는데 신고대행은 한 곳만 되나요", [("증권사 신고대행 FAQ", "Q7"), ("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3")]),
    ("신고를 안 하면 가산세가 얼마나 붙나요", [("국세기본법", "제47조의2"), ("국세기본법", "제47조의4")]),
    ("신고 기한을 넘겼는데 지금이라도 할 수 있나요", [("국세기본법", "제45조의3"), ("국세기본법", "제47조의2")]),
    ("홈택스에서 직접 신고하려면 뭐가 필요한가요", [("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3")]),
    ("양도소득세와 지방소득세는 따로 내나요", [("지방세법", "제103조의3"), ("소득세법", "제104조")]),
    # 외국납부세액·기타
    ("미국에서 양도세를 냈으면 한국에서 공제되나요", [("소득세법", "제118조의5")]),
    ("배당금에 대한 세금과 양도세는 다른 건가요", [("소득세법", "제118조의2"), ("소득세법", "제17조")]),
    ("해외주식 양도소득도 종합소득에 합산되나요", [("소득세법", "제118조의2"), ("소득세법", "제4조")]),
    ("부양가족이 해외주식으로 번 돈이 있으면 인적공제에 영향이 있나요", [("소득세법", "제50조"), ("소득세법", "제118조의2")]),
    # 상황별 (계산기 없이 규정만)
    ("증권사 계산서가 두 장인데 어느 게 맞나요", [("증권사 신고대행 FAQ", "Q7"), ("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3")]),
    ("여러 증권사 거래는 어떻게 합산 신고하나요", [("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3")]),
    ("증권사마다 계산 방식이 다른데 세무서는 어떤 걸 인정하나요", [("소득세법 시행령", "제162조"), ("증권사 신고대행 FAQ", "Q7")]),
    ("이체한 주식의 보유기간은 처음 산 날부터인가요", [("소득세법 시행령", "제162조"), ("소득세법", "제118조의4")]),
    ("무상증자나 액면분할로 받은 주식의 취득가액은요", [("소득세법 시행령", "제163조")]),
    ("증여받은 해외주식을 팔면 취득가액은 뭐로 보나요", [("소득세법 시행령", "제163조"), ("소득세법", "제97조")]),
    ("상속받은 해외주식 양도세 취득가액은요", [("소득세법 시행령", "제163조")]),
    ("미국 주식 공매도나 옵션 손익도 양도세인가요", [("소득세법", "제118조의2"), ("소득세법", "제94조")]),
    ("해외주식형 펀드 환매 차익은 양도세인가요 배당소득인가요", [("소득세법", "제17조"), ("소득세법", "제118조의2")]),
    ("소수점 거래(0.5주)도 양도세 계산에 들어가나요", [("소득세법", "제118조의2"), ("소득세법", "제118조의3")]),
    ("해외주식 양도세 신고에 필요한 서류가 뭔가요", [("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3")]),
    ("세무대리인 없이 혼자 신고해도 되나요", [("국세청 해외주식 양도소득세 신고안내", "Ⅱ-3"), ("소득세법", "제110조")]),
    ("양도세 납부는 분할납부가 되나요", [("소득세법", "제112조")]),
    ("환급이 나올 수도 있나요", [("소득세법", "제110조")]),
    ("작년에 신고를 잘못했으면 어떻게 고치나요", [("국세기본법", "제45조"), ("국세기본법", "제45조의2")]),
    ("증권사 계산서에 수수료가 안 보이는데 경비로 빠진 건가요", [("소득세법", "제118조의4"), ("증권사 신고대행 FAQ", "Q7")]),
    ("매도 후 바로 재매수하면 세금에 영향이 있나요", [("소득세법 시행령", "제162조")]),
    ("해외주식 양도세 비과세나 감면은 없나요", [("소득세법", "제103조"), ("소득세법", "제118조의7")]),
    ("1년 미만 보유와 장기 보유 세율이 다른가요", [("소득세법", "제104조")]),
    ("미성년 자녀 계좌 해외주식 양도세는 누가 신고하나요", [("소득세법", "제118조의2"), ("소득세법", "제110조")]),
]

OOS = [
    "테슬라 지금 사도 될까요? 추천해 주세요", "엔비디아 전망 어떻게 보세요, 더 오를까요", "지금 가진 애플 팔아야 할까요 들고 가야 할까요",
    "어떤 해외주식이 세금 적게 나오면서 수익 좋은가요 종목 추천요", "삼성전자 국내주식 양도세는 어떻게 되나요", "국내 상장 ETF 매매차익 세금 알려주세요",
    "아파트 팔았는데 양도세 얼마예요", "비트코인 수익 세금은 어떻게 신고하나요", "해외주식 배당금 세금은 어떻게 신고하나요",
    "미국 주식 배당 원천징수 15% 환급받을 수 있나요", "연말정산 때 해외주식 관련 공제 뭐 있어요", "종합소득세 신고 기한이 언제예요",
    "증권사 앱 로그인이 안 돼요", "해외주식 거래 수수료 이벤트 있나요", "환전 우대율 어떻게 받아요",
    "오늘 날씨 좋네요 상담원님 식사하셨어요", "미국 금리 인하되면 주식 오르나요", "달러 지금 환전해 두는 게 좋을까요",
    "ISA 계좌로 해외주식 사면 세금 안 내나요", "퇴직연금 계좌에서 해외 ETF 사도 양도세 내나요",
]


def status_for(exp: dict) -> tuple[str, str | None]:
    if exp["needs_confirmation"]:
        return "REVIEW", "METHOD_UNCONFIRMED"
    if exp["transferred_lot_sales"]:
        return "REVIEW", "TRANSFER_REVIEW"
    return "AUTO", None


def figures_for(exp: dict) -> dict:
    return {"tax_krw": exp["tax_krw"], "total_gain_krw": exp["total_gain_krw"],
            "by_account": [{"account_id": a["account_id"], "gain_krw": a["gain_krw"]} for a in exp["by_account"]]}


def fill(tpl: str, cust: dict) -> str:
    accs = cust["accounts"]
    a = BROKER_KO[accs[0]["broker_code"]]
    b = BROKER_KO[accs[1]["broker_code"]] if len(accs) > 1 else BROKER_KO[[k for k in BROKER_KO if k != accs[0]["broker_code"]][0]]
    t = TICKER_KO[cust["tickers"][0]]
    return tpl.format(a=a, b=b, t=t)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    out_path = Path(ap.parse_args().out)
    rng = random.Random(42)
    customers = {c["customer_id"]: c for c in json.loads((SYN / "customers.json").read_text(encoding="utf-8"))}
    expected = json.loads((SYN / "expected.json").read_text(encoding="utf-8"))["customers"]

    def pick(pred, n):
        pool = sorted(cid for cid, c in customers.items() if pred(c, expected[str(cid)]))
        rng.shuffle(pool)
        assert len(pool) >= n, (n, len(pool))
        return pool[:n]

    has_sales = lambda e: e["n_sales"] > 0  # noqa: E731
    plan = {  # category → (고객 수, 조건). TRANSFER/MULTI는 20고객 = 기준 케이스 그룹 후보도 겸함
        "TRANSFER_BASIS": pick(lambda c, e: c["scenario"] == "TRANSFER" and e["transferred_lot_sales"], 30),
        "MULTI_BROKER": pick(lambda c, e: c["scenario"] == "MULTI_BROKER" and len(e["by_account"]) >= 2, 30),
        "TAX_BASIS": pick(lambda c, e: c["scenario"] in ("SINGLE_FIFO", "SINGLE_AVG", "NO_SALE"), 20),
        "FX_BASIS": pick(lambda c, e: c["scenario"] in ("SINGLE_FIFO", "SINGLE_AVG") and has_sales(e), 10),
        "OFFSET": pick(lambda c, e: c["scenario"] in ("LOSS_HEAVY", "MULTI_BROKER") and e["total_gain_krw"] < 0, 10),
    }
    # TRANSFER·MULTI 각 20고객: 앞 10고객은 3가지 말투로 3건씩(variant 그룹 10개씩 = 20그룹), 뒤 10고객은 1건 → 각 40건
    plan["TRANSFER_BASIS"] = plan["TRANSFER_BASIS"][:20]
    plan["MULTI_BROKER"] = plan["MULTI_BROKER"][:20]

    cases, n = [], 0

    def add(kind, category, cust_id, text, exp, style=None, variant_of=None):
        nonlocal n
        n += 1
        cid = f"C{n:03d}"
        case = {"case_id": cid, "variant_of": variant_of, "kind": kind, "style": style, "customer_id": cust_id,
                "text": text, "text_source": "template", "category": category, "expected": exp}
        cases.append(case)
        return cid

    for category, custs in plan.items():
        group_custs = custs[:10] if category in ("TRANSFER_BASIS", "MULTI_BROKER") else []
        for cust_id in custs:
            c, e = customers[cust_id], expected[str(cust_id)]
            st, rr = status_for(e)
            exp = {"status": st, **({"review_reason": rr} if rr else {}), "figures": figures_for(e)}
            if cust_id in group_custs:
                base = None
                for style in ("formal", "casual", "typo"):
                    tpl = rng.choice(T[category][style])
                    cid = add("TAX", category, cust_id, fill(tpl, c), exp, style=style, variant_of=base)
                    base = base or cid
            else:
                style = rng.choice(["formal", "casual"])
                add("TAX", category, cust_id, fill(rng.choice(T[category][style]), c), exp, style=style)

    for q, arts in REG:
        add("REG", "REG", None, q, {"status": "AUTO", "articles": [list(a) for a in arts]})
    for q in OOS:
        add("OOS", "OUT_OF_SCOPE", None, q, {"status": "REJECTED"})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    from collections import Counter
    print(f"{len(cases)}건 → {out_path}")
    print("kind:", dict(Counter(c["kind"] for c in cases)), " category:", dict(Counter(c["category"] for c in cases)))
    groups = Counter(c["variant_of"] for c in cases if c["variant_of"])
    print(f"variant 그룹 {len(groups)}개 (각 {set(v + 1 for v in groups.values())}건)  expected.status:", dict(Counter(c["expected"]["status"] for c in cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
