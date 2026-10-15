# 排錯與恢復

## Python／conda

先在專案根目錄執行 `conda activate agent_trade`，確認 `python` 與 `python -m pip` 都來自這個環境，且已安裝 `requirements.txt`。不要把其他環境的 `pip` 裝到 `agent_trade`，也不要用系統的 `py` 啟動器繞過已啟用的 conda。

`agent_trade` 若是 Python 3.15，PyYAML 6.0.3 沒有 Windows 預編套件，pip 會改編譯 C 擴充並要求 Visual C++。安裝時加上 `PYYAML_FORCE_LIBYAML=0`，改用純 Python 的 PyYAML，不需要編譯器。這個環境變數只影響從原始碼建置；已有預編套件的 Python 版本仍會直接用套件。

```bash
conda activate agent_trade
python -m agent status
```

路徑有空格時用雙引號包住。JSON／CSV 用 UTF-8；股票代號保持字串，CSV 不要讓試算表自動轉換代號或百分比單位。若 Windows 出現 `ZoneInfoNotFoundError: Asia/Taipei`，用同一個 conda 環境重新安裝 requirements；其中的 Windows 專用 `tzdata` 提供 IANA 時區資料。

## Codex 無法登入或參數不支援

```bash
codex --version
codex exec --help
codex login status
codex login
python -m agent status
```

請在使用者本機瀏覽器完成登入，不把憑證貼進聊天或寫進設定。模型留空時沿用 Codex 可用設定；模型權限錯誤可移除覆寫或選擇帳號支援的名稱，不猜測已退役的模型 ID。

Adapter 採保守能力檢查：目前檢查過的 CLI 版本系列為 `0.159.x`，缺少所需隔離／唯讀／結構化輸出或停用危險功能的參數時，停止真實呼叫。較新版本也要先檢視官方 help、重新審查 adapter 與測試；單純升級並不代表相容。不可移除 sandbox、改成無限制權限或以 shell 執行字串繞過檢查。`--live` 未啟用時的拒絕是預期行為。

`codex login status` 成功只證明 CLI 認證狀態，不證明這次研究已執行。逾時／限流會保留錯誤與有限重試；Shadow 可以明示降級，正式模式依 policy 停止，不假造 GPT 決策。

若研究成功但模型欄位未知，查看 Codex JSONL 是否真的回報模型。`--model`／`model_requested` 只代表請求，不能作為實際值。Adapter 的真實研究整合尚未 Live 驗證，正式模式遇到此情況必須維持阻擋，後續需以官方可驗證回報補足，不手動填模型名稱。

## HTTP 403、代理或來源不可用

先看錯誤對應的網域與狀態碼。必要行情來源包含 `openapi.twse.com.tw`、`www.twse.com.tw`、`www.tpex.org.tw`，選用海外行情可能用 `query1.finance.yahoo.com`；研究來源依已授權的實際 URL 另行設定。

先區分研究 adapter 的 `research.allowed_domains` 與環境網路政策。研究清單預設 `[]` 會拒絕所有外部來源下載，即使有 `--live`；將合法可信的搜尋端點與來源網站明確加入清單。環境政策仍拒絕時才需要在環境允許清單加入必要網域。網站要求登入／授權則使用合法公開或授權來源。不要關閉 TLS／憑證驗證，不繞過付費牆，也不要把下載失敗轉成模型生成的數據。調整環境後只重試受影響步驟，記錄來源 unavailable 與資料限制。

可以先用 `--provider mock --offline` 完成工程測試，或匯入已合法取得的檔案；這不替代真實行情。沒有必要定價資料時，正式 D-Plan 必須停止。

## 正式模式被阻擋

| 訊息／原因 | 正確處理 |
| --- | --- |
| 隊伍代號未填 | 將主辦方正式代號填入設定，不猜造代號 |
| 股票池未確認 | 匯入官方固定 150 檔，確認來源、日期與市場分組 |
| 缺 Active Share 基準 | 匯入完整 ETF 前十大持股，確認權重尺度、比較集合及計算口徑 |
| 官方庫存日期不符 | 重新取得必要定價日的官方庫存／現金，不改日期冒充 |
| 交易日期／收件窗不符 | 核對台北時間、交易所日曆與主辦方賽程，調整已確認設定 |
| 現金、超賣、權重或產業限制 | 修正真實輸入或策略約束後重跑，不手動改委託股數 |
| 來源時間晚於截止 | 保留資料供後續決策使用，不回填到已凍結的舊交易日 |
| Metadata 不完整或非真實模型 | 修復 provider 執行紀錄；Mock／unknown 不冒充正式 GPT 執行 |

Active Share 在 Repository 規則中要求比較前十大，權重是否再正規化仍須主辦方確認。研究預設不是官方保證；不要只把「已確認」旗標改為 true 來跳過資料核對。

## 執行鎖、重跑與帳本

若有執行鎖，先確認是否仍有工作進行。不要同時啟動多個會寫同一 `state-dir` 的 daily；另一個實驗應用獨立目錄。異常退出後先保存資料庫與紀錄，核對程序和 checkpoint，再恢復，不直接刪除正式帳本或凍結結果。

同日相同版本重跑會復用結果；想換模型、prompt 或配置時保留新版本，不覆蓋舊決策。新策略版本的 Forward 曲線另行累積，不能混合成同一條績效。

若帳本與主辦方不同，以官方結算為準，使用 `import-book` 對帳。`mark-submitted` 只記錄真實送件收據；它不證明成交。已送件的決策不能以未送件版本默默取代。

## 查不到研究或評估為空

查看是否使用同一 `--state-dir` 及策略版本。一般 `knowledge` 讀真實 `research.sqlite3`；Mock／fixture 內容要用 `knowledge --synthetic` 查 `research_mock.sqlite3`，不會自動混用。研究來源缺失、被去重、超出截止、未通過驗證或只有未驗證傳聞，都可能沒有可用 Research Signal。請看 Markdown 中的限制與研究任務，不要強行提高證據等級。

剛建立的 Forward 沒有後續 session 時，NAV／預測可能未更新或為 PENDING。等到真實後續資料可得再評估，不能用未來價格提前填滿。停牌缺價、缺 benchmark 或缺交易日表時，結果應明確保留未知或 observed_settlements 標記。
