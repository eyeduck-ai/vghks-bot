# CI 與版本發布

Windows EXE 可先在本機執行 `./build.ps1` 建置與離線自測，再帶入內網驗收；推送 GitHub 不是取得測試 EXE 的前提。GitHub Actions 會重新驗證推送的來源碼，不需要把 EXE 提交進 Git。

## 已設定的流程

| 觸發 | 行為 |
| --- | --- |
| Push 到 `main`、Pull Request | 在 Windows runner 測試、打包、EXE 自測；保存下載用 Artifact |
| Actions 手動執行 | 同一套驗證與打包，不發布 Release |
| Push `v*` 標籤 | 先完成全部檢查，再將 EXE、ZIP 與 SHA-256 發布到該標籤的 GitHub Release |

工作流程：[windows.yml](../.github/workflows/windows.yml)。建置工作只有 `contents: read`；發布工作才取得 `contents: write`。使用內建 `GITHUB_TOKEN`，不需要額外 PAT、院內帳密、Google 金鑰或簽章憑證。

程式依賴鎖定版本，官方 Actions 固定 commit。Dependabot 每月提出 Actions 更新。Artifact 保留 14 天；Release 附件是正式下載入口。

## 發布新版本

1. 同步更新 `pyproject.toml` 的版本及 `vghks_bot/__init__.py` 的 `__version__`。
2. 在 `CHANGELOG.md` 最上方加入 `## <版本>` 與該次變更。
3. 在本機完成 EXE 自測及必要的院內驗收，再提交並推送 `main`，等待 Windows build 成功；只推送 `main` 會得到測試 Artifact，不會建立 Release。
4. 需要正式 Release 時，才在已驗證的同一 commit 建立版本標籤並推送：

```powershell
$releaseVersion = (.venv/Scripts/python.exe -c "from vghks_bot import __version__; print(__version__)").Trim()
git tag -a "v$releaseVersion" -m "VGHKS-bot v$releaseVersion"
git push origin "v$releaseVersion"
```

標籤必須等於兩處程式版本；版本不符會停止。發布只接受已存在標籤，檢查失敗不會建立 Release。既有 Release 不會被此流程覆蓋；修正版本應新增標籤。

## 下載與驗證

- 使用者可只下載 `VGHKS-bot.exe`。
- ZIP 另附操作文件、第三方聲明及原始碼／EXE 自測報告。
- 使用 PowerShell `Get-FileHash .\VGHKS-bot.exe -Algorithm SHA256`，比對 `SHA256SUMS.txt`。
- 新版只更換 EXE，保留 `VGHKS-bot-data`；資料格式不相容時依[操作說明](USER_GUIDE.md)處理。

目前未設定 Windows 程式碼簽章；CI 驗證與 SHA-256 不代表已簽章。若日後加入簽章，應在簽署完成後才重新計算 checksum。

## CI 可驗證的範圍

雲端 runner 不在院內網路，因此只使用合成資料，測試核心行為、資料庫、API、Google 差異邏輯與凍結後執行。真實回應格式、權限與刀房內嵌限制仍需院內驗收。不要將病歷資料或可攜式資料庫上傳為 CI Artifact。

官方參考：[Workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)、[Release CLI](https://cli.github.com/manual/gh_release_create)。
