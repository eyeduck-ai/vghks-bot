# DEBUG 與 SDK 重現

從「爬蟲紀錄 → 對應任務 → DEBUG → 下載完整 DEBUG ZIP」取得證據。匯出只讀目前帳號的本機資料，不連線院內系統。舊紀錄未保存的頁面無法補回；新版發生異常後才會產生下面的證據。

## 平時與出錯時的紀錄

成功操作只保留服務互動摘要與 SDK 計數，SDK 每 50 次成功或距前次彙總 60 秒後有操作完成時寫入彙總，登出時也會補寫。HTTP 時序及回應 bytes 暫存在記憶體，正常查詢完成後釋放，不逐頁解析 DOM 或保存成功臨床來源。

HTTP／網路異常、解析失敗、資料不完整、重試或 session 恢復會保存該次操作的詳細時序。內容包含操作／請求編號、父子操作、前置檢查、請求欄位名稱、HTTP 狀態、耗時、轉址路徑與查詢欄位名稱、重試等待、重新登入、SDK 錯誤原因鏈及失敗頁面結構。前 8 次操作的摘要作為背景，成功來源不保存。必要時附上同一次查詢、60 秒內且小於 16 KiB 的前置登入檢查回應。

SDK 在內部重試或重新登入後成功，也會留下異常診斷並標記恢復結果。帳號重建造成不同 SDK 工作階段時，使用相同 `interaction_id` 連結，`recovery` 保存新工作階段與驗證／讀取摘要。

6.6.46 起使用 SDK 0.22.6。WebMAAS timeout 由 SDK 在原操作內重建一次 SSO，平台不另建帳號；`application_session_recovery_started` 記錄原 `WEBMAAS_SESSION_TIMEOUT` 及安全欄位，須與相同 `operation_id` 的完成狀態對照。成功診斷的 `ErrorInfo.cause` 保留原 timeout，恢復失敗則保存 SDK 的最終代碼與原因鏈。SDK 的 `capture_operation_id` 若存在也原樣保留於操作時序，便於連結 SDK 原始錄製；平台預設使用自己的失敗證據收集器。

## ZIP 內容

| 檔案 | 用途 |
|---|---|
| `debug.json` | 所選任務／病人的診斷、查詢範圍、錯誤原因、程式位置、出錯欄位／表達式、恢復結果及證據完整性 |
| `sdk-context.json` | 每筆診斷的程式／SDK 版本、操作及請求關聯、來源編碼、恢復工作階段；另含匯出環境的 Python、平台及架構 |
| `evidence/<工作階段>/operation-*.jsonl` | 異常操作的詳細時序，依 `sequence` 排序；較晚才發現的資料驗證錯誤也會保存 |
| `evidence/<工作階段>/response-*.html` | UTF-8 失敗來源；JSON 也沿用此副檔名，可交給 SDK 純解析函式 |
| `evidence/<工作階段>/response-*.bin` | 無遮罩時為原始 bytes；遮罩後為 UTF-8 bytes，`binary_encoding` 會標示 |
| `evidence/<工作階段>/diagnostics.jsonl`、`summary.json` | 所選事件的工作階段摘要與版本；未登出時可能尚無 `summary.json`。舊版時序保留相容內容 |

密碼、Cookie／Authorization 值、token 與 session 值會遮罩；時序不保存原始請求內容或 URL 查詢值。失敗來源仍可能含病人醫療資料，留在本機，不會自動傳送至 SDK 或外部服務。原始 HTML 不在程式中執行。

## 容量與完整性

| 項目 | 上限 |
|---|---:|
| 每份回應來源／來源檔 | 4 MiB |
| 單一操作暫存回應 | 8 MiB／最近 8 份 |
| 單一操作時序 | 512 個暫存事件／2 MiB 檔案 |
| 單一操作回應證據 | 16 MiB／最多 8 份回應 |
| 每次 ZIP 匯出 | 1,000 筆診斷／128 MiB 未壓縮證據 |

`transport.evidence.responses` 標示各來源的 `redacted`、`truncated`、編碼、用途與 SHA-256 檔案摘要；`complete`、`capture_error`、`dropped_events` 及 `omitted_buffer_responses` 說明保存限制。沒有 HTTP 回應的網路錯誤不會拿前一個成功回應冒充失敗來源。

`export.missing_files` 列出缺檔、摘要不符或超出匯出容量的內容，`omitted_diagnostics` 表示超出診斷數量上限，`incomplete_evidence` 列出不完整事件。達上限會明確標示；舊證據不會因查詢成功而自動刪除。

## 交給 SDK 定位問題

先用 `debug.json` 的錯誤碼、階段、原因鏈確認是登入／連線、HTTP、解析或病人資料驗證問題，再用 `sdk-context.json` 的版本與 `interaction_id` 找出對應時序和恢復結果。HTTP 200 也可能是登入或錯誤頁，應查看最終轉址路徑與失敗 `.html`，不能只看 HTTP 狀態。

例如 `WEBMAAS_QUERY_FORM_MISSING` 可將保存的 UTF-8 來源傳給相同 SDK 版本的 `parse_query_form(source, "RSV11WForm")`；`JS_EXPRESSION_UNSUPPORTED` 可傳給 `parse_clinical_orders`。不需要連線院內或執行頁面 JavaScript。遮罩或截斷可能影響部分重現，須一併檢查完整性標記。

## 成功紀錄成本量測

2026-10-05，以 100 次成功操作、每份約 256 KiB 的合成 HTML，比較 SDK 0.22.2 逐筆寫入 HTTP／DOM 結構與新版收集器，三次量測中位數如下。只量測紀錄功能，不含院內網路、業務解析或資料庫保存。

| 記錄方式 | 耗時 | 寫入量 | 成功來源檔 |
|---|---:|---:|---:|
| SDK 逐筆 HTTP／結構 | 581.1 ms | 189,278 bytes | 0 |
| 新版異常才保存細節 | 14.17 ms | 2,224 bytes | 0 |

重現：`python tools/benchmark_debug.py`，結果保存在 `.build/debug-benchmark.json`。結構性驗證由 `tests/test_debug_trace.py`、`tests/test_patient_lookup.py` 與原始碼／EXE 自測檢查，不以耗時門檻判定測試成敗。
