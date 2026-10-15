# 每日操作流程

以下命令於 Repository 根目錄的 bash 執行，時間一律以台北時區 `+08:00` 解讀。先執行 `conda activate agent_trade`，確認這個環境已安裝 `requirements.txt`。

## 1. 系統與離線流程

```bash
python -m agent status
python scripts/create_demo_fixture.py --output data/runtime/demo_market.json
python -m agent daily --mode shadow --provider mock --offline --fixture data/runtime/demo_market.json
python -m agent evaluate
```

離線模式必須指定明確標示 synthetic 的 fixture 與 Mock，不會自動生成行情。產生器預設合成交易日為 `2026-10-15`，可用 `--date` 改成測試用平日；它使用平日日曆，並非官方交易日曆。daily 的 `--date` 若提供，必須和 fixture 相符。`--state-dir` 隔離實驗。請從實際輸出取得 Run ID、報告、D-Plan 與帳本位置。不要將這些輸出上傳競賽。

```bash
python -m agent daily --mode shadow --provider mock --offline --fixture data/runtime/demo_market.json --state-dir data/experiments/demo
python -m agent evaluate --state-dir data/experiments/demo
```

同日重跑應返回已凍結結果；需要比較新策略時使用新版本與狀態目錄。不要刪除帳本來避開保護。

## 2. 單獨研究與記憶查詢

```bash
python -m agent research --provider mock --offline
python -m agent knowledge --synthetic
python -m agent research --ticker 3363 --provider codex_cli --live
python -m agent knowledge
python -m agent knowledge --query "量產瓶頸"
python -m agent knowledge --technology "CPO"
python -m agent knowledge --ticker 3363
```

使用 `research --sources C:/data/research_sources.json` 匯入已合法取得的來源；資料格式見 [資料來源](DATA_SOURCES.md)。來源 manifest 可以提供合法正文，或指向 `research.source_roots` 內已授權文字檔；不會自動掃描所有資料夾。沒有可驗證來源時，結論停留於未驗證假說或研究佇列。預設 Mock 不新增假說，結構化研究完整流程使用測試中明確的模型回應 fixture 驗證。來源未能取得應保留 unavailable，不能由模型自行補成事實。

`knowledge` 預設只讀 `research.sqlite3`；Mock／fixture 研究寫入 `research_mock.sqlite3`，須加 `--synthetic` 才會讀取。`status` 同時顯示兩者，模擬證據不會被正式模式讀入。

Codex 真實呼叫須 `--provider codex_cli --live`；`--model` 僅在希望覆寫現有模型時使用。模型設定統一放在 `runtime.yaml`，舊 settings 中的 xAI 固定值已移除。Mock／offline 不啟動 Codex。`--no-research` 用於明確跳過新研究，報告必須標示此次無新 GPT 研究；不能把快取或 Quant baseline 包裝成當日模型決策。

下載研究來源前，另在 `research.allowed_domains` 明確列出可信網域，包含所用搜尋端點及來源網站；預設 `[]` 拒絕外部來源網路。`--live` 不自動解除此限制。合法授權的本機內容仍可用 source manifest 匯入。

## 3. 真實 Shadow Forward Test

```bash
codex login status
python -m agent daily --mode shadow --provider codex_cli --live
python -m agent evaluate
```

先確認市場與來源能存取。系統為 Q0／H1／H2 保存獨立帳本、候選、訊號、委託與凍結版本，各組分別 checkpoint；若中途失敗，重跑復用已完成部分。模擬成交依可取得的後續價格結算，尚未發生的成交及 1／5／10／20 日預測保持 PENDING；不使用前日收盤價假裝當日實際成交。

研究失敗時 Shadow 可以依政策使用既有研究或 Quant baseline 繼續，但會記錄降級與錯誤。行情不足不能計算的 NAV／報酬保持未知。評估需累積後續交易日資料，單日初始化不會得到有意義的績效曲線。

## 4. 正式競賽準備

正式啟用前確認：

1. 在 `config/settings.yaml` 填入主辦方配發的 `team_id`。
2. 匯入官方固定 150 檔股票池，確認名單來源與凍結日。
3. 匯入完整主動 ETF 前十大持股資料，確認比較對象、權重尺度及資料時點。
4. 匯入主辦方實際庫存與現金；首次空倉也必須使用經確認的初始帳本。
5. 依主辦方公告確認賽程、交易日與 Active Share 計算口徑，完成 runtime 的正式確認設定。
6. 確認前交易日行情與每檔持股定價完整，Codex 研究可用並留下真實 Metadata。

