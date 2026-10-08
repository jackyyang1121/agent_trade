from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.checks import schema_errors
from agent.pipeline import run
from agent.util import PlanError, parse_date


def main() -> None:
    if len(sys.argv) == 1:
        sys.argv.append("run")
    parser = argparse.ArgumentParser(description="產生當日 D-Plan 與記帳")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_parser = sub.add_parser("run", help="結算上一筆委託並產生下一個交易日的決策書")
    run_parser.add_argument("--date", help="指定交易日 YYYY-MM-DD；省略則依台北時間自動判斷")
    check_parser = sub.add_parser("check", help="只做 D-Plan schema 結構檢查")
    check_parser.add_argument("path")
    args = parser.parse_args()
    try:
        if args.cmd == "run":
            run(parse_date(args.date) if args.date else None)
        else:
            document = json.loads(Path(args.path).read_text(encoding="utf-8"))
            errors = schema_errors(document)
            if errors:
                raise PlanError("\n".join(errors[:40]))
            print(f"{args.path} 通過 schema")
    except PlanError as exc:
        print(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
