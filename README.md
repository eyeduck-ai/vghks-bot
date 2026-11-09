# VGHKS-bot

[![Windows build](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml/badge.svg)](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml)

VGHKS 院內系統的本機工作台，提供 Windows 單檔 EXE、瀏覽器介面及 SQLite 儲存。版本 **6.19.1**，使用固定的 SDK **0.24.2** 與共用刀表契約 **1.3.0**。

## 下載與更新

1. 從 [Releases](https://github.com/eyeduck-ai/vghks-bot/releases/latest) 下載 `VGHKS-bot.exe` 或完整 Windows x64 ZIP，放在可寫入的資料夾。
2. 更新前按側欄「結束程式」，更換 EXE 並保留同層完整的 `VGHKS-bot-data/`。
3. 執行 EXE 開啟本機介面。院內功能需要醫院網路及有效帳號；Google 刀表可獨立連線。

關閉瀏覽器不會停止程式，再次執行 EXE 可重新開啟介面。多帳號各自保存資料、憑證與院內工作階段。資料目錄含可攜式憑證及解密資訊，請連同病歷視為私人資料保管；發布包不包含這些內容。

## 功能

| 區域 | 主要用途 |
| --- | --- |
| 門診與病歷檢閱 | 掛號清單、病人集合、SOAP、按需查閱基本資料／歷年檢查、報告及備註 |
| 進階工具 | 視網膜比較、白內障術前分析、附件比較與離線匯出 |
| 手術與刀表 | 即時刀房、醫師排程、SOAP 刀表預覽／更新、月份／刀日、統計及原生封存 |
| 審查與薪資 | 查詢、版本、監控與匯出 |
| 資料庫 | 離線檢閱、備份、唯讀開啟、匯入及切換 |

病人清單依內容調寬，TAG 最多顯示兩行，其餘以「+N」查看；功能面板及檢閱清單均可用邊線小箭頭收合。醫囑附件逐份驗證、保存及顯示，同份報告的指定附件免額外等待，其他爬蟲節流與病人核對保留。

醫囑報告仍以 JPG／PDF 為主要檢視方式；病歷檢閱、視網膜比較與白內障分析另提供「開啟本地 PACS」，在同一台 Windows 工作站交接已啟動的院方 SmartIris／DownloadApp。使用方式與必要條件見[操作指南](docs/USER_GUIDE.md#本地-pacs-檢閱)。

主程式只更新刀表；日曆監控由獨立 [vghks-sheet2cal](https://github.com/eyeduck-ai/vghks-sheet2cal) 的指定主機執行。

## 文件

- [操作與問題處理](docs/USER_GUIDE.md)
- [Google 刀表連線設定](docs/GOOGLE_SHEETS_SETUP.md)
- [刀表更新、協作與封存](docs/SHEET_SYNC.md)
- [開發、架構、驗證及發布](docs/DEVELOPMENT.md)
- [版本紀錄](CHANGELOG.md)

## 開發與建置

Windows x64、Python 3.11、Git、Node.js 24：

```powershell
./build.ps1
```

完整測試、來源及 EXE 自測、封裝驗證通過後，產物位於 `dist/`。CI 對 PR 執行完整驗證，`main` 成功後建置 EXE；版本標籤沿用同一 commit 的已驗證產物發布。SDK 與共用核心均固定完整 Git commit，執行時不自動升級。
