# 開發、架構、驗證與發布

## 環境與目錄

使用 Windows x64、Python 3.11、Git 與 Node.js 24。依賴固定於 `requirements-build.lock.txt`，SDK 0.24.4／`a0416ae129b57dfc1a0ad1981318b610ef696101` 與共用核心 1.3.0／`3e4babe8bbb799d103372732bd2e45bc1405fb83` 也固定於 `pyproject.toml`；不可只按版本號判定安裝來源。

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.lock.txt
.venv/Scripts/python.exe run.py --data-dir ./VGHKS-bot-data
```

| 位置 | 用途與保留原則 |
| --- | --- |
| `vghks_bot/` | 正式 Python 後端與 `static/` 介面，不能當暫存清除 |
| `tests/`、`tools/` | 合成測試、契約、建置、驗證及量測工具 |
| `docs/` | 操作、Google 設定、刀表規則及本文四份文件 |
| `.github/` | 完整 Windows 驗證及已驗證產物發布流程 |
| `.venv/` | 可重建的固定依賴環境，通常保留以免重裝 |
| `.build/` | 建置、測試、合成資料及驗證暫存；清理前分辨仍需的 helper、checkout、報告與備份 |
| `output/`、`.playwright-cli/` | 本機截圖、瀏覽器快照及日誌，不加入 Git |
| `.local/` | 需保留的私人診斷、歷史封存與本機備份，不加入 Git |
| `dist/` | 當前 EXE、配套驗證檔及正式 ZIP；更新須先核對相同來源與 hash |
| `VGHKS-bot-data/` 或外部資料庫 | 真實帳密、病歷、附件及工作狀態，不是建置快取 |

工作區改名／搬移後，`.venv` 啟動腳本可能仍指向舊路徑。`./build.ps1` 會核對路徑及依賴，需要時重建；也可明確使用 `-RebuildEnvironment`。資料目錄及外部資料庫路徑另行保留。

## 架構與不變條件

```text
瀏覽器 → 127.0.0.1 本機 HTTP → DatabaseManager／BotApplication
  → 每帳號 Workspace
      ├─ AccountGateway → 固定版本 vghks-sdk → 院內系統
      ├─ Review／ReviewHistory／Analysis／ScannedRecords
      ├─ Approvals／Earnings／SurgerySchedule
      └─ SQLite、附件、診斷；SheetService → Google Sheets
