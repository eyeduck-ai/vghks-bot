# VGHKS-bot

[![Windows build](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml/badge.svg)](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml)

VGHKS 院內系統的本機工作台。使用 Windows 單檔 EXE、瀏覽器介面與 SQLite，透過 [vghks-sdk](https://github.com/eyeduck-ai/vghks-sdk) 整合門診、手術排程、審查及薪資資料。這是非官方工具，院內功能需要可連線的醫院網路及有效帳號。

## 下載與開始使用

1. 從 [Releases](https://github.com/eyeduck-ai/vghks-bot/releases/latest) 下載 `VGHKS-bot.exe`，或下載附文件的 Windows x64 ZIP。
2. 將 EXE 放在可寫入的資料夾後執行，瀏覽器會開啟本機介面。
3. 輸入院內帳密；成功後可記住登入資訊。多帳號各有獨立資料、任務與院內 session，可直接切換。
4. 以側欄「結束程式」離開。更新時先關閉舊程式，再換 EXE，保留原有資料夾。

Windows x64 ZIP 同時提供主程式與 `VGHKS-calendar-monitor.exe`。需要刀表同步日曆時，在固定主機另行執行監控程式並匯入服務帳戶、刀表及日曆設定；監控不需要院內登入。主程式的「手術系統 → 線上刀表管理」也可在沒有病人清單或院內連線時使用 Google 刀表。[完整同步與離線規則](docs/SHEET_SYNC.md)

6.9.0 將執行歷史、重試、完成紀錄與完整操作備份保存在本機，沿用刀表既有識別欄與單一共享基準分頁。已確認的雲端批次標記會回收；監控沒有資料變動時只讀取兩輪完整識別，不再逐位病人重讀整本。更新時請一併更換兩個 EXE，保留各自完整資料目錄；6.9.0 建立的操作明細與備份由 6.9.0 以上版本讀取。

關閉瀏覽器分頁後，程式與背景任務會繼續執行。再次雙擊同一個 EXE 即可重新開啟目前介面，不必記住本機網址或埠號。此功能自 6.6.51 起提供；首次更新仍需先結束舊版程式。

6.11.0 的刀表連線設定可從同一資料庫其他帳號帶入刀表目標與服務帳戶，複製後各自獨立保存。手術、FU 新增及手動刀日預設採用目前帳號院區，也可在預覽前手動修改；既有排程位置與 FU 人工資料保留。[刀表操作說明](docs/SHEET_SYNC.md)

6.12.1 的歷年醫囑支援已核對的舊病歷號與就診，保留原始參照及病人身分核對；少數資料解析失敗時仍顯示其他可確認結果，續跑只重查未完整的類別。抓取中可先檢閱已存資料，載入動畫與等待秒數持續顯示。SDK 0.23.5 固定於 commit `67755b3`，建置同時核對實際 SDK commit。掛號紀錄保留預計時間、掛號／取消人員與時間、註記及原始欄位，明確未到診與未知分開顯示。[掛號紀錄說明](docs/USER_GUIDE.md#帳號與門診工具)

CI 將完整測試拆成四組並行，main 打包並自測兩個 EXE；正式 Release 直接發布同一 commit 已驗證的產物。配套 GAS 2026.10.15 與兩個 EXE 交叉驗證新增月份、刀日、院區、跨月搬移及年度原生封存。[發布流程](docs/RELEASING.md)

發布包不含帳密、Google 金鑰、個人刀表網址或病人資料。Google 刀表需自行設定。[服務帳戶設定](docs/GOOGLE_SHEETS_SETUP.md)

## 功能

6.12.0 新增每月手術統計：LenSx、CATA、各術式及水晶體型號眼數，OU 計 2、取消列排除；年度封存可補入較早月份，與 GAS 共用兩階段核對及封存互斥標記。[使用規則](docs/SHEET_SYNC.md#不需病人名單的刀表管理)

| 系統 | 功能 |
| --- | --- |
| 門診 | 含掛號序號的日期掛號清單、多筆手動病人輸入、病人集合、結構化 SOAP、歷年數值／醫囑／掃描病歷、病人備註與檔案比較 |
| 進階工具 | 病歷檢閱直接轉送、各工具持久待處理清單、白內障術前分析、Google 刀表預覽與每小時 SOAP 更新 |
| 日曆監控 | 獨立 `VGHKS-calendar-monitor.exe`，以永久排程 ID 同步整列搬移、跨月剪貼及日曆事件 |
| 手術 | 即時刀房自動內嵌、預設本人並可指定醫師卡號的手術排程、未來與過去分區 |
| 線上刀表管理 | 無需病人名單建立月份／刀日、修復同步結構、過去月份封存、水晶體清單及型號數量、斷線快取與待處理操作 |
| 審查 | 單一累積案件表、自動補查新增案件的醫囑名稱、變更歷史；每次啟動首次進入後才開始持續監控 |
| 薪資 | 保存院方目前公布報表、內容版本、監控、CSV／JSON 匯出 |
| 資料庫 | 本機保存、離線檢閱、備份、獨立匯入、多資料庫切換 |

## 資料存放

```text
VGHKS-bot.exe
VGHKS-bot-data/
  databases.json
  datasets/<資料庫>/
    accounts.sqlite3
    accounts/<帳號>/clinical.sqlite3
    accounts/<帳號>/diagnostics/
    accounts/<帳號>/assets/
VGHKS-calendar-monitor.exe
VGHKS-calendar-data/
```

預設儲存在各 EXE 同層；主程式使用 `VGHKS-bot-data`，監控另用 `VGHKS-calendar-data` 保存設定、憑證與同步紀錄。升級時保留兩個資料目錄；搬移時攜帶對應 EXE 與完整資料目錄。憑證採可攜式保存，解密資料也在資料庫內，因此持有整份資料的人可以使用這些憑證。若開啟外部位置的資料庫，也要另外攜帶該資料夾。

## 文件

- [完整操作說明](docs/USER_GUIDE.md)
- [開發、測試與本機打包](docs/DEVELOPMENT.md)
- [系統架構與資料範圍](docs/ARCHITECTURE.md)
- [GitHub CI 與 Release 發布](docs/RELEASING.md)
- [驗證範圍與院內驗收](docs/VALIDATION.md)
- [效率驗收與量測紀錄](docs/PERFORMANCE.md)
- [DEBUG 內容與 SDK 離線重現](docs/DEBUG.md)
- [版本紀錄](CHANGELOG.md)
- [病人轉送、刀表與日曆監控](docs/SHEET_SYNC.md)

## 開發快速開始

需要 Windows x64、Python 3.11、Git 與 Node.js 24。從 clone 後的 repository 根目錄執行：

```powershell
./build.ps1
```

它會建立 Python 環境、安裝鎖定依賴，執行測試、打包、EXE 自測及封裝核對，輸出至 `dist/`。專案資料夾搬移或改名後，會自動重建含舊路徑的 `.venv`；也可用 `./build.ps1 -RebuildEnvironment` 明確重建。測試只使用合成資料，不需院內或 Google 帳密。SDK 與第三方依賴的授權聲明隨發布包提供，也內嵌於 EXE。

`main` 的每次 push 也會在 GitHub Actions 產生可下載的測試 Artifact；正式 Release 只有推送與程式版本相符的標籤後才建立。本機 EXE 可先帶至內網驗收，不必等待 GitHub 建置。

清理可重建檔案時，可執行 `./tools/clean.ps1 -KeepValidationReports -OldReleases -BrowserArtifacts -WhatIf` 預覽，再移除 `-WhatIf` 套用。它保留最新兩版 ZIP、EXE、驗證報告、Python 環境、資料庫與診斷原檔。[清理範圍](docs/DEVELOPMENT.md#清理與本機檔案)
