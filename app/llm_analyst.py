import json
from openai import OpenAI

class LLMAnalyst:
    def __init__(self, api_key, model):
        self.client = OpenAI(api_key=api_key) if api_key else None
        self.model = model

    def assess(self, symbol, technical, price, portfolio):
        if not self.client:
            return {"signal": "HOLD", "score": 0.0, "confidence": 0.0,
                    "reason": "OPENAI_API_KEY 미설정: LLM 중립"}
        payload = {"symbol": symbol, "price_krw": price,
                   "indicators": technical, "portfolio": portfolio}
        result = self.client.chat.completions.create(
            model=self.model, temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content":
                 'JSON으로만 답하세요: {"signal":"BUY|HOLD|SELL","score":-1~1,'
                 '"confidence":0~1,"reason":"한국어 근거"}. 입력 데이터 외 추측 금지. '
                 '불확실하면 HOLD.'},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ], timeout=30)
        data = json.loads(result.choices[0].message.content)
        signal = data.get("signal", "HOLD")
        if signal not in ("BUY", "HOLD", "SELL"): signal = "HOLD"
        return {"signal": signal,
                "score": max(-1., min(1., float(data.get("score", 0)))),
                "confidence": max(0., min(1., float(data.get("confidence", 0)))),
                "reason": str(data.get("reason", ""))[:400]}
