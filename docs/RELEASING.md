# 建置與版本發布

本 repo 只建置、測試及發布 `VGHKS-bot.exe`。獨立監控的版本與部署見 [vghks-sheet2cal](https://github.com/eyeduck-ai/vghks-sheet2cal)。

| 觸發 | 驗證與產物 |
| --- | --- |
| PR 到 main | 四組完整 Windows 測試、來源自測、Ruff／JS 及固定 GAS 契約 |
| main push | 相同驗證成功後，建置主程式、EXE 自測、封裝核對及 Artifact |
| 手動 windows.yml | 完整來源與測試驗證 |
| v* 標籤 | 下載同一 commit 已成功的 main 產物，核對後發布，不重建 |

## 來源與產物核對

四組測試的完整探索名單須相同，且每項恰好完成一次；缺漏、重複或失敗均停止建置。來源及 EXE 自測都必須成功。

`tests/fixtures/gas_contract.json`、pyproject 與建置鎖固定相同共用核心 commit。CI checkout 該 GAS source，執行真實解析、寫入與封存匯出器；建置核對實際安裝的 SDK 與核心 Git 來源，EXE 再核對內嵌規格、案例、靜態檔、空白憑證與授權聲明。

`BUILD_INFO.json` 綁定 repository、main commit、Git tree、GAS／核心 revision、run ID／attempt、版本及雜湊。發布只接受同倉庫同 commit 的成功 main run，核對 EXE／ZIP 的 SHA-256、ZIP 內主程式與三份驗證報告。舊監控 EXE 或報告混入主程式 ZIP 時拒收。

Artifact 保留 14 天；正式 Release 是長期下載入口。產物過期時重跑該 commit 的 main workflow，再重跑標籤發布。驗證工作只有讀取權限，發布使用內建 GITHUB_TOKEN 的 contents:write 與 actions:read。

## 發布步驟

1. 同步更新 pyproject、`vghks_bot/__init__.py`，在 CHANGELOG 加入 `## <版本>`。
2. 完成測試並合併 PR，等待該 main commit 的 Windows verification and build 成功。
3. 在同一 commit 建立並推送 `v<版本>` 標籤。
4. 確認 Release workflow 成功，附件有 EXE、ZIP 與 SHA256SUMS.txt。

本機完整建置：

```powershell
.venv/Scripts/python.exe tools/run_checks.py --build --gas-repo .build/gas-contract
```

可用 `--checks-only`、`--tests-only`、`--source-only` 分段檢查；單組測試不代表完整通過。發布不覆蓋舊版本，修正使用新標籤。

## 更新與驗收

結束舊主程式，更換 EXE，保留完整 `VGHKS-bot-data/`。以 `Get-FileHash .\VGHKS-bot.exe -Algorithm SHA256` 比對校驗檔。CI 使用合成資料，實際院內回應與權限仍依[驗證範圍](VALIDATION.md)驗收；目前未設定 Windows 程式碼簽章。
