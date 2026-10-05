# VGHKS-bot

[![Windows build](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml/badge.svg)](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml)

VGHKS 院內系統的本機工作台。使用 Windows 單檔 EXE、瀏覽器介面與 SQLite，透過 [vghks-sdk](https://github.com/eyeduck-ai/vghks-sdk) 整合門診、手術排程、審查及薪資資料。這是非官方工具，院內功能需要可連線的醫院網路及有效帳號。

## 下載與開始使用

1. 從 [Releases](https://github.com/eyeduck-ai/vghks-bot/releases/latest) 下載 `VGHKS-bot.exe`，或下載附文件的 Windows x64 ZIP。
2. 將 EXE 放在可寫入的資料夾後執行，瀏覽器會開啟本機介面。
3. 輸入院內帳密；成功後可記住登入資訊。多帳號各有獨立資料、任務與院內 session，可直接切換。
4. 以側欄「結束程式」離開。更新時先關閉舊程式，再換 EXE，保留原有資料夾。

發布包不含帳密、Google 金鑰、個人刀表網址或病人資料。Google 刀表需自行設定。[服務帳戶設定](docs/GOOGLE_SHEETS_SETUP.md)

## 功能

| 系統 | 功能 |
| --- | --- |
| 門診 | 含掛號序號的日期掛號清單、多筆手動病人輸入、病人集合、結構化 SOAP、歷年數值／醫囑／掃描病歷、病人備註與檔案比較 |
| 進階工具 | 視網膜比較、白內障術前分析與掃描病歷、Google 刀表差異預覽與明確套用 |
| 手術 | 即時刀房自動內嵌、預設本人並可指定醫師卡號的手術排程、未來與過去分區 |
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
```

預設儲存在 EXE 同層；搬移時攜帶 EXE 與完整 `VGHKS-bot-data`。憑證採可攜式保存，解密資料也在資料庫內，因此持有整份資料的人可以使用這些憑證。若開啟外部位置的資料庫，也要另外攜帶該資料夾。

## 文件

- [完整操作說明](docs/USER_GUIDE.md)
- [開發、測試與本機打包](docs/DEVELOPMENT.md)
- [系統架構與資料範圍](docs/ARCHITECTURE.md)
- [GitHub CI 與 Release 發布](docs/RELEASING.md)
- [驗證範圍與院內驗收](docs/VALIDATION.md)
- [效率驗收與量測紀錄](docs/PERFORMANCE.md)
- [DEBUG 內容與 SDK 離線重現](docs/DEBUG.md)
- [版本紀錄](CHANGELOG.md)

## 開發快速開始

需要 Windows x64、Python 3.11、Git 與 Node.js 24。從 clone 後的 repository 根目錄執行：

```powershell
./build.ps1
```

它會建立 Python 環境、安裝鎖定依賴，執行測試、打包、EXE 自測及封裝核對，輸出至 `dist/`。專案資料夾搬移或改名後，會自動重建含舊路徑的 `.venv`；也可用 `./build.ps1 -RebuildEnvironment` 明確重建。測試只使用合成資料，不需院內或 Google 帳密。SDK 與第三方依賴的授權聲明隨發布包提供，也內嵌於 EXE。

`main` 的每次 push 也會在 GitHub Actions 產生可下載的測試 Artifact；正式 Release 只有推送與程式版本相符的標籤後才建立。本機 EXE 可先帶至內網驗收，不必等待 GitHub 建置。

清理可重建檔案時，可執行 `./tools/clean.ps1 -KeepValidationReports -OldReleases -BrowserArtifacts -WhatIf` 預覽，再移除 `-WhatIf` 套用。它保留最新兩版 ZIP、EXE、驗證報告、Python 環境、資料庫與診斷原檔。[清理範圍](docs/DEVELOPMENT.md#清理與本機檔案)
