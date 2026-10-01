# 系統架構

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
| `vghks_bot/review.py`、`review_history.py`、`scanned_records.py` | 檢閱任務、歷史索引與按需掃描附件 |
| `vghks_bot/soap_data.py`、`tags.py` | 結構化 SOAP、tag 與搜尋依據 |
| `vghks_bot/analysis*.py` | 歷年比較、數值、醫囑與附件 |
| `vghks_bot/library_data.py` | 帳號範圍資料清單、影響預覽、分類清除與附件引用檢查 |
| `vghks_bot/approvals.py`、`earnings.py`、`surgery_schedule.py` | 三個系統的資料保存與查詢 |
| `vghks_bot/google_sheets.py`、`sheet_plan.py` | Google 授權、逐格差異與寫入核對 |
| `vghks_bot/databases.py`、`bot_store.py`、`library.py` | 資料庫管理及持久儲存 |
| `vghks_bot/static/` | 本機 Web UI，EXE 內嵌；主畫面 `bot.html`，進階工具仍嵌入 `/tools` 的既有比較頁 |
| `tests/`、`vghks_bot/selftest*.py` | 合成單元／整合／凍結後驗證 |

## 帳號與任務

臨床 API 以 `/api/accounts/{account_id}/…` 隔離；本機 Session、CSRF、同源及資料庫上下文檢查共用。每帳號擁有獨立 SQLite、附件及 SDK 工作階段，不跨帳號共用病人快取。`review_history.py` 的歷年索引與所選附件也要先核對任務、病人及伺服器已保存的參照。

同帳號的 SDK 操作序列執行，病人延伸查詢在安全步驟之間插入並重新確認上下文。不同帳號可平行，預設最多三個。關閉／登出保存進度並暫停，重開不自動續跑未完成任務。

審查與薪資監控使用程式內排程器；僅在 EXE 開啟且帳號在線時生效，與 GitHub Actions 無關。GitHub CI 不執行院內抓取或監控。

## 資料

資料格式目前為 1，與應用程式版本分開。原始 SOAP 與版本、任務採用的版本引用、解析數值、醫囑、審查及薪資內容分開保存。手術排程以內容雜湊共用快照，每次取得的時間與任務引用仍保留。

PDF／影像以內容雜湊存檔；刪除會處理無引用附件與衍生資料。刪除狀態避免中斷任務續跑後還原已刪內容。移除集合不等於刪除病歷。

門診與進階工具的清除入口集中於「病歷管理」。`/library/data/read`、`/preview-delete`、`/delete` 分別提供分類清單、影響範圍及清除；確認使用資料指紋重新核對，帳號有執行中任務時拒絕清除。就診索引連帶清除依賴的歷史快取，其他類別使相關分析摘要失效；刪除所有版本及結果副本，保留集合、手動 TAG、備註與任務錯誤摘要。`clinical_cache_deletions` 保留明確清除標記，讓工具切頁只讀本機，不自動補抓；前端事件同步清除失效的檢閱及比較狀態。

`progress.js` 以帳號及任務識別維護共用懸浮進度，觀察既有 API 請求及任務輪詢，不另外新增院內請求。內嵌工具經同源及來源視窗核對後將狀態交給主頁；主頁將卡片置入最上層對話視窗。`clinical-ui.js` 共用病歷號複製與 PDF 開啟設定；檔案比較用既有節點及 CSS 排序，避免改變版面時重新載入 PDF。

帳密與 Google 金鑰以可攜式形式保存在資料庫，解密材料也一同保存。瀏覽器與任務日誌不回傳密碼或私鑰；完整資料目錄應視為包含憑證的私人資料。

## 白內障數值與背景佇列

`numeric_measurements.py` 保守解析驗光及 KM，使用 Decimal 計算 SE／Kavg，API values 為可運算數值、exact 保留完整十進位字串。所有來源列、眼別、文字及單位保留，缺值為 null；拆欄與整串形式一致處理，矛盾欄位不推算。

