#!/usr/bin/env python3
"""
轻量级 Hindsight Recall 脚本
用法: python hindsight_recall.py "查询内容" [--tokens MAX_TOKENS]
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict
import getpass

HINDSIGHT_URL = "http://125.102.1.12:58888"

class HindsightRecall:
    def __init__(self):
        self.api_url = HINDSIGHT_URL
        self.api_token = ''

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        return h

    def recall(
        self,
        bank_id: str,
        query: str,
        max_tokens: int = 512,
        budget: str = "mid",
    ) -> Dict[str, Any]:
        encoded_bank = urllib.parse.quote(bank_id, safe="")
        url = f"{self.api_url}/v1/default/banks/{encoded_bank}/memories/recall"

        body = json.dumps({
            "query": query,
            "budget": budget,
            "max_tokens": max_tokens,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in self._headers().items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw).get("data", json.loads(raw))
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8"))
                msg = err.get("error", err.get("message", str(e)))
            except Exception:
                msg = str(e)
            return {"error": msg, "status_code": e.code}
        except Exception as e:
            return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Hindsight 轻量级召回")
    parser.add_argument("query", nargs="*", help="查询内容（留空则从 stdin 读取）")
    parser.add_argument("--tokens", type=int, default=512, help="最大 token 数 (default: 512)")
    parser.add_argument("--budget", default="mid", choices=["low", "mid", "high"], help="预算级别")
    args = parser.parse_args()

    if args.query:
        query = " ".join(args.query)
    else:
        query = sys.stdin.read().strip()

    if not query:
        print("错误: 请传入查询内容，或通过管道输入", file=sys.stderr)
        sys.exit(1)
    bank_id = getpass.getuser()

    client = HindsightRecall()
    result = client.recall(
        bank_id=bank_id,
        query=query,
        max_tokens=args.tokens,
        budget=args.budget,
    )

    if "error" in result:
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()