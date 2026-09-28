# 系統架構

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
                 ├─ Approvals / Earnings / SurgerySchedule
                 └─ SQLite、附件及診斷資料
```

| 位置 | 用途 |
| --- | --- |
| `opd_monitor/app.py`、`bot.py`、`bot_server.py` | 程式入口、帳號生命週期及本機 API |
| `opd_monitor/bot_gateway.py`、`clinical_identity.py` | SDK 工作階段、序列操作及病人身分核對 |
| `opd_monitor/review.py`、`review_history.py`、`scanned_records.py` | 檢閱任務、歷史索引與按需掃描附件 |
| `opd_monitor/soap_data.py`、`tags.py` | 結構化 SOAP、tag 與搜尋依據 |
| `opd_monitor/analysis*.py` | 歷年比較、數值、醫囑與附件 |
| `opd_monitor/approvals.py`、`earnings.py`、`surgery_schedule.py` | 三個系統的資料保存與查詢 |
| `opd_monitor/google_sheets.py`、`sheet_plan.py` | Google 授權、逐格差異與寫入核對 |
| `opd_monitor/databases.py`、`bot_store.py`、`library.py` | 資料庫管理及持久儲存 |
| `opd_monitor/static/` | 本機 Web UI，EXE 內嵌；主畫面 `bot.html`，進階工具仍嵌入 `/tools` 的既有比較頁 |
| `tests/`、`opd_monitor/selftest*.py` | 合成單元／整合／凍結後驗證 |

## 帳號與任務

臨床 API 以 `/api/accounts/{account_id}/…` 隔離；本機 Session、CSRF、同源及資料庫上下文檢查共用。每帳號擁有獨立 SQLite、附件及 SDK 工作階段，不跨帳號共用病人快取。`review_history.py` 的歷年索引與所選附件也要先核對任務、病人及伺服器已保存的參照。

同帳號的 SDK 操作序列執行，病人延伸查詢在安全步驟之間插入並重新確認上下文。不同帳號可平行，預設最多三個。關閉／登出保存進度並暫停，重開不自動續跑未完成任務。

審查與薪資監控使用程式內排程器；僅在 EXE 開啟且帳號在線時生效，與 GitHub Actions 無關。GitHub CI 不執行院內抓取或監控。

## 資料

資料格式目前為 1，與應用程式版本分開。原始 SOAP 與版本、任務採用的版本引用、解析數值、醫囑、審查及薪資內容分開保存。手術排程以內容雜湊共用快照，每次取得的時間與任務引用仍保留。

PDF／影像以內容雜湊存檔；刪除會處理無引用附件與衍生資料。刪除狀態避免中斷任務續跑後還原已刪內容。移除集合不等於刪除病歷。

帳密與 Google 金鑰以可攜式形式保存在資料庫，解密材料也一同保存。瀏覽器與任務日誌不回傳密碼或私鑰；完整資料目錄應視為包含憑證的私人資料。

## 外部整合

- SDK commit 同時固定在專案與建置鎖定檔。聯醫掛號紀錄走 WebMAAS SSO；PRQ 就診與 SOAP 遇院方調閱審查時，由 SDK 核對並按需送出「了解病情」。SDK 在送出前可重登，送出後不得由帳號閘道重播整筆 PRQ 讀取。錯誤診斷隨帳號資料庫保存。
- Google 使用官方 `google-auth`，先預覽逐格差異，再明確套用、核對原值及讀回驗證。不同帳號指向同一刀表的寫入會序列處理。
- 即時刀房由瀏覽器開啟院內網址；iframe 按需載入。它有獨立的院方登入流程，不由後端代理，也不自動注入帳密。
