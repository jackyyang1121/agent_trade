from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from agent.util import ROOT, PlanError


def load_runtime(path: Path | None = None) -> dict:
    config_path = path or ROOT / "config" / "runtime.yaml"
    value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PlanError("runtime.yaml 必須是 mapping")
    for key in ("llm", "research", "signals", "competition", "forward"):
        if not isinstance(value.get(key), dict):
            raise PlanError(f"runtime.yaml 缺少 {key} 設定")
    return value


def strategy_version(runtime: dict | None = None) -> str:
    """包含所有影響決策的原始碼、設定、研究 prompt 與公開說明書。"""
    import json

    digest = hashlib.sha256()
    paths = sorted((ROOT / "agent").rglob("*.py")) + sorted((ROOT / "config").glob("*.yaml"))
    paths += [ROOT / "ETF投資策略說明文檔.md", ROOT / "(驗證繳交格式)D-Plan.schema_v4.2.json"]
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    if runtime is not None:
        # Runtime 只有非秘密設定；不將環境憑證納入 digest 或快照。
        digest.update(json.dumps(runtime, sort_keys=True, ensure_ascii=False).encode())
    return "sha256:" + digest.hexdigest()[:24]
