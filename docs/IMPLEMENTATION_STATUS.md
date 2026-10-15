# 交付狀態與驗證邊界

本文件描述目前程式與驗證範圍。核心模組、離線整合與失敗保護已實作；真實 Codex 產業研究、完整真實行情 Forward 流程及正式競賽尚未完成 Live 驗證。沒有已驗證投資 alpha 或獲利保證。

## 各階段狀態

| 階段 | 已完成的實作 | 驗證與仍有的限制 |
| --- | --- | --- |
| Phase 0：安全修正 | 正式 Active Share／股票池／官方帳本／賽程 Fail Closed；提交生命週期；真實 Metadata；Schema／語意檢查；模式隔離 | 離線成功及拒絕案例可重現；實際隊伍資料、主辦方規則口徑與收件結果尚未提供／驗證 |
| Phase 1：資料與模型 | 來源 metadata／文字封存、hash、合法性與時間檢查；受控檔案／HTTPS／搜尋介面；Mock、Codex CLI、選用 API provider；逾時／取消／有限重試與執行紀錄 | Mock 及子程序安全測試可運行；Codex doctor 已確認開發環境 0.159.2、ChatGPT 登入與所需能力，未進行真實投資研究推理；來源 adapter 不是所有網站的專用爬蟲 |
| Phase 2：持久研究 | SQLite Entity／Edge／來源／Thesis 版本、支持與反方證據、FTS、新穎性、任務與研究記憶；有預算的多輪與反方研究 | 使用明確 Mock 回應驗證新增、查詢、更新與歷史保留；沒有預載已確認供應鏈。實際研究品質與覆蓋率待真實資料累積 |
| Phase 3：雙引擎 | 保留量化 baseline、獨立多時間尺度訊號、雙向候選、預期差代理、催化劑、情境與證據門檻融合、成本及風險約束 | 離線數值與安全測試覆蓋；初始參數為未校準 heuristic。缺共識或 OHLC 等資料時明示未知；相關性限制目前是逐對持股，不是完整群集上限 |
| Phase 4：競賽整合 | Python 官方股數、費率與稅；D-Plan v4.2；來源版本鎖定；官方庫存匯入／對帳；送件收據與不可變紀錄 | Mock E2E 的 Schema／context 語意通過；正式模式阻擋 Mock／fixture、缺資料與未知實際模型。沒有自動競賽送件、券商下單或真實官方收件驗證 |
| Phase 5：Forward | Q0／H1／H2 獨立帳本、不可變凍結、後續 VWAP 模擬結算、費用、NAV／MDD／換手／暴露、到期預測、基於凍結資料的錯過機會分類 | 離線多日、未到期、停牌缺價、版本與並發冪等測試可運行；尚未累積足以評估策略效果的真實多日資料。錯過機會分類不等於已證明因果或全自動事後產業調查 |
| Phase 6：整合與操作 | 一鍵 CLI、相容 `run`、狀態與查詢、Markdown 報告、每組 checkpoint、文件、fixture 產生器與測試 | 已在 Linux 執行離線 CLI 與整合測試；提供 PowerShell 指令及 Windows `tzdata` 依賴，尚未在原生 Windows 完成執行驗證 |

## 模型、網路與資料庫限制

- Codex provider 以已登入的 ChatGPT CLI 執行，不要求預設 API Key。真實呼叫必須明確 `--live`；沒有執行 GPT 時如實標示無新模型研究。
- 請求模型 `model_requested` 與實際模型不同。若 Codex JSONL 沒有回報實際模型，保留 unknown；正式模式因此 Fail Closed，不能手動填入所期待的名稱繞過。
- `research.allowed_domains: []` 預設拒絕外部來源擷取。啟用 `--live` 後仍需明確配置可信合法的來源及搜尋端點網域，環境網路政策亦須允許。行情來源先前遭代理 HTTP 403，完整 Live 行情未驗證。
- 真實研究庫為 `research.sqlite3`；Mock／fixture 使用 `research_mock.sqlite3`，以 `knowledge --synthetic` 查詢。正式模式不讀模擬庫。預設 Mock 沒有內建投資假說，完整研究資料流透過明確回應 fixture 的測試展示。
- Thesis 的支持／反對來源 ID 與版本一路傳遞至訊號及 D-Plan；來源更新保留舊原文。凍結決策、context 及每個 variant checkpoint 可供恢復，不回寫已完成預測。

## 正式啟用前需要的輸入

需要主辦方隊伍代號、官方固定 150 檔名單及來源、完整主動 ETF 前十大持股／日期／比較集合、主辦方實際庫存與現金、實際競賽首末日與休市口徑，以及 Active Share 計算定義。Repository 的現存名單與示例不代表上述條件已確認。

來源與模型可用後，應明確執行一次 Live 公司研究，核對來源、原文版本、Thesis 變化、反方論點、實際模型與用量紀錄；再進行真實行情 Shadow Forward，最後才驗證正式競賽路徑。不要以離線通過取代這些步驟。

## 已執行的工程驗證

開發過程已實跑 `status`、官方範例 `check`、合成 fixture 生成、Mock 離線 daily、三組產出附 context 的 `check`、`knowledge`／模擬庫查詢與 `evaluate`，並檢查文件本地連結。這些命令沒有啟動真實 GPT。

完整測試入口為 `py -m unittest discover -s tests -v`。實際通過數與最新結果以測試輸出及 PR 驗證紀錄為準，本文不固定會隨新增案例變動的總數。離線工程驗證、未執行的 Live 整合、待提供官方資料與尚未驗證的投資效果分開記錄。

## 後續改進方向

優先累積真實來源與多日 Forward，再校準訊號門檻、情境損失及換手要求；擴充合法產業資料與明確共識基準，改善研究覆蓋及來源獨立性判斷。之後可增加完整相關性群集約束、原生 Windows CI、模型／CLI 版本相容測試，以及更細緻的錯過機會研究與歸因，不以大型歷史回測作為目前必要前置條件。
