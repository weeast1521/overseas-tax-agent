# data/corpus/raw — 원문 보관 (SCRUM-31)

`python scripts/build_corpus.py --build` 가 이 폴더를 읽어 `data/corpus/v1/` chunk를 만든다.

| 파일 | 어디서 | 어떻게 |
| --- | --- | --- |
| `law_*.json` | 법제처 Open API | `LAW_OC=... python scripts/build_corpus.py --fetch` 가 자동 저장 |
| `nts_guide.txt` | 국세청 해외주식 양도소득세 신고안내 (nts.go.kr) | 브라우저에서 복사해 붙여 넣기 |
| `nts_ruling.txt` | 국세법령정보시스템 서면-2022-국제세원-0764 | 질의·회신 본문 복사 |
| `broker_faq.txt` | 미래에셋 신고대행 안내 PDF, NH 나무 FAQ | 해당 항목 복사 (**git에 올리지 않음**) |
| `<아무이름>.txt` | API가 안 될 때 법령 조문을 직접 붙여 넣기 | 머리에 `# law:` `# doc_type:` 추가 |

## 붙여 넣기 형식

```
# source_url: https://www.nts.go.kr/...
# effective_date: 2025-05-01
# tax_year_scope: 2024 귀속
## 환율 기준
(여기에 원문을 그대로 붙여 넣기)
## 손익 통산
(원문)
```

- `## 제목` 하나가 article 하나. 법령 조문이면 `## 제118조의2(국외자산 양도소득의 범위)` 처럼 조 번호로 시작.
- 요약하거나 고쳐 쓰지 말고 원문 그대로. 코퍼스는 답변의 '근거'라서 원문과 다르면 검증이 의미 없다.
- `★` 문자는 넣지 않는다 (자리표시 검사에 걸림).
