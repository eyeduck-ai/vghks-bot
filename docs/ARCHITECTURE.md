# 系統架構

本文對齊 6.6.49，沿用本機 HTTP、每帳號 SQLite 與 SDK 閘道。SDK 固定為 0.22.6，完整 commit 同步列於專案及建置鎖定檔。

## 已存術前報告匯出

`analysis_export.py` 提供帳號範圍的 `POST /api/analysis/export/read`（可匯出病人摘要）與 `POST /api/analysis/export`（ZIP）。核對清單成員、指定抓取帳號及病人附件引用；只讀取已存資料，不呼叫 SDK，唯讀及離線亦可使用。ZIP 在暫存目錄建立並串流傳回，請求結束即清除暫存檔；重複附件在每位病人資料夾內去重，缺檔保留明確標記。

離線 HTML 使用相對檔名與外部 `data.js`，避開 file:// 的 fetch 限制，重用 SOAPView 與 CataractNumeric。資料同時提供 JSON／TXT／CSV 及原始附件；不輸出 SDK 參照或內網下載路徑。`downloads.py` 統一檔名與 UTF-8 Content-Disposition；附件 GET 保留範圍讀取，`download=1` 改為原檔下載。

`ClinicalUI` 共用附件下載、文字 TXT、圖片剪貼簿及比較勾選。圖片複製於點擊時啟動 ClipboardItem，將圖片像素轉為 PNG；原生右鍵與原檔格式保留，不支援或權限遭拒時提示替代操作。

## 元件

```text
瀏覽器（HTML / CSS / JavaScript）
  └─ 127.0.0.1 本機 HTTP Server
       ├─ DatabaseManager：選擇／備份／匯入資料庫
       └─ BotApplication：帳號入口與監控排程
            └─ Workspace：每帳號獨立工作區
                 ├─ AccountGateway → vghks-sdk → 院內系統
                 ├─ Review → 病人集合、SOAP 版本、備註與工具任務
                 ├─ ReviewHistory / ScannedRecords → 歷次就診、報告與掃描索引
                 ├─ Analysis → 檢查比較、附件、刀表預覽／套用
                 ├─ LibraryData → 本機病歷與分類快取清除
                 ├─ Approvals / Earnings / SurgerySchedule
                 └─ SQLite、附件及診斷資料
```

| 位置 | 用途 |
| --- | --- |
| `vghks_bot/app.py`、`bot.py`、`bot_server.py` | 程式入口、帳號生命週期及本機 API |
| `vghks_bot/bot_gateway.py`、`clinical_identity.py` | SDK 工作階段、序列操作及病人身分核對 |
| `vghks_bot/request_gate.py`、`task_manager.py`、`workbench.py` | API 存取租約、任務名額及共用精簡狀態 |
| `vghks_bot/review.py`、`review_history.py`、`scanned_records.py` | 檢閱任務、歷史索引與按需掃描附件 |
| `vghks_bot/soap_data.py`、`tags.py` | 結構化 SOAP、tag 與搜尋依據 |
| `vghks_bot/analysis*.py` | 歷年比較、數值、醫囑與附件 |
| `vghks_bot/library_data.py` | 帳號範圍資料清單、影響預覽、分類清除與附件引用檢查 |
| `vghks_bot/approvals.py`、`earnings.py`、`surgery_schedule.py` | 三個系統的資料保存與查詢 |
| `vghks_bot/google_sheets.py`、`sheet_plan.py` | Google 授權、逐格差異與寫入核對 |
| `vghks_bot/databases.py`、`bot_store.py`、`library.py` | 資料庫管理及持久儲存 |
| `vghks_bot/migrations.py`、`task_data.py`、`library_search.py`、`analysis_metadata.py` | 可重建摘要、搜尋索引及可回復的結構更新 |
| `vghks_bot/connection_state.py`、`patient_lookup.py`、`debug_*.py` | SDK 錯誤分類、病人查詢及異常證據 |
| `vghks_bot/static/` | 本機 Web UI，EXE 內嵌；主畫面 `bot.html`，進階工具仍嵌入 `/tools` 的既有比較頁 |
| `tests/`、`vghks_bot/selftest*.py` | 合成單元／整合／凍結後驗證 |

## 帳號與任務

臨床 API 以 `/api/accounts/{account_id}/…` 隔離；本機 Session、CSRF、同源及資料庫上下文檢查共用。每帳號擁有獨立 SQLite、附件及 SDK 工作階段，不跨帳號共用病人快取。`review_history.py` 的歷年索引與所選附件也要先核對任務、病人及伺服器已保存的參照。

同帳號的 SDK 操作序列執行，病人延伸查詢在安全步驟之間插入並重新確認上下文。不同帳號可平行，預設最多三個院內連線。同帳號前景任務最多 4 個，待執行上限 64 個；前景名額包含等待 SDK 操作的工作，不代表同帳號同時發出 4 個院內請求。相同帳號與查詢參數沿用未完成的前景任務，額滿以 409 拒絕且不建立新任務。關閉／登出保存進度並暫停，重開不自動續跑未完成任務。