Codex adapter 已實作但尚未完成 Live 研究驗證；若 CLI 事件未回報實際模型名稱，正式模式仍會阻擋，即使已用 `--model` 請求某模型。不能把請求名稱當成實際執行證明。

以下 bash 先輸入實際資料的來源與日期，不使用虛構官方網址。只有完成核對後才使用 `--confirm`：

```bash
read -r -p "官方固定股票池的 HTTPS 來源 URL: " UniverseUrl
read -r -p "官方股票池凍結日 YYYY-MM-DD: " UniverseDate
read -r -p "完整主動 ETF 持股的 HTTPS 來源 URL: " BenchmarkUrl
read -r -p "基準資料日期 YYYY-MM-DD: " BenchmarkDate
read -r -p "官方庫存對應的前交易日 YYYY-MM-DD: " PricingDate
read -r -p "主辦方確認的定義 top10_normalized 或 top10_raw 或 full_normalized: " ActiveShareDefinition
python -m agent import-universe "C:/data/official_universe.csv" --source-url "$UniverseUrl" --as-of "$UniverseDate" --confirm
python -m agent import-benchmarks "C:/data/active_etfs.csv" --source-url "$BenchmarkUrl" --as-of "$BenchmarkDate" --definition "$ActiveShareDefinition" --confirm
python -m agent import-book "C:/data/official_book.json" --pricing-date "$PricingDate"
```

匯入會回傳 `path` 與 `manifest`。目前不會自動改寫 `config/runtime.yaml`；將股票池輸出的路徑填入 `competition.official_universe_path`、`competition.universe_manifest`，基準輸出填入 `competition.benchmarks_path`、`competition.benchmarks_manifest`。在已核對賽程後設定 `schedule_confirmed: true`、真實 `first_trade_date` 與 `last_trade_date`；需要不同環境設定可用 `--config C:/data/runtime.yaml`，不必改 Python。

```bash
python -m agent status
python -m agent daily --mode competition --provider codex_cli --live
```

資料匯入格式見 [資料來源](DATA_SOURCES.md)。`--confirm` 僅記錄資料核對，不會向主辦方遠端驗證。缺 Active Share 基準、官方確認或帳本時，正式模式採 Fail Closed。Repository 文件對競賽首日提及 10/26，既有設定因休市處理使用 10/27；必須以主辦方賽程與交易所日曆核對後配置，不能只根據本機日期猜測。

## 5. 驗證、人工上傳與確認

```bash
python -m agent check "C:/data/D-Plan_TEAM_XXX_YYYY-MM-DD.json"
python -m agent check "C:/data/D-Plan_TEAM_XXX_YYYY-MM-DD.json" --context "C:/data/validation_context.json"
```

`check` 會自動尋找 JSON 同名的 `.context.json`；也可以明確提供 `--context`。沒有完整 context 時，獨立檔案檢查只能確認可觀察的格式／引用，不代表官方帳本、現金及 Active Share 均已通過。正式 daily 的驗證使用當次真實狀態。不要手改已產出的 JSON、股數或理由；修正資料／設定後，在允許的版本流程重新產生。

D-Plan 檔名為 `D-Plan_<team_id>_<trade_date>.json`。交易股數由官方公式推導：

```text
目標股數 = floor(目標權重 × 前日 NAV ÷ 前日收盤價 ÷ 1000) × 1000
委託股數 = 目標股數 − 主辦方實際結算股數
```

人工上傳至競賽系統。依 Repository 規則，收件窗為前一日 19:30 至交易日上午 08:55，每隊每日最多 25 次；在正式運行前仍須確認主辦方最新公告。程式不會替你按送件，也不會將產出自動當作已提交。

只有拿到主辦方成功收件的真實憑證後，才執行：

```bash
python -m agent mark-submitted ACTUAL_RUN_ID --receipt ACTUAL_RECEIPT
```

該命令記錄送件事實，不是執行交易。之後匯入官方結算庫存／現金完成對帳；無成交或停牌個股不能假設全部成交，超賣會使整份 D-Plan 被拒收。

## 6. 例行檢視

閱讀當日 Markdown 報告的新增證據、Thesis 增減、雙向候選、反方證據、未交易理由、研究限制與待辦。查看 `evaluate` 的成本、NAV、MDD、到期預測及錯過機會歸因。新的研究資料只能影響後續決策，不能回寫已凍結預測。
