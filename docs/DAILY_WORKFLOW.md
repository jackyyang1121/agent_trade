# 每日操作流程

在 Repository 根目錄的 bash 執行 `conda activate agent_trade`。時間一律為台北 `+08:00`。

## 賽前演練

```bash
PYYAML_FORCE_LIBYAML=0 python -m pip install -r requirements.txt
codex login status
python -m agent cleanup-shadow
python -m agent rehearse --live --skip-benchmarks
```

未登入時先 `codex login`。移除 `--skip-benchmarks` 即包含前十檔 ETF 的十五大持股更新；也可獨立執行 `python -m agent refresh-benchmarks --live`。

`rehearse` 預設今天為首次交易日（休市順延），用前交易日收盤行情定價。若在盤中或收盤後執行，會保留真實研究時間；這是報告預覽，不是事前凍結的投資績效。每次產生新的十億元全現金模擬 DataPack，不會匯入正式帳本、改寫既有 Forward 預測或模擬當日已成交。

輸出的 `report`、`portfolios.H2.path`、`datapack` 分別指向可讀報告、v4.3 D-Plan 與模擬帳戶。報告預設在 `決策報告繳交紀錄/演練/`，模擬 DataPack 在 `DataPack/simulated/`；禁止將這類檔案上傳競賽。重跑是新演練，不累積初始帳戶績效；真實研究記憶保留於 `data/runtime/rehearsal/research.sqlite3`；可用 `python -m agent knowledge --state-dir data/runtime/rehearsal` 查詢，與一般研究庫分開。

無來源、新研究未成功或來源只有模型猜測時不產出 D-Plan；診斷在終端列出的 `research_failure.json`，原始失敗來源仍可在研究庫追查。`--provider mock --offline` 只用於工程測試，不會執行 GPT。

## 清理舊報告與輸出位置

`cleanup-shadow` 同時檢查目前 `state_dir/shadow` 及舊 `runtime/shadow`。只移除 summary 明示無新模型研究、或 D-Plan 明示量化／Mock 的報告，不刪 SQLite、來源、行情快照、正式資料或無法辨識的檔案；以 `CLEANED.json` 標記避免復用失效報告。已在本機生成且未推送的檔案，需在本機執行此命令。

| 位置 | 用途 |
| --- | --- |
| `決策報告繳交紀錄/演練/` | 模擬初始帳戶、真實行情及 GPT 的報告預覽 |
| `決策報告繳交紀錄/正式/` | 正式模式已驗證的待上傳 D-Plan |
| `決策報告繳交紀錄/影子/`、`個人/`、`離線測試/` | 各模式報告，互相隔離 |
| `DataPack/official/` | 後台 DataPack 原始帳戶與淨值紀錄 |
| `DataPack/simulated/` | 明確標記的初始模擬帳戶 |
| `DataPack/benchmarks.csv` | 模型查詢、Python 核對的 ETF 持股及日期來源 |
| `DataPack/benchmark_updates/` | ETF 原文快照、核對與失敗紀錄 |
| `DataPack/legacy/` | 舊淨值檔保留；不能當正式 DataPack |
| `data/runtime/` | 內部研究庫、帳本、checkpoint 與 Forward 紀錄 |

以上運行資料不納入 Git。`--state-dir PATH` 改為在該目錄下保存 `reports/`、`DataPack/` 及內部狀態，不污染預設資料。

## 正式比賽

已設定隊伍代號 `11329`、已核對的固定 150 檔股票池、10/27 首日與 11/27 末日。股票池使用本機官方 PDF 核對紀錄與 SHA-256，不需要下載 URL。若以新 CSV 重新匯入，可用：

```bash
python -m agent import-universe path/to/universe.csv --source-note "已核對的官方 PDF 名稱及版本" --as-of 2026-07-31 --confirm
```

將回傳的 path/manifest 填入 runtime。ETF 基準依目前使用者設定取參考清單前十檔、每檔前十五大原始 NAV 權重；自動更新保留資料來源與日期。正式模式仍須確認競賽要求是否涵蓋更多 ETF，以及 `top15_normalized` 或 `top15_raw` 計算口徑；演練的預設計算不冒充主辦確認。

正式當日先從後台下載 DataPack，接著：

```bash
python -m agent import-book path/to/DataPack.json --pricing-date YYYY-MM-DD
python -m agent daily --mode competition --live
```

`--pricing-date` 填前交易日，不能直接照抄 placeholder。匯入會核對資料包的 team_id、結算日、現金、持股、nav_tradable、aum 與應收權益；首次 `as_of_date: null` 只有真正全現金才有效。原始 DataPack 也保存在 `DataPack/official/`，供後續查閱帳戶與績效。自建／模擬 DataPack 不可匯入正式帳本。

D-Plan 使用你提供、版本欄位為 `4.x` pattern 的官方 Schema，輸出 `4.3`。目前原檔名仍含 v4.2；不需要重複提供或手動改內容。原版 v4.3 指南與 DataPack 規格見 [官方文件](official/)。

正式模式缺官方 DataPack、必要行情、完整設定基準或可追溯實際模型時停止；演練不代表正式送件條件已齊備。Codex 未回報實際型號時，正式模式仍阻擋，不能把請求型號當實際型號。

## 驗證、上傳、對帳

```bash
python -m agent check path/to/D-Plan.json
```

會讀取同名 `.context.json` 重算股數、現金與限制；沒有 context 時只有 Schema／引用驗證。明確指定不存在的 `--context` 會失敗。

收件窗為前一日 19:30 至交易日 08:55。到後台上傳、通過驗證後，仍須按正式提交並確認當日委託成功；上傳不等於送件。取得真實成功收件證據才記錄：

```bash
python -m agent mark-submitted ACTUAL_RUN_ID --receipt "實際成功收件證據"
```

生成、VALIDATED、SUBMITTED、成交與官方對帳分開。每日最多 25 次是提交次數，並非單份委託筆數。D-Plan 股數由 Python 依官方千股取整公式與實際結算庫存推導，不由模型直接填寫。下一日重新下載 DataPack 核對，不把本機 orders 當作已成交。

## 持續 Forward 與研究

```bash
python -m agent research --ticker 3363 --live
python -m agent knowledge --query "供應鏈瓶頸"
python -m agent daily --mode shadow --live
python -m agent evaluate
```

Shadow 三組 Q0/H1/H2 在截止前凍結，後續 session 才依 VWAP 模擬結算；1/5/10/20 日預測未到期時保持 PENDING。與演練、正式及個人帳本分開，不能以刪除歷史帳本繞過凍結。
