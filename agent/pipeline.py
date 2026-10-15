"""舊入口的相容層；每日決策統一由 orchestration 管理。

不再將產生的委託預掛正式帳本，亦不以自行模擬結算取代主辦庫存。
"""
from __future__ import annotations

import json
import warnings
from datetime import date
from pathlib import Path
from agent.util import PlanError


def code_version() -> str:
    from agent.orchestration.configuration import strategy_version
    return strategy_version()


def run(trade_day: date | None = None) -> Path:
    from agent.orchestration.daily import daily
    warnings.warn('run() 現在預設為 shadow；正式競賽請使用 daily --mode competition --live', DeprecationWarning, stacklevel=2)
    summary = daily(mode='shadow', trade_date=trade_day)
    return Path(summary['portfolios']['H2']['path'])


def check_file(path: str) -> None:
    from agent.compliance import structural_errors
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    errors = structural_errors(document)
    if errors:
        raise PlanError('\n'.join(errors))
    print(f'{path} Schema 與引用鏈通過；未提供帳本，不能宣稱完整交易合規。')
