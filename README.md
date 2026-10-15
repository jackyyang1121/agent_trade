# agent_trade：證據驅動的雙引擎投資研究

採 **Evidence-Driven Asymmetric Momentum**：從產業與基本面研究尋找預期差、催化劑及非對稱機會，以量化動能、交易成本與風險評估形成動態配置。投資宣告見 [ETF 公開說明書](ETF投資策略說明文檔.md)。

## 賽前先看真實模型報告

在 Repository 根目錄的 bash 執行：

```bash
conda activate agent_trade
PYYAML_FORCE_LIBYAML=0 python -m pip install -r requirements.txt
codex login
python -m agent cleanup-shadow
python -m agent rehearse --live --skip-benchmarks
```

已有 Codex 登入可略過 `codex login`。`--skip-benchmarks` 方便先看研究與交易報告，會明確標示未驗證最新 Active Share；移除此旗標即可同時搜尋前十檔 ETF 的前十五大持股。兩種操作都不需要正式 DataPack。

`rehearse` 把今天視為演練首日（休市日順延），以**模擬十億元全現金帳戶、真實公開行情、真實 Codex 研究**產生 v4.3 D-Plan 及 Markdown 報告。每次都是新的初始帳戶，研究記憶保留。可在正式收件窗以外測試，日期與實際執行時間照實記錄；不計入 Forward 績效，不能上傳競賽。

終端輸出的 `report` 是可讀報告，`portfolios.H2.path` 是 D-Plan。預設位置：

- `決策報告繳交紀錄/演練/<交易日>/<run_id>/`：研究報告、D-Plan、驗證 context。
- `DataPack/simulated/`：明確帶 `_simulation: true` 的初始帳戶。
- `data/runtime/rehearsal/`：隔離研究記憶、內部來源與執行快照。

模型先以 Codex 原生 web search 探索公開來源，Python 再抓取正文、核對原頁發布時間、保存來源版本，供後續模型研究與反方檢驗。沒有成功的新研究或可核對來源時，會報錯並留下 `research_failure.json`，不會把量化備案當作模型研究。量化候選仍會如實標示是否有個股研究支持。

## 模型與資料

預設使用已登入 ChatGPT 的 Codex CLI，不需要預設 API Key，模型留空時使用 Codex 帳號可用預設。真實呼叫需 `--live`。`config/runtime.yaml` 保留 `xhigh` 與時間／來源／模型呼叫預算；來源探索占用研究呼叫預算，ETF 更新另有一次呼叫。完整模式可能耗時數分鐘以上。

Codex 在隔離唯讀目錄執行；只有來源探索與 ETF 查詢可使用原生 web search，分析階段不開工具。不開 shell、MCP、插件或檔案修改權限。預設研究白名單已配置於 `research.allowed_domains`，空清單仍拒絕下載。網站無法存取、無可核對日期或只提供搜尋摘要時，會記錄限制，不補造原文。

程式保留實際 provider、時間與可取得用量。CLI 未回報實際模型時保留未知，請求型號不冒充實際值；演練可閱讀這種報告，正式模式仍要求實際模型可追溯。開發環境已驗證 CLI 0.159.x 能力；其他版本若安全能力不符會停止。

## ETF 基準、帳戶與正式報告

```bash
python -m agent refresh-benchmarks --live
```

依你指定的範圍，查詢 `data/reference/active_etfs.csv` **原始順序前十檔** ETF、各十五大持股，更新 `DataPack/benchmarks.csv`，保留原基金權重、來源、資料日期、擷取時間、正文 hash 與核對紀錄。海外持股保留原代號，不改成台股。資料不完整時保留原檔，不用模型猜測補齊。只覆蓋這十檔，不宣稱覆蓋參考表內全部三十檔；正式賽前仍須核對主辦方要求的集合及 Active Share 正規化口徑。

正式 DataPack 放在 `DataPack/official/`，匯入時也會自動保留副本；其帳戶、持股與應收權益就是淨值資料來源，不再另建淨值紀錄資料夾。舊紀錄移至 `DataPack/legacy/`，未當成官方 DataPack。正式 D-Plan 輸出至 `決策報告繳交紀錄/正式/`。詳細匯入與上傳步驟見 [每日流程](docs/DAILY_WORKFLOW.md)。

你提供的 Schema 原檔仍名為 `(驗證繳交格式)D-Plan.schema_v4.2.json`，其內容接受 `4.x`，程式以內容驗證並輸出 `4.3`，沒有改寫官方 Schema。股票池採你已核對的本機官方 PDF 名單與 CSV 雜湊，不要求另造 HTTPS 網址；賽程已依你的確認設定為 10/27–11/27。

## 工程測試與持續觀察

```bash
python -m unittest discover -s tests -v
python -m agent status
```

離線流程僅驗證軟體，不能用來觀察 GPT 真實研究能力：

```bash
python scripts/create_demo_fixture.py --output data/runtime/demo_market.json
python -m agent daily --mode shadow --provider mock --offline --fixture data/runtime/demo_market.json
```

這些報告在 `決策報告繳交紀錄/離線測試/`，研究寫入 `research_mock.sqlite3`。持續多日觀察請使用 `daily --mode shadow --live` 與 `evaluate`；Shadow 需要事前凍結，保留 08:55 截止與後續 VWAP 結算，不適合隨時預覽今天的報告。`--state-dir PATH` 會隔離狀態、報告及 DataPack，方便獨立實驗。

本次以替身與離線資料驗證流程，沒有在開發環境遞迴啟動真實 GPT。分享對話連結與部分外部行情受雲端代理限制，未宣稱已完成本機 Live 驗證。Windows/bash/conda 操作方式已保留，原生 Windows 尚待實際執行。

更多資料見 [架構](docs/ARCHITECTURE.md)、[資料來源](docs/DATA_SOURCES.md)、[測試](docs/TESTING.md)、[排錯](docs/TROUBLESHOOTING.md)。