審查與薪資監控使用程式內排程器；僅在 EXE 開啟且帳號在線時生效。每次啟動程式後，每個帳號的審查監控須首次進入審查功能才開始，之後依偏好持續執行。薪資使用既有監控偏好。GitHub CI 只處理合成驗證與發布，不執行院內抓取或監控。

## API、交易與輪詢

HTTP 層核對本機 Session、CSRF、帳號、資料庫上下文及輸入；功能服務執行查詢及保存。共用 `Handler.dispatch_get`／`dispatch_post` 接受明確的 Workspace，不暫時替換 Handler 的帳號或伺服器上下文。

`RequestGate` 的共用租約允許 API 並行，mutex 只保護存取計數。資料庫切換、匯入等生命週期操作使用排他租約；使用中回覆 409，既有請求結束後即可重試。帳號登入、SDK 序列鎖及 SQLite 寫入鎖分開管理，院內請求不占整個 HTTP Server 的共用鎖。

`Library.read_snapshot()` 開啟獨立 WAL 唯讀快照，純讀取不取得帳號寫入鎖；同執行緒的讀取共用連線。`Library.batch()` 保留寫入鎖與交易，供需要寫回數值、保存備註或合併設定的本機操作使用。兩者都不跨院內請求或排程等待。啟動任務以應用程式鎖再取得前景任務鎖；暫停循序任務前先釋放前景鎖，避免反向取得。

四種工具來源的讀取及保存由 `Review.tool_state()`／`save_tool_state()` 負責；保存時在同一交易核對集合、讀取最新設定並合併目前工具。並行操作不會覆蓋其他工具，寫入中斷會回復。清空只保存目前工具的空來源，保留集合、既有結果與執行中任務的固定名單。

主頁與內嵌工具共用 `/api/accounts/{id}/status` 的精簡狀態、版本與關注任務；活動每 2 秒、閒置每 10 秒、隱藏頁面停止輪詢。病歷全文及爬蟲紀錄按需讀取，紀錄每頁 40 組完整任務家族，支援系統、病人、狀態、日期與備註篩選。`tasks/detail?summary=1` 只回傳任務摘要；清單分頁預設 40、上限 200，舊介面不指定摘要或分頁時維持完整回傳。進度卡只保存必要欄位，完成 5 秒、其他終態 15 秒後釋放。

## 資料

資料格式目前為 1，與應用程式版本分開。原始 SOAP 與版本、任務採用的版本引用、解析數值、醫囑、審查及薪資內容分開保存。手術排程以內容雜湊共用快照，每次取得的時間與任務引用仍保留。

`record_search`／自動 TAG 索引與任務病歷關聯先在 SQL 限定範圍，計數及分頁後才解析當頁全文。內容摘要與 TAG 規則版本決定索引有效性，舊資料按批次重建。`bot_task_state`／項目摘要以 SQL 彙總進度，動態更新不改寫固定病人清單；`analysis_step_metadata`／run 摘要保存讀取狀態與版本，不複製數值與附件全文。結構更新由 `migrations.py` 在同一交易保存資料及完成標記，中斷可重新執行；原始內容、版本、備註、手動 TAG 與診斷不因重建而移除。

PDF／影像以內容雜湊存檔；刪除會處理無引用附件與衍生資料。刪除狀態避免中斷任務續跑後還原已刪內容。移除集合不等於刪除病歷。

門診與進階工具的清除入口集中於「病歷管理」。`/library/data/read`、`/preview-delete`、`/delete` 分別提供分類清單、影響範圍及清除；確認使用資料指紋重新核對，帳號有執行中任務時拒絕清除。就診索引連帶清除依賴的歷史快取，其他類別使相關分析摘要失效；刪除所有版本及結果副本，保留集合、手動 TAG、備註與任務錯誤摘要。`clinical_cache_deletions` 保留明確清除標記，讓工具切頁只讀本機，不自動補抓；前端事件同步清除失效的檢閱及比較狀態。

`progress.js` 以帳號及任務識別維護共用懸浮進度，觀察既有 API 請求及任務輪詢，不另外新增院內請求。內嵌工具經同源及來源視窗核對後將狀態交給主頁；主頁將卡片置入最上層對話視窗。`clinical-ui.js` 共用病歷號複製與 PDF 開啟設定；檔案比較用既有節點及 CSS 排序，避免改變版面時重新載入 PDF。

帳密與 Google 金鑰以可攜式形式保存在資料庫，解密材料也一同保存。瀏覽器與任務日誌不回傳密碼或私鑰；完整資料目錄應視為包含憑證的私人資料。

## 白內障數值與背景佇列

`numeric_measurements.py` 保守解析驗光及 KM，使用 Decimal 計算 SE／Kavg，API values 為可運算數值、exact 保留完整十進位字串。所有來源列、眼別、文字及單位保留，缺值為 null；拆欄與整串形式一致處理，矛盾欄位不推算。

