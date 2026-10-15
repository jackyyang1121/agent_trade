"""人讀報告與內部 checkpoint 分開；不把演練當成正式送件。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent.util import ROOT, PlanError
from .io import atomic_json


def output_roots(state_override: Path | None) -> tuple[Path, Path]:
    if state_override is not None:
        return state_override.resolve() / "reports", state_override.resolve() / "DataPack"
    return ROOT / "決策報告繳交紀錄", ROOT / "DataPack"


def publish(summary: dict, artifact_dir: Path, reports: Path) -> None:
    mode = "離線測試" if summary["synthetic"] and summary["mode"] != "rehearsal" else {
        "competition": "正式", "rehearsal": "演練", "shadow": "影子", "personal": "個人"}[summary["mode"]]
    destination = reports / mode / summary["trade_date"] / summary["run_id"]
    destination.mkdir(parents=True, exist_ok=True)
    for portfolio in summary["portfolios"].values():
        internal = Path(portfolio["path"])
        public = destination / internal.name
        atomic_json(public, json.loads(internal.read_text(encoding="utf-8")), immutable=True)
        atomic_json(public.with_suffix(".context.json"),
                    json.loads(internal.with_suffix(".context.json").read_text(encoding="utf-8")), immutable=True)
        portfolio["path"] = str(public)
    shutil.copyfile(artifact_dir / "研究報告.md", destination / "研究報告.md")
    summary["report"] = str(destination / "研究報告.md")
    atomic_json(destination / "執行摘要.json", summary, immutable=True)


def clean_unresearched_shadow(root: Path) -> dict:
    """移除已明確判定無新模型研究的 Shadow 報告，保留帳本與證據庫。

    無法辨識的資料、正式資料、SQLite、來源與凍結快照均不刪。
    tombstone 避免舊日執行被當作成功報告重用。
    """
    if root.name != "shadow" or not root.is_dir() or root.is_symlink():
        if not root.exists():
            return {"root": str(root), "removed": [], "note": "目錄不存在"}
        raise PlanError("清理範圍僅限非符號連結的 shadow 目錄")
    removed = []
    for path in sorted(root.rglob("summary.json")):
        if path.is_symlink() or any((root / p).is_symlink() for p in path.relative_to(root).parents if p != Path('.')):
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            metadata = summary.get("research", {})
            if summary.get("mode") != "shadow" or metadata.get("used_llm") or metadata.get("new_research"):
                continue
            for child in path.parent.iterdir():
                if child.is_symlink() or not child.is_file():
                    continue
                if (child.name.startswith("D-Plan_") and child.suffix == ".json") or child.name == "研究報告.md":
                    child.unlink()
                    removed.append(str(child))
            atomic_json(path.parent / "CLEANED.json", {"reason": "無新模型研究的舊 Shadow 報告已清除",
                                                       "preserved": "來源、快照、帳本、summary 保留供追查"})
        except (ValueError, OSError, AttributeError):
            continue
    # 中斷的舊執行可能尚無 summary，只清除模型欄位明確表示量化／Mock 的 JSON。
    for path in sorted(root.rglob("D-Plan_*.json")):
        if path.is_symlink() or any((root / p).is_symlink() for p in path.relative_to(root).parents if p != Path('.')):
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            model = document.get("agent_metadata", {}).get("model_version")
            if document.get("doc_type") != "D-Plan" or model not in {"deterministic-quant-no-new-llm", "mock", "synthetic"}:
                continue
            path.unlink()
            removed.append(str(path))
            context = path.with_suffix(".context.json")
            if context.is_file() and not context.is_symlink():
                context.unlink()
                removed.append(str(context))
            atomic_json(path.parent / "CLEANED.json", {"reason": "舊量化／Mock 報告已清除，帳本保留"})
        except (ValueError, OSError, AttributeError):
            continue
    return {"root": str(root), "removed": removed, "note": "未清除正式資料或 SQLite 帳本"}
