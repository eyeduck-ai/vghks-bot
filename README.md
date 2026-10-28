# VGHKS-bot

[![Windows build](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml/badge.svg)](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml)

VGHKS 院內系統的本機工作台：Windows 單檔 EXE、瀏覽器介面與 SQLite，透過 [vghks-sdk](https://github.com/eyeduck-ai/vghks-sdk) 存取院內功能。版本 **6.15.1**，共用刀表契約 **1.3.0**。

## 下載與更新

1. 從 [Releases](https://github.com/eyeduck-ai/vghks-bot/releases/latest) 下載 `VGHKS-bot.exe` 或附文件的 Windows x64 ZIP。
2. 放在可寫入的資料夾後執行，瀏覽器會開啟本機介面。院內功能需要醫院網路及有效帳號；Google 刀表可獨立使用。
3. 更新前用側欄「結束程式」關閉舊版，更換 EXE 並保留完整 `VGHKS-bot-data/`。

關閉瀏覽器不會停止程式；再次執行 EXE 可開啟現有介面。多帳號各自保存資料、任務與院內 session。

**日曆監控已移至 [vghks-sheet2cal](https://github.com/eyeduck-ai/vghks-sheet2cal)**，由指定主機常駐執行；本 repo 與發行包只包含主程式。

## 功能

| 系統 | 功能 |
| --- | --- |
| 門診與檢閱 | 掛號名單、病人集合、SOAP、歷年醫囑／數值、掃描病歷、附件及備註 |
| 進階工具 | 病人轉送、白內障術前分析、Google 刀表預覽及每小時 SOAP 更新 |
| 手術 | 即時刀房、指定醫師排程、未來與過去分區 |
| 線上刀表 | 月份／刀日、永久 ID、統計、水晶體清單、原生年度封存及離線待處理操作 |
| 審查與薪資 | 查詢、快取、版本、監控及匯出 |
| 資料庫 | 本機保存、離線檢閱、備份、匯入及切換 |

月份患者日期與院區取自上方刀日；患者醫院欄可留白或手填，程式不再補齊或覆寫。主程式與獨立監控使用同一固定版本的 `vghks-sheet-core`；GAS 以相同合成案例驗證。[刀表操作與共用規格](docs/SHEET_SYNC.md)

## 資料與文件

資料預設存放在 EXE 同層的 `VGHKS-bot-data/`。搬移及備份須攜帶完整資料夾；憑證解密資訊也保存在資料庫，請依私有資料保管。發布包不包含帳密、Google 金鑰、個人刀表設定或患者資料。

- [操作說明](docs/USER_GUIDE.md)
- [Google 服務帳戶設定](docs/GOOGLE_SHEETS_SETUP.md)
- [刀表、協作與封存](docs/SHEET_SYNC.md)
- [架構](docs/ARCHITECTURE.md)、[開發](docs/DEVELOPMENT.md)、[驗證範圍](docs/VALIDATION.md)
- [發布流程](docs/RELEASING.md)、[版本紀錄](CHANGELOG.md)
- [效率量測](docs/PERFORMANCE.md)、[DEBUG](docs/DEBUG.md)

## 建置

Windows x64、Python 3.11、Git 與 Node.js 24：

```powershell
./build.ps1
```

完整合成測試、來源自測、EXE 自測及封裝核對通過後，產物位於 `dist/`。CI 的 PR 執行完整驗證；main 另建置主程式，版本標籤沿用同一 commit 的已驗證產物發布。SDK 固定為 0.23.5／`67755b3`，SDK 與共用核心都核對實際完整 Git commit。