`analysis_steps` 以 `numeric-structured:<source_key>`／kind `numeric_structured` 保存衍生列，包含 source_digest、source_saved_at、parser_version。數值寫入時同一交易保存原始與衍生資料；舊資料讀取時按來源與版本補解析，唯讀只在記憶體解析，並以來源摘要避免並行更新或刪除後寫回舊資料。分類清除涵蓋衍生紀錄，但資料庫清單與清除預覽不重複計數。結果、比較數值欄與 ZIP 離線頁共用十五項分類及結構化格式；CSV 增加解析欄位。

帳號範圍 POST `/api/accounts/{id}/analysis/cataract/queue` 接受 cohort_id、action（start/prioritize/pause/resume）及目前病人 mrn。`cataract/status` 回傳 queue 狀態、pending、active_mrn、priority_mrn、wait_seconds 及 reason。集合與抓取帳號在伺服器核對。

白內障狀態及結果使用 `Library.batch()`：同執行緒的本機讀取共享一份帳號 SQLite 交易，保留帳號鎖及失敗回復；交易不跨院內請求或排程等待。狀態只核對 SOAP 識別及附件存在，不載入病歷全文與版本。`data_revision` 由原始／衍生步驟、SOAP、就診索引、清除標記及附件存在狀態產生，供前端核對已存結果。

`cataract.js` 將結果快取限定於帳號及目前集合，合併重複狀態／病人請求；所選病人的結果讀取不等待狀態輪詢。背景保存改變版本時只使相關病人快取失效，清除與集合切換會丟棄已失效的延遲回應。`analysis.js` 的請求序號使連續切換不會以舊病人的 SOAP 覆蓋新選取；已完成或失敗待續跑的病人不因檢閱而送出 prioritize。

`cataract_queue.py` 的每帳號排程器在獨立執行緒等待，不占原任務工作佇列；SDK 操作仍由 AccountGateway 序列化，沿用連線上限及 session 恢復。每位建立可追溯的分析任務；在已保存的讀取邊界協作暫停，先處理手動選取的病人或原佇列中的前景工作，再接回原病人。背景病人間隨機等待 10–20 秒，成功／失敗的本次讀取記憶避免插隊後重抓或隱含重試。部分失敗須明確續跑，離線或無法恢復連線暫停；重啟從集合的佇列摘要恢復為暫停，需明確繼續。清除標記阻止背景自動補回資料。

## 外部整合

- SDK commit 同時固定在專案與建置鎖定檔。聯醫掛號紀錄走 WebMAAS SSO；PRQ 就診與 SOAP 遇院方調閱審查時，由 SDK 核對並按需送出「了解病情」。SDK 在送出前可重登，送出後不得由帳號閘道重播整筆 PRQ 讀取。錯誤診斷隨帳號資料庫保存。
- `diagnostics.TraceRecorder` 在 SDK 操作內關聯最後一筆請求／回應；臨床頁面解析失敗時保存完整 bytes 與解碼 HTML，診斷以 SHA-256 與相對檔名引用。出錯表達式保留醫療值；SDK 時序不記錄登入請求值。`/api/accounts/<id>/reviews/diagnostics/export` 僅打包該帳號已核對的診斷引用，拒絕越界路徑、核對內容摘要，供離線 SDK 重現。新舊診斷可並存；舊資料缺少的細節不推造。
- 分析任務的 run journal 保存模組名稱與代碼，舊 journal 從帳號 `analysis_runs.options.modules` 一次批次補讀，不載入完整病人結果。爬蟲紀錄只提供分析續跑及 DEBUG，檢閱由所屬工具處理。
- Google 使用官方 `google-auth`，先預覽逐格差異，再明確套用、核對原值及讀回驗證。不同帳號指向同一刀表的寫入會序列處理。
- 即時刀房由瀏覽器開啟院內網址；iframe 按需載入。它有獨立的院方登入流程，不由後端代理，也不自動注入帳密。