```

| 模組 | 責任 |
| --- | --- |
| `app.py`、`bot.py`、`bot_server.py`、`launcher.py` | 啟動、帳號及資料庫生命週期、本機 API 與可信重連 |
| `bot_gateway.py`、`clinical_identity.py`、`encoding.py` | 同帳號序列操作、完整病人／就診核對、節流及回應解碼 |
| `review*.py`、`analysis*.py`、`scanned_records.py` | SOAP、歷史檢查、按需報告與完整性／續跑 |
| `databases.py`、`bot_store.py`、`library*.py`、`migrations.py` | 帳號 SQLite、快照、版本、索引與可回復更新 |
| `sheet_service.py`、`sheet_*.py` | 連線、共用寫入鎖、差異預覽、永久 ID、核對及封存 |
| `debug_*.py`、`diagnostics.py`、`connection_state.py` | 異常證據、錯誤分類及恢復結果 |
| `static/` | 正式 UI；主畫面與嵌入 `/tools` 比較頁皆需保留 |

- 帳號、資料庫、病人與完整就診 selector 都屬於資料身分。快取、進行中查詢合併及延遲回應必須依完整範圍隔離；來源切換後不採用舊結果。
- 同帳號院內操作序列化，保留任務／連線上限、SDK 登入恢復、取消檢查、429／503 退避及 `Retry-After`。跨任務共享取得不增加併發或延長有效期。
- 成功回應在帳號序列鎖內使用嚴格遞增時間戳，避免同刻或回退的系統時鐘讓較舊回應覆蓋新快取；各任務仍保留自己的取得快照。
- 只有正確 PRQ 來源、報告附件範圍內的指定影像／PDF GET 使用零額外等待；仍逐一下載，不修改共享帳號政策或 SDK 私有欄位。
- 附件通過格式／身分核對後原子保存、逐份發布並更新 revision。部分解析、失敗或缺檔不算完整快取；普通重試只補未完成分支，強制更新另行取得。
- 病歷檢閱按需讀取 40 位摘要及選定病人 SOAP；虛擬列以實測高度保留錨點，畫面／本機鄰近預讀不得自動抓取全員基本資料、歷年數值或醫囑索引。
- SQLite 保存原始內容、版本、任務引用與可重建摘要；WAL 快照讀取及批次交易維持一致。清除先預覽並重新核對引用，集合、手動 TAG、備註與診斷保留。
- 本機 HTTP 的 Host／Origin／啟動憑證與資料庫切換租約必須保留；病人文字只當文字顯示，失敗 HTML 不執行。憑證及解密材料僅存本機，不回傳瀏覽器或公開日誌。
- `pacs.py` 只接受已核對清單中的指定醫囑；`/reviews/pacs/open` 為須 Session／CSRF／目前資料庫 context 的明確 POST。先唯讀預檢 DownloadApp，再按需補查所選醫囑的原生參照；舊快取只補明細／文字分支，不重新下載 JPG。JPEG 參照不轉成原生參照，多筆要求明確選擇。
- PACS 準備沿用帳號 SDK 序列鎖，本機交接使用 SDK 的獨立 Session 與跨程序鎖，平台另以全帳號鎖阻擋重複進行中的操作。不快取啟動 context、不回傳 Study／IP／URL，不自動重送、啟動或安裝 EXE；`HANDOFF_SENT` 與影像載入確認分開。

## 測試與瀏覽器驗收

```powershell
# 首次下載固定版本 actionlint，核對 SHA-256 並驗證有效／無效工作流程。
.venv/Scripts/python.exe tools/check_workflows.py --install --self-test

# 來源、依賴、Ruff、JavaScript 與固定 GAS 契約。
.venv/Scripts/python.exe tools/run_checks.py --checks-only --gas-repo .build/gas-contract

# 完整合成測試與來源自測；不建置 EXE。
.venv/Scripts/python.exe tools/run_checks.py --tests-only

# 指定 Python 測試，或單獨執行來源自測。
.venv/Scripts/python.exe -m unittest discover -s tests -p test_review_paging_ui.py -v
.venv/Scripts/python.exe tools/run_checks.py --source-only

# 一次性合成瀏覽器工作區；不連院內。
.venv/Scripts/python.exe tools/browser_bot_fixture.py --review-patients 1000 --report-gallery --demographics --local-board

