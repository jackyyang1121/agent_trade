# 測試與驗收

## 離線測試

自動測試使用 Python `unittest`、Mock provider 與固定 fixture，不要求 Codex 登入、付費 API Key 或外部網站即時可用。於 Repository 根目錄執行：

```bash
conda activate agent_trade
python -m unittest discover -s tests -v
python -m agent status
python scripts/create_demo_fixture.py --output data/runtime/demo_market.json
python -m agent daily --mode shadow --provider mock --offline --fixture data/runtime/demo_market.json --state-dir data/experiments/acceptance
python -m agent knowledge --synthetic --state-dir data/experiments/acceptance
python -m agent evaluate --state-dir data/experiments/acceptance
```

請以命令實際輸出的通過數與錯誤為準，不把本文視為某次執行結果。離線 CLI 使用明確建立的合成行情；預設 Mock 不新增研究假說。完整研究整合測試透過明確的 MockProvider 回應 fixture 驗證來源、Thesis、圖譜與訊號的傳遞；不是依賴預設 Mock 製造投資故事。全部輸出須保留 synthetic／Mock 標記。

## 應維持的工程不變條件

| 項目 | 驗證重點 |
| --- | --- |
| 既有 baseline | 官方千股公式、費率／稅、交易日、因子與公開說明書相容 |
| 資料與研究 | 必填時間、證據分級、合法性、來源去重、未知來源不得升級成事實 |
| 記憶與圖譜 | 新增／查詢關係、支持與反方證據版本鎖定、as-of 截止、Mock 與真實庫隔離 |
| 模型 adapter | Mock 結構輸出、型別／語意拒絕、逾時與登入錯誤、CLI 安全參數 |
| 雙引擎 | 缺值／異常值、雙向候選、研究條件式融合、反方／不確定性部位限制 |
| 正式合規 | Schema、引用鏈、全持股覆蓋、超賣／現金／個股／產業上限及 Active Share 缺失 |
| Forward | 三個獨立帳本、停牌無成交、費用、NAV、未到期 PENDING 與缺價 MISSING_DATA |
| 可重現性 | 相同 Run ID 重跑不扣款、variant checkpoint、凍結決策不覆寫、不同策略版本不混線 |

新增功能時，測試真正的數值、邊界與失敗行為，避免只逐行重述實作。修正重要失敗不得以降低正式驗證標準或寫死漂亮數字通過測試。

## Schema 與語意驗證

```bash
python -m agent check "(範例)D-Plan_TEAM_042_2026-10-27.json"
```

官方範例含節錄與合理推估數字，只能用於格式示範，不能作為真實市場 fixture 或完整帳本語意合規證明。檔案沒有正式 context 時，Schema 通過不代表可提交；完整語意驗證還需要實際持股、現金、前日價格、官方股票池與基準。

## Live Smoke Test

Live 測試獨立且必須明確選擇，會使用帳號額度：

```bash
codex --version
codex exec --help
codex login status
python -m agent rehearse --live --skip-benchmarks
```

`status`／doctor 檢查版本、參數與登入不等於執行投資研究。請檢查研究執行紀錄中的 provider、實際模型、來源、時間、錯誤與用量；未知 token 數保持省略，不能編造。Codex JSONL 若沒有實際模型，不能用 `model_requested` 冒充；正式模式應維持阻擋。Adapter 已實作但尚未完成真實研究 Live 驗證。不要在單元測試中反覆啟動真實 Codex，也不要把 Codex 自己的執行環境當成可無限制遞迴的 worker。

測試中空的 `allowed_domains: []` 必須拒絕下載；預設 runtime 已配置可信公開網域，Live 來源仍需受該白名單約束，不能只加 `--live` 就認為任意網站已允許。Windows 的 `tzdata` 安裝列入依賴，原生 Windows 執行仍待獨立驗證。

真實行情測試另需網路允許 TWSE／TPEx 等必要網域。HTTP 403 或代理拒絕時應記錄阻擋，不改用虛構即時行情。離線整合通過與真實來源、Codex 或正式競賽已驗證是不同結論。

## Forward 結果的解讀

初始化及同日重跑不能代替多日模擬。到後續可交易 session 才結算模擬委託，依當日金額／股數推導 VWAP，停牌或無量則 UNFILLED。1／5／10／20 日預測尚未到期時為 PENDING；到期卻缺報價為 MISSING_DATA。若沒有官方交易日表，`session_basis=observed_settlements` 代表只按已觀測 session 評估，不能宣稱完整交易日績效。

檢查每個策略版本是否有一致的資料截止、股票池、成本及初始資金。基準缺失時相對報酬保持未知。少量 fixture 測試不證明模型品質、報酬優勢或因子權重最優，策略效益須由保留原始預測的長期真實 Forward 實驗檢驗。
