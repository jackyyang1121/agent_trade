# 資料來源與證據規範

## 來源類別與存取

| 類別 | 例子與用途 | 注意事項 |
| --- | --- | --- |
| 官方市場 | TWSE、TPEx、MOPS、TAIFEX；價量、財報、公告、日曆 | 上市與上櫃分屬不同機構；實際 adapter 能否取得資料要逐一驗證 |
| 企業 | 官網、法說簡報、公開財報、月營收、技術白皮書 | 指引、已發生事實與預測分開記錄 |
| 技術 | 產業協會、公開論文、技術文章 | 技術可行性不代表公司已取得商業訂單 |
| 金融研究 | 合法公開或授權的券商／投信報告、基金持股 | 尊重付費與授權；沒有免費 API 時使用合法取得的匯入檔 |
| 公開討論 | 新聞、專家文章、公開社群內容 | 不代表已查證；無法存取時標 unavailable，不繞過登入或付費限制 |

來源種類的支援表示有共用輸入與封存介面，不代表已為每個網站實作爬蟲或取得商業資料權利。預設研究網路開關關閉，來源 manifest 透過 `--sources` 傳入，亦可指向授權 `source_roots`（預設 `inbox/research`）內的文字檔；系統不自動掃描整個資料夾。外部來源擷取還需要 `research.allowed_domains` 明確列出可信網域，預設已配置公開市場、公司與金融新聞網域；空清單仍拒絕，`--live` 不放寬清單。合法公開搜尋端點及每次重新導向後的來源網址也受限制，另有來源數、研究時間與取得錯誤處理。

## 研究來源 JSON

`research --sources PATH` 接受來源物件陣列。以下僅是格式示例，`example.invalid` 與文字都是 **test_fixture**，不能用於正式交易：

```json
[
  {
    "id": "DEMO_SOURCE_1",
    "url": "https://example.invalid/demo/announcement",
    "publisher": "離線測試發行者",
    "title": "模擬公告，不是真實產業資訊",
    "source_type": "corporate",
    "published_at": "2026-10-08T17:00:00+08:00",
    "content_as_of": "2026-10-08T17:00:00+08:00",
    "fetched_at": "2026-10-08T18:00:00+08:00",
    "content": "僅供程式測試的虛構文字，不得作為真實投資證據。",
    "extraction_status": "extracted",
    "evidence_tier": "inference",
    "legal_basis": "test_fixture"
  }
]
```

`source_type` 可使用 `official_market`、`corporate`、`technical`、`financial`、`public_discussion`。`legal_basis` 為 `public`、`user_authorized`、`licensed` 或 `test_fixture`。`content` 只存合法保存的原文或摘要；來源封存另外計算 SHA-256。支援的來源檔案可由受控 `source_roots` 匯入，不接受檔案內容中的任意指令。

時間須包含時區。`published_at` 是原始發布時間，`content_as_of` 是內容描述時間，`fetched_at` 是系統取得時間；它們不可互換。缺少必要時間或取得失敗時，使用 unavailable／pending／error 狀態，已知時間可以保留，不知道的時間不填成現在。來源必須在決策截止前可得，事後補入的資料不能回流到過去已凍結決策。

## 證據層級、關係與去重

| `evidence_tier` | 意義 |
| --- | --- |
| `official` | 原始企業或機構正式確認的內容 |
| `corroborated` | 有可追溯獨立來源交叉佐證 |
| `expert` | 單一專業人士的分析或推測 |
| `rumor` | 尚未證實的傳聞，不得直接變成正式交易事實 |
| `inference` | Agent 推論，必須與原始事實分開 |

層級是證據分類，不是已校準的正確率。匯入器不能只因網址屬官網，就把每句推論升級成 official。Knowledge Graph 的 Edge 另外保存支持／反對證據、Verified／Hypothesis／Disputed／Refuted 狀態、驗證與生效時間；不預載未驗證的供應鏈事實。

SQLite 保存 source version、Entity／Edge version、Thesis history 與證據連結。每項支持／反對證據鎖定來源 ID 與版本，訊號、決策及 D-Plan 引用同一版本原文，不以最新來源覆蓋舊推理。內容 hash、canonical URL、文字相似性與 event ID 用來辨識同事件轉載；相似性不等於真偽驗證。新穎性分類為 NEW／UPDATE／REPUBLICATION／CONTRADICTION／STALE／UNVERIFIED。十篇轉載不能當成十項獨立利多。