`analysis_steps` 以 `numeric-structured:<source_key>`／kind `numeric_structured` 保存衍生列，包含 source_digest、source_saved_at、parser_version。數值寫入時同一交易保存原始與衍生資料；舊資料讀取時按來源與版本補解析，唯讀只在記憶體解析，並以來源摘要避免並行更新或刪除後寫回舊資料。分類清除涵蓋衍生紀錄，但資料庫清單與清除預覽不重複計數。結果、比較數值欄與 ZIP 離線頁共用十五項分類及結構化格式；CSV 增加解析欄位。

帳號範圍 POST `/api/accounts/{id}/analysis/cataract/queue` 接受 cohort_id、action（start/prioritize/pause/resume）及目前病人 mrn。`cataract/status` 回傳 queue 狀態、pending、active_mrn、priority_mrn、wait_seconds 及 reason。集合與抓取帳號在伺服器核對。

白內障狀態使用 `Library.read_snapshot()` 批次取得集合內病人的步驟、醫囑完成狀態及資料版本，持續核對病人身分、附件存在與部分解析；不載入 SOAP 全文與版本。結果可能補寫舊數值的衍生資料，使用 `Library.batch()` 保留交易回復。`data_revision` 由原始／衍生步驟、SOAP、就診索引、清除標記及附件存在狀態產生，供前端核對已存結果。

歷年醫囑正常流程一次讀取全部類別的索引，再以已核對的眼科就診與檢查名稱篩選，只對符合的眼科醫囑取得明細、報告與附件。一般病歷檢閱仍可查看其他科別的索引。`sdk_orders.py` 處理與 SDK 相容的光照治療附件參照，未知路徑及不符病人資訊拒收。

`cataract.js` 將結果快取限定於帳號及目前集合，合併重複狀態／病人請求；所選病人的結果讀取不等待狀態輪詢。背景保存改變版本時只使相關病人快取失效，清除與集合切換會丟棄已失效的延遲回應。`analysis.js` 的請求序號使連續切換不會以舊病人的 SOAP 覆蓋新選取；已完成或失敗待續跑的病人不因檢閱而送出 prioritize。

`cataract_queue.py` 的每帳號排程器在獨立執行緒等待，不占原任務工作佇列；SDK 操作仍由 AccountGateway 序列化，沿用連線上限及 session 恢復。每位建立可追溯的分析任務；在已保存的讀取邊界協作暫停，先處理手動選取的病人或原佇列中的前景工作，再接回原病人。背景病人間隨機等待 10–20 秒，成功／失敗的本次讀取記憶避免插隊後重抓或隱含重試。部分失敗須明確續跑，離線或無法恢復連線暫停；重啟從集合的佇列摘要恢復為暫停，需明確繼續。清除標記阻止背景自動補回資料。

## 外部整合

- SDK commit 同時固定在專案與建置鎖定檔。WebMAAS timeout 由 SDK 0.22.6 在原操作內重建一次 SSO；平台不另重建帳號或重送 Portal 帳密。PRQ 就診與 SOAP 遇院方調閱審查時，由 SDK 核對並按需送出「了解病情」，送出後不由平台重播整筆讀取。`connection_state.py` 依 SDK ErrorInfo 與原因鏈區分無資料、網路、登入、授權、解析及密碼更新；無法確認連線的錯誤不保存為成功的空資料。
- `debug_trace.py`／`debug_evidence.py` 在記憶體暫存當次 SDK 操作的時序與回應，成功只保存彙總；異常或 SDK 恢復時才保存詳細證據。平台保留 SDK 原始失敗、恢復結果與原因鏈，遮罩憑證值、標示缺檔及截斷，以 SHA-256 與相對檔名引用。`/api/accounts/<id>/reviews/diagnostics/export` 僅打包該帳號已核對的診斷引用，供 SDK 離線重現。容量、紀錄策略與 ZIP 內容見 [DEBUG 說明](DEBUG.md)。
- 分析任務的 run journal 保存模組名稱與代碼，舊 journal 從帳號 `analysis_runs.options.modules` 一次批次補讀，不載入完整病人結果。爬蟲紀錄只提供分析續跑及 DEBUG，檢閱由所屬工具處理。
- Google 使用官方 `google-auth`，先預覽逐格差異，再明確套用、核對原值及讀回驗證。不同帳號指向同一刀表的寫入會序列處理。
- 即時刀房由瀏覽器開啟院內網址；iframe 按需載入。它有獨立的院方登入流程，不由後端代理，也不自動注入帳密。

## 維護範圍

來源、合成測試、文件與可重現工具進入 Git；建置檔、執行資料與個人診斷保持本機。`tools/verify_repository.py` 檢查已追蹤及未提交新檔案，`tools/clean.ps1` 只處理指定可重建目標並可預覽。[開發與清理](DEVELOPMENT.md)、[驗證](VALIDATION.md)、[效能量測](PERFORMANCE.md)。

後續可逐步將 `review.py` 的任務執行與 `bot_server.py` 的路由表拆成獨立服務，沿用目前明確的 Workspace 呼叫與交易界線；爬蟲紀錄家族搜尋與 TAG 群組仍有遍歷摘要的成本，資料量再增加時可用合成量測決定是否加入更多 SQL 篩選。這些工作不需要改變每帳號資料庫與院內操作序列化的架構。
