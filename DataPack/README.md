# DataPack 與帳戶資料

正式後台原檔放 `official/`；`import-book` 也會在此保存經核對的 DataPack 副本。帳戶、持股、應收權益與 nav_tradable/aum 是每日淨值觀察依據，不再另建淨值紀錄資料夾。

`simulated/` 僅存賽前十億元全現金帳戶，帶 `_simulation: true`，不能匯入正式帳本。`benchmarks.csv` 與 `benchmark_updates/` 是前十檔 ETF 前十五大持股的更新結果、來源日期與原文快照。

`legacy/` 保留既有淨值紀錄，未宣稱是官方 DataPack。運行中的正式帳戶、模擬帳戶與抓取原文均由 Git 忽略。

操作方式見 [每日流程](../docs/DAILY_WORKFLOW.md)。
