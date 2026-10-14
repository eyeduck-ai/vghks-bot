# CI 與版本發布

本機 `./build.ps1` 可建置主程式與獨立日曆監控 EXE，完成離線自測後帶入內網驗收。GitHub Actions 驗證已提交來源；EXE、本機資料目錄與金鑰不提交進 Git。

## 6.10.1 起的流程

| 觸發 | 完整測試 | 雙 EXE 打包 | Release |
| --- | --- | --- | --- |
| Pull Request 到 main | 四組 Windows 測試並行、來源自測、固定 GAS 原始碼契約 | 不打包 | 不發布 |
| Push 到 main | 相同必要檢查 | 測試成功後打包、自測、核對與保存 Artifact | 不發布 |
| Actions 手動執行 windows.yml | 相同必要檢查 | 不打包 | 不發布 |
| Push v* 標籤 | 核對同一 commit 已成功的 main run | 下載該 run 已驗證的產物 | 核對後發布 |

[windows.yml](../.github/workflows/windows.yml) 將完整測試拆成四組。依 TestCase 的穩定雜湊分組，同一類別的 setup／teardown 留在同一工作；沒有以修改路徑或本機結果略過測試。各組輸出完整探索名單與已完成名單，`tools/ci_artifacts.py collect-tests` 核對四組一致、每項恰好執行一次且全部成功；任一組缺漏、失敗或探索結果不同，建置便停止。兩個原始碼自測只執行一次，與來源／Ruff／JS／固定 GAS 契約放在同一工作。

GAS 版本固定於 `tests/fixtures/gas_contract.json`，checkout 後執行實際格式、解析及寫入匯出器，包含新月份、同日不同院區與跨月搬移；不自動追蹤 GAS 最新分支。

main 建置下載同一 workflow run 的完整測試彙總與來源自測報告，再完成雙 EXE 自測、封裝檢查、ZIP 與 SHA-256。`BUILD_INFO.json` 將產物綁定 repository、main commit、Git tree、GAS revision、run ID／attempt、版本及檔案雜湊。

[release.yml](../.github/workflows/release.yml) 只接受同倉庫、同 commit、成功的 main push 工作流程。下載後核對上述來源資訊、三個檔案的 SHA-256、ZIP 內的 EXE 與六份驗證報告，再建立 Release；不採用 PR 或其他 commit 的產物，也不執行下載內容。若標籤推送較早，最多等待該 main 建置 20 分鐘。

最新 PR 更新會取消該 PR 的過時驗證；main 及標籤工作保留，讓每個已提交版本都有可追溯產物。四組測試改善等待時間，沒有減少完整回歸覆蓋；實際時間仍受 GitHub runner 排隊與效能影響。

## 權限、報告與本機驗證

驗證／建置只有 `contents: read`；發布取得 `contents: write` 與下載所需的 `actions: read`。使用內建 GITHUB_TOKEN，不需要額外 PAT、院內帳密、Google 金鑰或簽章憑證。依賴鎖定版本，官方 Actions 固定 commit，Dependabot 每月提出更新。

Artifact 保留 14 天：`shard-<組別>-<SHA>` 含 tests.log、JUnit tests.xml 與耗時；`tests-<SHA>` 包含四組報告、完整覆蓋彙總及來源自測；`windows-<SHA>` 是可發布產物與 BUILD_INFO。Release 附件是長期正式下載入口。產物過期或缺失時，重新執行該 commit 原本的 main push run，再重跑標籤發布；不要拿其他 commit 的產物替代。

本機完整驗證維持：

```powershell
.venv/Scripts/python.exe tools/run_checks.py --build --gas-repo .build/gas-contract
```

分階段可用 `--checks-only`、`--tests-only`，都通過後才執行 `--build-only`。CI 另使用 `--source-only` 跑兩個來源自測，及 `tools/test_suite.py --shard-index 0 --shard-count 4 --report-dir .build/ci/shard-0` 跑單一分組；單組結果不能代表完整驗證。

## 發布新版本

1. 同步更新 pyproject.toml 與 `vghks_bot/__init__.py` 的版本，並在 CHANGELOG.md 最上方加入該版本變更。
2. 完成本機完整驗證與必要院內驗收，提交 PR、通過完整驗證後合併至 main。
3. 確認合併後同一 commit 的 Windows verification and build 成功。
4. 在該 commit 建立並推送版本標籤：

```powershell
$releaseVersion = (.venv/Scripts/python.exe -c "from vghks_bot import __version__; print(__version__)").Trim()
git tag -a "v$releaseVersion" -m "VGHKS-bot v$releaseVersion"
git push origin "v$releaseVersion"
```

只推送 main 會產生 Artifact；推送標籤才發布 Release。標籤須等於程式版本並指向已驗證 main commit；不符或檢查失敗就停止。既有 Release 不會覆蓋，修正版本使用新標籤。

## 下載與驗證

- 主程式下載 VGHKS-bot.exe；需要日曆同步時另外使用 VGHKS-calendar-monitor.exe。
- ZIP 包含雙 EXE、操作文件、第三方聲明及原始碼／凍結程式自測與封裝報告。
- `Get-FileHash .\VGHKS-bot.exe -Algorithm SHA256` 比對 SHA256SUMS.txt；監控 EXE 與 ZIP 也各有校驗值。
- 更新前結束兩個程式，再更換 EXE，保留 VGHKS-bot-data 與 VGHKS-calendar-data。

目前未設定 Windows 程式碼簽章；CI 與 SHA-256 不代表已簽章。雲端 runner 不在院內網路，只使用合成資料；真實院方回應、權限與內嵌刀房仍需院內驗收。

官方參考：[Matrix jobs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations)、[Workflow artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts)、[Release CLI](https://cli.github.com/manual/gh_release_create)。
