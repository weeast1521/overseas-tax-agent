# eval/ — 평가셋 (F10)

| 파일 | 역할 |
| --- | --- |
| `cases.jsonl` | 평가 케이스 200건. 러너(SCRUM-43)의 입력 |
| `build_cases.py` | 합성 고객·정답지에서 200건 생성 (시드 고정). 템플릿 문장 |
| `validate_cases.py` | 스키마·분포·정답 일치(현재 tax_calc)·조문 유무 검사. **러너 전 필수** |
| `rewrite_texts_batch.py` | 템플릿 문장을 Batch API로 자연스럽게 (맥에서 submit → status → merge) |
| `cases.template.jsonl` | Batch 반영 전 원본 (merge 때 자동 생성) |

## 구성 (200)

- **세액 120** — TRANSFER_BASIS 40 · MULTI_BROKER 40 · TAX_BASIS 20 · FX_BASIS 10 · OFFSET 10. 고객은 `data/synthetic`, 정답은 `expected.json`(tax_calc).
  이 중 20그룹 × 3건 = 60건은 같은 고객·같은 정답을 세 말투(formal/casual/typo)로 — 표현 민감도 측정(variant_of).
- **규정 55** — 질문 + `expected.articles` [(법, 조문)]. ★조문 번호는 SCRUM-31에서 원문 확인 후 확정. 코퍼스에 없는 조문은 validate가 경고로 나열.
- **범위 밖 25** — 투자 권유·국내주식·부동산·코인·배당·연말정산·앱 문의·잡담 20 + 규정 질문 중 범위 밖 5(가산세·배당·인적공제·펀드 환매·수정신고, 범위 결정 (a) 10/24~25) → `REJECTED`. 신고 절차·과세 대상 질문은 범위 안(`FILING` 유형).

## expected.status 해석

세액 케이스의 status는 **계산 결과만으로** 정한다: 선택제 계좌에 고객 선택값 없음 → `REVIEW/METHOD_UNCONFIRMED`,
이체 lot 포함 매도 → `REVIEW/TRANSFER_REVIEW`, 그 외 `AUTO`. Judge 확신도가 낮아 `REVIEW/LOW_CONFIDENCE`가 나오는 건
틀린 게 아니라 **보정 지표**(SCRUM-22)로 따로 센다. 러너가 그렇게 채점한다.

## 러너·리포트 (SCRUM-43)

```bash
python -m eval.run --config stub                 # 비용 0. 러너 점검·재현성 기준선 (RuleJudge + 대본 LLM)
python -m eval.run --config A --estimate         # 10건만 → 200건 예상 비용·시간
python -m eval.run --config A --tag 20261024     # 본 실행 (opus + haiku). 중단되면 --resume
python -m eval.report results/A_20261024.jsonl   # 지표 7종 + status 분포 + confidence 보정 표 + 실패 목록 → results/report_*.md
python -m eval.report results/A_run1.jsonl --diff results/A_run2.jsonl   # 재현성: 결정(status·reason·category·tax_krw) 불일치 0이어야
```

결과 파일(`results/`)은 git에 올리지 않는다. 보고서 표만 README/노션으로 옮긴다.
stub 기준선(10/24): 숫자 100% · 분류 66% · 차단 20% · 조문 5% · 표현 민감도 55% — 규칙 Judge와 키워드 검색의 한계가 그대로 숫자로 보인다. 구성 A는 이 표와 나란히 놓는다.
