# 샘플 (샘플일 뿐입니다 — 실제 환율은 서울외국환중개 매매기준율 일별 데이터로 교체)

## synthetic/ — 합성 고객 300명 (SCRUM-38, F08-B)

`python scripts/gen_synthetic.py` 로 생성. 시드 42 고정 → 같은 파일. 생성기를 고치면 다시 만들어 커밋한다(테스트가 비교함).

| 파일 | 내용 |
| --- | --- |
| `customers.json` | 고객 2001~2300: 시나리오(SINGLE_FIFO/SINGLE_AVG/MULTI_BROKER/TRANSFER/NO_SALE/LOSS_HEAVY), 계좌·증권사·방식, 종목, 2024 lot 이월 여부 |
| `trades.json` | 거래 1,983행. `TRADES_FILE=data/synthetic/trades.json` 으로 API·평가에 바로 사용 |
| `fx_rate.csv` | 2024~2025 일별 매매기준율 (합성 랜덤워크 1,250~1,500) |
| `expected.json` | 고객별 tax_calc 정답: tax_krw·total_gain·by_account·이체 lot 매도·needs_confirmation. 평가셋(F10)의 정답지 |

분포(시드 42): 세액 발생 98 / 손실 98 / 매도 없음 24 / 이체 lot 매도 54 / 선택제(나무) 계좌 49 / 계좌 2개 이상 120.
가격은 실제 시세가 아니다 — 종목별 2년 목표 수익률에 맞춘 합성 경로. 실데이터(`mine/`)와 customer_id 대역이 겹치지 않는다.
