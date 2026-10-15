# 本次重構交付報告

## 1. 實際系統架構

`research` 透過受控來源擷取、來源封存與結構化模型輸出，進行有預算的產業發掘、反向研究及反方檢驗。`knowledge` 以 SQLite 保存版本化來源、圖譜、Thesis、研究任務及執行紀錄。`signals` 保留既有量化 Baseline、獨立市場指標與證據門檻融合，交由確定性的 `portfolio` 產生股數。

`orchestration` 串接每日流程、不可變資料快照、研究來源版本與 checkpoint；`compliance`、`checks`、`dplan` 驗證正式競賽資料與完整證據鏈。`evaluation` 為 Q0／H1／H2 分別凍結預測、以後續 VWAP 模擬成交及評估結果；已結算的實驗回饋可傳回研究提示，但不回寫歷史假說或事前理由。

## 2. Git 修改範圍

新增 `agent/llm/`、`research/`、`knowledge/`、`signals/`、`evaluation/`、`orchestration/` 及 `agent/compliance.py`。改進 CLI、投資組合、驗證、官方帳本及行情轉接；舊 `run` 改為 Shadow 安全相容入口。新增 `config/runtime.yaml`、合成資料產生器、測試與繁中操作文件。

保留最新 ETF 公開說明書、官方競賽說明、D-Plan 指南、官方 Schema、原有股票池、正式帳本與 ETF 資料原樣。未將個人庫存、秘密或運行資料庫提交至版本控制。

## 3. GPT 執行方式

在使用者本機安裝並以 `codex login` 登入 Codex CLI。系統檢查當前 CLI 參數、登入與安全能力，採唯讀沙箱、隔離工作目錄、停用工具、無 shell 的 argv 呼叫及 JSON Schema。模型可由 `runtime.llm.model` 或 `--model` 指定，預設不硬編碼模型 ID。

真實呼叫需明確 `--live`。本次只檢查 Codex 0.159.2／ChatGPT 登入及能力，沒有啟動真實 GPT 推理。若 CLI 事件未提供實際模型，系統保留未知，正式競賽阻擋；請求模型名稱不能冒充實際回報名稱。選用 OpenAI API provider 不影響預設 ChatGPT CLI 方案。

## 4. 每日操作（Windows PowerShell）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m agent status
python scripts/create_demo_fixture.py --output data/runtime/demo_market.json
python -m agent daily --mode shadow --provider mock --offline --fixture data/runtime/demo_market.json
python -m agent knowledge --synthetic
python -m agent evaluate
```

以上 Mock 指令使用明確合成資料驗證工程流程。預設 Mock 不憑空新增研究假說；帶結構化回應的研究完整流程由離線整合測試驗證。

真實公司研究需先在 `runtime.research` 配置合法來源、搜尋介面與 `allowed_domains`：

```powershell
python -m agent research --ticker 3363 --provider codex_cli --live
python -m agent daily --mode shadow --provider codex_cli --live
```

## 5. 正式競賽操作

先取得並核對隊伍代號、固定 150 檔名單、完整 Active ETF 前十大持股、公式口徑及實際首末日。`import-universe`、`import-benchmarks` 建立帶來源、日期與雜湊的匯入檔及 manifest，依命令輸出設定 runtime 路徑；完整範例見 [每日工作流程](DAILY_WORKFLOW.md)。

```powershell
python -m agent import-book inbox/official_book.json --pricing-date YYYY-MM-DD
python -m agent daily --mode competition --provider codex_cli --live
python -m agent check PATH_TO_DPLAN
```

成功輸出只代表 `VALIDATED`。使用者上傳並確認主辦實際收件後，才以 `mark-submitted RUN_ID --receipt RECEIPT` 記錄 `SUBMITTED`；後續匯入主辦實際結算庫存才能核對。程式不自動送件或把產生委託視為成交。

## 6. Forward Testing

Q0、H1、H2 使用相同初始本金、股票池、費用、資料截止與交易規則，各有獨立帳本。決策在截止前凍結，後續資料到來才結算、評估 1／5／10／20 交易日期限。未到期維持 `PENDING`；不同策略版本不混接曲線。

每日期錄研究、量化候選、未交易理由、委託及 context，輸出 Markdown 報告。`evaluate` 彙整 NAV、報酬、成本、換手、回撤、產業與相關風險及基於當時快照的錯過機會分類。這是前瞻實驗框架，尚非已驗證的投資優勢。

## 7. 已執行驗證

```text
/workspace/venvs/agent_trade/bin/python -m unittest discover -s tests -v
Ran 119 tests — OK
```

另通過 Python 編譯、`pip check`、`git diff --check` 與保留檔案比對。實際 CLI 驗收包含 status、Mock daily、三份 D-Plan 的完整 context 驗證、同日重跑、次日結算、evaluate、模擬知識庫查詢及正式模式拒絕合成輸入。

離線示範三組各產生 24 筆委託；次日各結算一次且有費用與 NAV 紀錄，正式帳本未建立或改寫。數字皆為合成行情測試結果，不是投資績效。正式成功流程另以服務測試替身驗證，沒有冒稱真實官方資料或模型執行。

## 8. 未完成 Live 驗證與必要輸入

- 行情來源仍遭目前環境代理 `403 Forbidden`；真實行情 Forward 流程未驗證。
- 未執行真實 GPT 產業研究、選用 OpenAI API Live 整合或原生 Windows 執行驗證。
- 正式隊伍、完整官方資料、收件及公式口徑仍需使用者提供／確認。未知模型 Metadata 會阻擋正式輸出。
- 部分來源僅支援通用 JSON、RSS、文字與 HTML 轉接；付費、登入保護或 PDF 專有來源需合法資料與適當擷取器，失敗顯示 unavailable。
- 錯過機會分類及證據等級是透明啟發式，並非已證明的因果歸因或校準成功機率。詳見 [實作狀態](IMPLEMENTATION_STATUS.md)。

## 9. 後續改進

優先累積真實來源與多日 Forward，校準證據、訊號與換手門檻；擴充合法產業資料、明確共識基準、來源獨立性判斷與模型品質評估。工程上可增加原生 Windows CI、CLI 新版能力審查及完整相關性群集約束。這些工作不以大型歷史回測作為目前必要前置條件。
