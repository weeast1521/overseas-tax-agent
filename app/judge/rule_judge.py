"""RuleJudge — 키워드 규칙. API 키 없이 로컬 테스트할 때, 그리고 LocalJudge 비교 기준선으로 쓴다."""
from __future__ import annotations

import re

from app.judge.base import DEFAULT_TOOLS, Judgment


class RuleJudge:
    name = "rule"

    async def classify(self, text: str) -> Judgment:
        t = re.sub(r"\s", "", text)
        if re.search(r"추천|사도|매수해|오를|전망|살까", t):
            return Judgment(category="OUT_OF_SCOPE", confidence=0.9, flags=["ASKS_INVESTMENT_ADVICE"])
        if re.search(r"이체|옮겼|옮긴|대체입고|입고|이관|가져왔", t):
            cat = "TRANSFER_BASIS"
        elif re.search(r"증권사|두군데|두곳|계산서|합산|합쳐|나무|미래에셋|삼성증권|키움|토스", t):
            cat = "MULTI_BROKER"
        elif re.search(r"환율|원화|달러", t):
            cat = "FX_BASIS"
        elif re.search(r"상계|손해|손실|통산", t):
            cat = "OFFSET"
        elif re.search(r"세금|얼마|차익|수익률", t):
            cat = "TAX_BASIS"
        else:
            return Judgment(category="TAX_BASIS", confidence=0.5, required_tools=DEFAULT_TOOLS["TAX_BASIS"])
        return Judgment(category=cat, confidence=0.96, required_tools=DEFAULT_TOOLS[cat])
