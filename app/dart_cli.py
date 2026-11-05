# 개발 담당: 최준혁
"""Inspect DART data without team login, LLM calls or orders."""
import argparse
import json
from pathlib import Path

from .config import get_settings
from .dart import build_dart_service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--output")
    args = parser.parse_args()
    service = build_dart_service(get_settings())
    result = service.collect(args.symbol) if service else {"status": "disabled", "symbol": args.symbol}
    content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