# 四個合成帳號：不同院區、同院區長名稱與需要密碼的帳號。
.venv/Scripts/python.exe tools/browser_bot_fixture.py --account-switcher --sheet-management --local-board
```

fixture 印出本機 URL；結束測試服務後清除其暫存資料庫。`--lookup-failures --lookup-delay 5` 可驗收慢病人查核，`--registrations` 可驗收掛號。工具旗標與個別模式以 `--help` 為準。

非同步測試共用 `tests/async_helpers.py`，以單調時計的共同期限等待事件、任務及執行緒。測試由明確事件控制先後順序，`finally` 釋放阻擋並檢查收尾；逾時只作故障保護，會提供等待標籤、安全狀態及相關執行緒堆疊，不輸出區域變數。診斷回呼只讀不會阻塞的事件／計數，避免在逾時後再等待同一把鎖。

回歸重點包括帳號／病人身分拒收、登入恢復與不可重送操作、部分失敗及續跑、缺檔補抓、force 分流、同秒 revision、WAL／交易回復與延遲回應。瀏覽器須實際驗收桌面／窄畫面、鍵盤／觸控、TAG 兩行收合與數量、SOAP 捲動錨點、模態比較及漸進附件；圖片、PDF 節點與勾選不因更新重建。

所有公開測試只使用合成資料、一次性金鑰及暫存資料庫。院內權限／真實回應、Google 分享／保護範圍及實際寫入須另在授權環境及測試副本驗收；合成測試不代表取得這些權限。日曆網路操作與常駐服務由獨立 [vghks-sheet2cal](https://github.com/eyeduck-ai/vghks-sheet2cal) 驗證。

## 建置、CI 與發布

```powershell
./build.ps1
# 已安裝依賴時，可直接執行相同完整流程。
.venv/Scripts/python.exe tools/run_checks.py --build --gas-repo .build/gas-contract
```

完整流程：actionlint → 來源／版本／安裝 commit → pip／Ruff／JS → 82 項共用 GAS 契約 → 完整測試 → 來源自測 → PyInstaller → EXE 自測 → 內嵌靜態檔／空白憑證／授權核對 → ZIP／SHA-256。`tools/build_exe.py` 單獨打包不等於完整通過。建置只納入 Git 索引中的靜態檔與文件，新公開檔案須先加入索引。

actionlint 固定為 1.7.12，下載壓縮檔及解出的工具均核對固定 SHA-256，快取位於 `.build/`。`build.ps1` 與 CI 來源驗證工作在安裝依賴前完成預先檢查；一般 `run_checks.py --checks-only` 使用已驗證快取離線檢查。`--self-test` 實際驗證正確 YAML、錯誤 YAML 及錯誤表達式必須得到預期結果；診斷寫入 `.build/ci/actionlint.json`，CI 即使失敗也保留。

| 觸發 | 門檻與產物 |
| --- | --- |
| PR 到 `main` | 四組完整 Windows 測試、來源自測、語法與固定 GAS 契約 |
| `main` push | 同樣驗證通過後建置、EXE 自測、封裝核對與 Artifact |
| 手動 `windows.yml` | 完整來源及測試驗證 |
| `v*` 標籤 | 下載同 commit 已成功的 `main` 產物，核對後發布，不重建 |

四組依 `tools/test-timings-baseline.json` 的實測類別耗時均衡分配，同類別保留相同 setup／teardown；彙總器獨立重算計畫，完整探索名單須相同，每項恰好完成一次。缺漏、重複、分組指紋不符或任一失敗都停止建置。大量 fixture 可用 `Library.batch()`，但須在被測操作、背景執行或等待開始前提交，保留真實 SQLite／原子保存驗證。

`BUILD_INFO.json` 綁定 repository、workflow、commit／tree、run／attempt、固定 GAS／核心、版本、完整測試名單／分組指紋及檔案 hash。下載只接受指定成功執行的同 commit 產物，先在 staging 核對 EXE、ZIP、ZIP 內 EXE、來源／EXE 自測與封裝報告，再備份舊配套檔更新 `dist/`；最後獨立核對 `dist/BUILD_INFO.json` 及實際 EXE SHA-256，不用其他版本代替。

發布時同步更新 `pyproject.toml`、`vghks_bot/__init__.py`、README 與 CHANGELOG，完成該 commit 的 `main` 驗證後再推送版本標籤。確認 Release workflow 及 EXE／ZIP／校驗檔齊全。Artifact 保留 14 天，Release 為長期入口；產物過期須使用同 commit 的完整重跑結果。版本不覆蓋，修正使用新版本。目前沒有 Windows 程式碼簽章。

更新本機 EXE 前先結束舊程式，保留完整資料目錄。下載後可用 `Get-FileHash .\dist\VGHKS-bot.exe -Algorithm SHA256` 獨立核對；只需驗證下載時不必執行成品。

## DEBUG 與問題定位

由「爬蟲紀錄 → DEBUG → 下載完整 DEBUG ZIP」匯出當前帳號／任務／病人的已存證據，不另發院內請求。正常成功只保存彙總，詳細回應／時序在有上限記憶體暫存，異常、部分解析、重試或 SDK 恢復才保存；重試成功不刪除原失敗證據。

| ZIP 內容 | 判讀重點 |
| --- | --- |
| `debug.json` | 階段、範圍、錯誤碼／原因鏈、受影響項目及恢復結果 |
| `sdk-context.json` | 程式／SDK／Python 版本、平台、操作／請求關聯及編碼 |
| `operation-*.jsonl` | 依 sequence 對照 HTTP 狀態、耗時、重試等待與重新登入 |
| `response-*.html`／`.bin` | 失敗來源與 SHA-256；PDF／JPEG 保留二進位，不錯當 HTML 解碼 |
| 匯出完整性欄位 | `missing_files`、`incomplete_evidence`、截斷、遺漏及保存失敗 |

文字 `.html` 是採用編碼解碼後的 UTF-8；`.bin` 無遮罩時可保留原 bytes，遮罩時會改為 UTF-8 並標示，不能當成完整原始回應。來源可能含醫療資料，與資料庫一樣留在私人環境；時序不保存 URL 查詢值、原始請求或憑證值，原始 HTML 不執行。

`TEXT_DECODE_INCOMPLETE` 保留 HTTP／meta charset、SDK 選擇的編碼及替代字元證據。只記固定替代字元標記的 hex 或非法序列長度遮罩。這些證據描述整份回應。成功查詢的 SDK 互動摘要另可記錄已解析姓名或 SOAP 的私用區碼位及換算的 Windows CP950 對應，供核對院方字表。兩類摘要均省略原文、鄰近位元組與位置。搭配相同 SDK 版本的純解析函式可離線定位。

每份回應上限 4 MiB，操作暫存最近八份／8 MiB、證據 16 MiB；每次 ZIP 最多 1,000 筆診斷／128 MiB 未壓縮證據。達上限、缺檔或舊版未保存時明確標示，不拿其他成功回應冒充證據。

## 效能驗收

- `tools/benchmark_review.py --sizes 40 1000 5000 20000`：合成資料庫的清單、遠處定位、單人 SOAP、備註及精簡狀態；禁止 socket。
- `tools/benchmark_efficiency.py`：SQL／全文解析量與索引成本；`tools/benchmark_debug.py`：成功紀錄的耗時及寫入量。
- 報告寫入忽略的 `.build/`。冷次與暖態、請求數與掃描輪數、runner 排隊與執行時間分開比較；不以不穩定的耗時門檻取代行為斷言。
- 保留的歷史合成結果：2026-10-15 相同 500 筆醫囑純解析中位數 416.37 → 220.53ms；2026-10-05 成功紀錄收集器 581.1 → 14.17ms。它們不含院內網路及完整業務流程，也不是目前版本重新量測的保證。

## 清理與依賴更新

清理前先結束合成服務及建置／下載工作，按實際用途選取可重建目標。需要保留的診斷、驗收截圖、驗證 helper、原始 checkout 及版本備份移至明確的本機封存位置；不能把真實資料目錄當快取。

```powershell
# 只預覽；確認清單後再依需要選擇目標。
./tools/clean.ps1 -KeepValidationReports -OldReleases -BrowserArtifacts -WhatIf
```

腳本會核對絕對路徑並拒絕連結／junction。`-KeepValidationReports` 只保留 `.build/ci/`，不保留其他 `.build` helper 或 checkout；`-OldReleases` 只保留最新兩版正式命名 ZIP；`-BrowserArtifacts` 涵蓋 `.playwright-cli/` 及 `output/playwright/`。`.venv/`、`.local/`、EXE 與資料目錄不是這些選項的清除目標。不要在仍需使用 helper／驗收檔案時直接移除整個 `.build/`。

更新 SDK／核心時，核對上游變更，再同步修改專案、鎖定檔與 GAS 契約固定 revision，重新安裝並驗證實際 Git 來源，執行完整測試／EXE 自測／封裝。SDK 同版本但 commit 不符也必須重新安裝；CI 不自動追蹤上游 main。來源只提交程式、合成測試、公開預設及這四份重點文件，實際憑證、病歷、HAR、診斷及建置包保持本機。