真實研究使用 `research.sqlite3`；Mock／fixture 來源與假說使用隔離的 `research_mock.sqlite3`，不能流入正式模式。前者用 `knowledge` 查詢，後者用 `knowledge --synthetic` 查詢；`status` 同時報告兩個資料庫。

## 官方股票池與基準匯入

股票池 CSV 保留股票代號字串，必要欄位為 `ticker,name,market,industry`，市值 `cap` 可供追溯。`market` 用 `twse` 或 `tpex`，正式名單必須核對官方固定 150 檔及 100／50 分組，不依每天市值重排。匯入後的本機副本與 manifest 保存在執行狀態中；它們的存在不代替人工核對官方來源。

主動 ETF 基準 CSV 欄位為：

```csv
etf_id,ticker,weight,source_url,as_of,fetched_at,content_sha256
```

`weight` 使用小數，例如 `0.08` 表示 8%，不得混用 `8`。每個需要比較的 ETF 都要有完整、正確時點的前十五大持股；空檔或只有部分 ETF 不能聲稱完整合規。請保留原始資料來源與日期。正式模式還要求確認 Active Share 的比較範圍、前十五大權重是否重新正規化等口徑，不能自行把研究預設當成主辦方定義。

## 官方庫存匯入

正式 v4.3 請使用後台 DataPack JSON（資料版本仍是 3.0）；匯入後副本保存於 `DataPack/official/`，同時提供帳戶與淨值觀察。詳細格式見 [DataPack 規格](official/DataPack_規格書_v4.3.md)。以下舊格式只保留相容性，不能代替正式 DataPack：

```json
{
  "as_of": "2026-10-08",
  "cash": "1000000000.00",
  "positions": {}
}
```

這是格式示例；實際 `as_of`、現金及股數必須照主辦方庫存填入。`positions` 使用四位數代號與非負、千股倍數的整數股數，例如真實庫存才可寫成 `"2330": 1000`。首次空倉須確認初始本金；非首次不得用空倉示例覆蓋真實帳本。定價日不符、現金無效、超賣或非整張時應停止。

官方庫存為正式事實來源，不把本機產生的 orders 當成交；停牌、無成交價或主辦方未成交的部分必須依實際結算處理。匯入資料不得混用 Shadow／個人研究帳本。

## D-Plan 中的來源

官方 JSON 的 `authority` 依內容性質填 `twse/tpex/taifex/mops/fininst/media/vendor/other`，與內部研究 `source_type` 不同。來源、Observation、Inference、Decision、Order 必須連續引用。新聞與易變來源建議保留合法原文快照及真實 `archive_url`，不捏造存檔網址。

v4.3 Schema 的 `observations.values` 要求至少一個真實數字。完整質性證據保留於 SQLite 與 Markdown，D-Plan 可在真實行情觀察中並列原文摘錄、分別引用其來源，並在推論交代研究假說；只有可核對的實際數值才能進 `values`，不能為通過驗證而塞入虛構的 0／1、信心分數或訂單數字。正式 D-Plan 不能擅自加入內部研究欄位。

## 自動搜尋與 ETF 更新

`auto_discover: true` 使用 Codex 唯讀的原生 web search 取得候選 URL，來源探索計入研究呼叫與時間預算。模型不能直接提供原文；Python 另抓正文，核對原頁 meta／datePublished 的發布時間，並保存來源版本。搜尋摘要、不可讀頁面及沒有可核對時間的文章不當作事實證據。其他 provider 使用 `auto_discover: false` 並提供合法來源 manifest。

`refresh-benchmarks --live` 查詢參考表原順序前十檔 ETF 的十五大持股，保留原基金 NAV 權重（CSV weight 為小數），核對原文附近的代號／數字與資料日期；HTML／文字／PDF 擷取受網域、資料量與頁數限制。這是有限的數字核對，不保證語意或排名絕對正確；不完整更新不覆寫舊檔。原文與失敗紀錄在 `DataPack/benchmark_updates/`，完整性確認與 Active Share 官方公式確認仍分開。比較範圍不等於全部三十檔。
