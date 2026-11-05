# 경제사건 추출 및 비교 평가

## 추출 결과와 판단 연결

뉴스 제목에서 대상 기업이 등장하는 절만 분석합니다. 사건 종류, 기업명, 근거 문자열과 원문 위치(`span`), 연도·분기 등의 기간 표현을 기록합니다. 매출과 영업이익의 백분율 변화는 지표·값·단위·방향·전년/전분기 비교 기준으로 나눕니다. 두 비교 기준이 함께 있으면 `ambiguous`로 기록합니다.

문장의 주장 상태는 다음과 같습니다.

| 값 | 의미 | 예시 |
|---|---|---|
| reported | 확정형으로 보도됨. 독립적으로 사실 검증됐다는 의미가 아님 | 영업이익 20% 증가 |
| forecast | 전망·예상 | 영업이익 20% 증가 전망 |
| denied | 부인·부정 | 영업이익 증가 보도 부인 |
| withdrawn | 철회 | 영업이익 증가 전망 철회 |
| uncertain | 가능성·루머 등 | 영업이익 증가 가능성 |
| mixed | 절마다 주장 상태가 다름 | 흑자 전환; 공장 화재 전망 |
| none | 지원하는 경제사건이 없음 | 신제품 공개 |

`reported`가 아닌 사건이 있거나 긍정·부정 사건이 섞인 기사는 직접 뉴스 점수를 주지 않습니다. 이러한 기사는 기존 선택적 뉴스 LLM 입력에서도 제외합니다. 기존 출처 수, 시간 감쇠, 공급 지연, 최대 조정폭 및 DART 대조 조건을 유지합니다.

이 기능은 제목 규칙 기반이며 학습 모델이 아닙니다. 복잡한 부정 범위·인용·기업 간 관계를 완전히 해석하지 못합니다. 근거 위치와 기간 표현은 해석을 점검하기 위한 출력이며 모든 날짜를 표준화한 결과는 아닙니다. DART 대조는 기존과 같이 매출만 지원하며 영업이익 수치를 공시로 자동 검증하지 않습니다.

## 실제 뉴스 검토 목록

수집 결과 JSON에는 기사 원문 URL, 발행 시각, 실제 관측 시각과 공급자 정보가 있어야 합니다.

```bash
python -m app.news_cli --provider gnews --symbol 005930 --output data/news_005930.json
python -m app.event_evaluation prepare --reports data/news_005930.json --output data/annotation_queue.json
```

검토 목록의 `expected`는 비어 있으며 모델 예측은 포함하지 않습니다. 평가자는 제목만 읽고 별도로 정답을 작성합니다. 예를 들어 합성 문장 `삼성전자 영업이익 20% 증가`의 정답은 다음과 같습니다.

```json
{
  "annotator": "실제 검토자 식별자",
  "split": "development",
  "expected": {
    "events": ["earnings_improvement"],
    "assertion": "reported",
    "figures": [
      {"metric": "operating_profit", "value": 20, "unit": "percent", "direction": "increase", "comparison_basis": "unspecified"}
    ]
  }
}
```

위 항목은 각 기사의 기존 필드에 추가합니다. 사건이나 수치가 없으면 빈 배열을 사용합니다. 실제 뉴스의 정답을 추출기의 예측으로 채우지 않습니다. 충분한 수량을 별도 검토한 뒤 평가하고, 규칙 개선에 사용한 기사는 `development`로 두며 최종 평가를 위한 `holdout`과 분리합니다. 검토자·분할 이름 자체가 독립적인 평가를 보장하지는 않습니다.

```bash
python -m app.event_evaluation evaluate --dataset data/annotation_queue.json --split holdout --output data/real_event_evaluation.json
```

정답 미작성 기사는 채점에서 제외합니다. 채점 대상이 없으면 `awaiting_labels`, 정확도와 F1은 `null`로 출력합니다. 사건 종류와 수치 튜플의 micro precision/recall/F1, 주장 상태 혼동행렬, 기사별 완전 일치율 및 오답을 제공합니다. 같은 종목의 동일 URL 또는 동일 제목은 개발/평가 분할을 가로질러 중복될 수 없습니다. 표현이 다른 전재기사는 추가 수동 검토가 필요합니다.

기사 제목이 포함된 실제 검토 자료는 개인 실행 폴더 `data/`에 보관하고 공개 저장소에 포함하지 않습니다.

## 같은 입력의 기능별 비교

```bash
python -m app.policy_comparison --dataset examples/policy_comparison.json --output data/comparison_demo.json
python -m app.policy_comparison --database data/trading.db --output data/comparison_recorded.json
```

정상적인 종목 분석 사이클에서 `research_input`과 `policy_comparison`을 기존 `policy_decisions` 기록에 추가합니다. `RESEARCH_COMPARISON_ENABLED=false`로 끌 수 있습니다. 예전 기록에 해당 입력이 없으면 비교에서 제외합니다.

세 가지 조건은 동일한 정량 점수, 기사, 시각 및 설정을 사용합니다.

1. 가격 지표만 사용
2. 가격 지표와 뉴스 사용
3. 가격 지표와 뉴스, DART 수치 대조 사용

뉴스의 영향을 분리하기 위해 세 조건 모두 LLM을 사용하지 않으며 연구용 뉴스 정책은 활성화합니다. 따라서 실제 운용에서 LLM을 사용하거나 뉴스 정책을 껐다면 연구용 판단과 실제 판단이 다를 수 있습니다. 정책 임계값·출처 수·감쇠 등 비교에 쓰는 설정은 입력과 함께 저장합니다. 이 기능은 실제 주문에 사용하는 `decision`을 대체하지 않고 추가 API 호출도 하지 않습니다. 연구 기록 실패는 매매 정책을 중단시키지 않습니다.

결과에는 BUY/SELL/HOLD 분포와 뉴스·DART로 판단이 바뀐 횟수가 포함됩니다. 수익률·승률·체결비용이나 인과 효과를 계산한 백테스트가 아닙니다. 과거 캔들로 정량 점수를 만들 때 미래 자료를 섞지 않았는지는 입력 생성자가 확인해야 하며, 현재 DART 응답으로 정정 전 과거 공시를 복원할 수 없습니다.

## 합성 진단

```bash
python -m app.event_evaluation evaluate --dataset examples/event_diagnostics.json --split development --output data/event_diagnostics.json
```

합성 진단 예문은 규칙을 개발·점검하기 위한 자료입니다. 실제 뉴스의 독립 평가나 투자 성과로 해석하지 않습니다. 실제 뉴스 정답 검토가 끝나기 전에는 실제 뉴스 정확도를 보고하지 않습니다.
