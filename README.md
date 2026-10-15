# agent_trade：證據驅動的雙引擎投資研究

本專案實作 **Evidence-Driven Asymmetric Momentum**：以產業研究發現基本面預期差與催化劑，以量化動能觀察市場確認，再在交易成本與風險限制下形成配置。研究覆蓋跨產業機會，不把技術題材當成訂單事實，也不預設持有天數。ETF 宣告以根目錄 [ETF 投資策略說明文檔](ETF投資策略說明文檔.md) 為準。

支援競賽 D-Plan、個人研究及 Q0／H1／H2 獨立模擬組合。這是研究與決策軟體；模擬結果不能證明投資超額報酬，專案不會自動向券商下單或向競賽網站送件。

## 第一次安裝：Windows PowerShell

建議使用 Python 3.12 與 Git。於現有 checkout 執行：

```powershell
Set-Location C:\path\to\agent_trade
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m agent status
```

以下指令使用 `py` 示意；若沒有啟用虛擬環境，請將 `py` 換成 `.\.venv\Scripts\python.exe`。Linux／macOS 可使用虛擬環境中的 `python`。

Windows 所需的 `tzdata` 已列入依賴，用於載入台北時區；目前測試在 Linux 執行，尚未完成原生 Windows 驗證。

## 先執行離線驗證

```powershell
py scripts/create_demo_fixture.py --output data/runtime/demo_market.json
py -m agent daily --mode shadow --provider mock --offline --fixture data/runtime/demo_market.json
py -m agent knowledge --synthetic
py -m agent evaluate
```

Mock 與明確建立的合成 fixture 僅驗證工程流程，輸出會標示模擬資料與模型，研究寫入隔離的 `research_mock.sqlite3`。一般 `knowledge` 只讀真實研究庫，`knowledge --synthetic` 查看 Mock 庫，`status` 同時顯示兩者。離線 daily 必須提供 `--fixture`，系統不會自動捏造行情。預設 Mock 不新增投資假說，研究輸出整合由具有明確模型回應 fixture 的測試驗證。它們不代表真實產業研究、即時行情或可提交的競賽決策。重複相同日期與版本的執行應復用凍結結果，不得重複扣款。需要獨立實驗時使用另一個 `--state-dir`。

## 使用 ChatGPT 帳號登入 Codex

依 [Codex 官方 CLI 安裝說明](https://developers.openai.com/codex/cli/) 安裝當前版本，再於本機 PowerShell 執行：

```powershell
codex --version
codex exec --help
codex login
codex login status
py -m agent status
```

登入採瀏覽器流程，不把帳號、憑證或 API Key 寫進 Repository。ChatGPT 訂閱能使用的模型與用量以帳號及 Codex 當下設定為準，並不等於額外的 API 額度。`config/runtime.yaml` 可設定 provider、模型與研究預算；模型留空時沿用 Codex 可用預設，不固定某個模型 ID。

真實 Codex 呼叫必須明確加 `--live`。先確認來源與權限，再執行：

```powershell
py -m agent research --ticker 3363 --provider codex_cli --live
py -m agent daily --mode shadow --provider codex_cli --live
```

`--model` 可覆寫模型設定。僅在已取得合法來源且模型實際執行成功時，輸出才代表新 GPT 研究；沒有來源時，不會把 Mock 或自行推測冒充事實。`openai_api` 是主動配置才啟用的可選 provider，不是預設方案。

常用的非秘密設定放在 `config/runtime.yaml`，亦可用子指令的 `--config PATH` 指定自己的設定檔：

| 設定 | 用途 |
| --- | --- |
| `state_dir` | 隔離資料庫、帳本與報告的根目錄 |
| `llm.provider`、`llm.model` | 模型來源與可選模型覆寫 |
| `llm.timeout_seconds`、`llm.max_retries` | 模型單次逾時與有限重試 |
| `research.max_rounds`、`research.max_model_calls`、`research.max_seconds` | 研究深度、呼叫及總時間預算 |
| `research.max_sources`、`research.max_tasks` | 來源與任務數上限 |
| `research.source_roots`、`research.search_url_template` | 授權檔案根目錄及選用的合法公開搜尋端點 |
| `research.allowed_domains` | 明確信任的研究來源網域；預設空清單拒絕外部來源擷取 |
| `signals`、`config/strategy.yaml` | 啟發式訊號與部位／風險限制 |
| `competition` | 官方匯入資料、賽程確認及保守降級政策 |

CLI 的 `--live` 明確開啟該次真實模型與受控研究網路存取，不會自動放寬 `allowed_domains`。先配置所需的可信來源與合法搜尋端點網域；行情 adapter 及環境網路政策另行檢查。`--offline` 停止真實模型與來源擷取。`--dry-run` 在暫存狀態執行並刪除產出，不會保留可供送件的檔案，也不是只列計畫的功能。

Codex adapter 已實作，尚未完成真實投資研究 Live 驗證。若 Codex JSONL 未回報實際模型，Metadata 會保留未知；`--model` 指定的是請求值，不能代替實際值，正式模式因此維持阻擋。詳細狀態見 [交付狀態](docs/IMPLEMENTATION_STATUS.md)。

## 正式競賽前的必要資料

需要主辦方隊伍代號、官方固定 150 檔股票池、完整主動 ETF 前十大持股基準、主辦方實際庫存／現金，以及已確認的賽程與交易日設定。Repository 中既有股票池不等於已由主辦方確認，空白 `config/benchmarks.csv` 不能通過正式 Active Share 驗證。

依 [每日流程](docs/DAILY_WORKFLOW.md) 的完整匯入命令保存來源 URL、資料日期與確認 manifest，將回傳路徑填入 runtime，並設定隊伍代號與已確認賽程後，再執行 `py -m agent daily --mode competition --provider codex_cli --live`。`import-book` 必須另提供 `--pricing-date`。缺行情、官方帳本、基準、模型紀錄或重要合規條件時必須停止；Shadow 的成功不等於正式模式已可提交。生成、驗證、人工上傳、成交及對帳是不同狀態。

## 文件與入口

| 文件 | 內容 |
| --- | --- |
| [架構](docs/ARCHITECTURE.md) | 模組、資料流、持久化與隔離 |
| [投資策略](docs/STRATEGY.md) | 雙向研究、訊號、風險及限制 |
| [每日流程](docs/DAILY_WORKFLOW.md) | PowerShell 指令、官方匯入、提交與結算 |
| [資料來源](docs/DATA_SOURCES.md) | 證據分級、時間、合法存取與格式 |
| [測試](docs/TESTING.md) | 離線驗收、Live 測試與解讀限制 |
| [排錯](docs/TROUBLESHOOTING.md) | 登入、代理、來源、合規與狀態問題 |
| [交付狀態](docs/IMPLEMENTATION_STATUS.md) | 各階段完成程度、驗證證據及待提供資料 |

`py -m agent --help` 與子指令 `--help` 顯示實際支援參數。舊 `run` 入口保留遷移相容性，請改用明確的 `daily --mode ...`。
