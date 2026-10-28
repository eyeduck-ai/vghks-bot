# 開發與建置

## 環境

- Windows x64、Python 3.11、Git、Node.js 24。
- SDK 與 Python 套件固定於 `requirements-build.lock.txt`。SDK commit 也列在 `pyproject.toml`，兩者必須一致。
- 不需要院內帳密、Google 金鑰或真實資料庫即可測試及打包。

```powershell
git clone https://github.com/eyeduck-ai/vghks-bot.git
cd vghks-bot
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.lock.txt
```

### 資料夾與套件名稱

外層專案資料夾可命名為 `vghks-bot`；主程式套件為 `vghks_bot/`，`run.py` 與命令列入口都載入 `vghks_bot.app:main`。套件包含後端、資料儲存及 `static/` 介面，不能當作暫存移除。

既有工作區改名時，先關閉執行中的程式及使用該目錄的終端，再於檔案總管改名。在 Codex 既有專案的「Edit project」調整資料夾，將新路徑設為主要資料夾；保留原專案及對話，不需要刪除後重建。舊對話記錄的工作目錄可能仍是舊路徑，執行命令時應確認目前位於新目錄。

Python 虛擬環境的啟動腳本與部分命令列程式含建立時的絕對路徑，單純搬移 `.venv` 不代表環境已重建。在新資料夾執行建置，腳本會檢查啟動路徑；缺少或不符目前路徑時自動重建並重新安裝固定依賴：

```powershell
./build.ps1

# 環境損壞或要明確重新安裝時。
./build.ps1 -RebuildEnvironment
```

重建只處理目前專案的 `.venv`，遇到連結或 junction 會停止。建置工具依自身位置取得專案根目錄，不需修改 Git remote；若有設定外部資料庫，仍須保留其資料夾及有效路徑。

可核對 `.venv/pyvenv.cfg` 的建立命令與 `Scripts/activate.bat` 的 `VIRTUAL_ENV` 是否為新路徑，再確認 Python 套件及命令列啟動器：

```powershell
.venv/Scripts/python.exe -c "import sys, vghks_bot; print(sys.executable); print(sys.prefix); print(vghks_bot.__file__)"
.venv/Scripts/pip.exe --version
.venv/Scripts/ruff.exe --version
```

## 執行與測試

```powershell
# 真實工作區；明確指定資料位置，避免與其他執行中的程式共用。
.venv/Scripts/python.exe run.py --data-dir ./VGHKS-bot-data

# 合成瀏覽器測試，院內刀房也替換成 localhost。
.venv/Scripts/python.exe tools/browser_bot_fixture.py --followup --library --local-board

# 全部檢查，不打包。
.venv/Scripts/python.exe tools/run_checks.py

# 個別測試。
.venv/Scripts/python.exe -m unittest discover -s tests -p test_surgery_schedule.py -v
```

瀏覽器 fixture 會印出帶本機登入 token 的 URL；開啟後以「結束程式」離開。進階工具目前仍使用嵌入的 `/tools` 比較頁，因此 `index.html`、`app.js`、`analysis.js` 等檔案仍是正式程式的一部分。可用 `tools/browser_analysis_fixture.py` 單獨驗證比較頁。

掛號紀錄可使用 `tools/browser_bot_fixture.py --registrations --local-board`，提供完整掛號欄位、明確未到診、未知圖示、取消及日期不明的合成案例。`tests/test_registration_history.py` 另經真實 SDK 與合成 HTTP 驗證快取升級及身分拒收；不需讀取院內 ZIP 或 HAR。

刀表合成資料由 `tests/fixtures/` 與相關整合測試維護。`tools/verify_sheet_contract.py` 離線核對主程式採用的共用核心與 GAS 匯出的 82 組格式契約；指定 `--gas-repo <GAS checkout>` 時，另從該 checkout 執行 GAS 匯出器，並實際執行主程式封存請求後交由 GAS 續封，驗證雙向相容。`tests/fixtures/gas_contract.json` 固定 GAS commit，`gas_archives.json` 保存原生封存匯出。需要線上只讀檢查時，使用 `tools/check_sheet_contract.py`，依 `--help` 指定外部服務帳戶檔、刀表、日曆與報告路徑；憑證及線上報告不加入 Git。

來源邊界檢查同時涵蓋 Git 已追蹤檔案與尚未提交、未被忽略的新檔案，不需要先 stage 才能檢查。提交前仍以 `git add <明確路徑>` 或明確的來源目錄選取變更；不要加入真實資料庫或病歷 fixture。失敗重現請建立合成資料。

## 主程式 EXE

```powershell
./build.ps1

# 已安裝依賴時，直接使用與 CI 相同的流程。
.venv/Scripts/python.exe tools/run_checks.py --build
```

流程包括：來源範圍與版本核對 → pip／Ruff／JS 語法 → 共用刀表契約 → Python 測試 → 主程式原始碼自測 → PyInstaller → 主程式 EXE 自測 → 靜態檔及預設值核對 → ZIP 與 SHA-256。

- `dist/VGHKS-bot.exe`：Windows x64 主程式。
- `dist/VGHKS-bot-v<版本>-windows-x64.zip`：主程式 EXE、文件、第三方聲明及驗證報告。
- `dist/SHA256SUMS.txt`：主程式 EXE 與 ZIP 校驗值。
- `.build/ci/`：測試、建置日誌及主程式原始碼／EXE 自測與封裝核對 JSON。

只打包、不跑完整檢查的開發命令是 `python tools/build_exe.py`；它不是 Release 流程。CI 一律使用公開預設值，帳密留白。

6.10.1 的 CI 將完整測試拆成四組並行，彙總核對覆蓋名單；PR 不重複打包，main 才建置主程式 EXE，標籤沿用同一成功 main commit 的已驗證產物。本機完整流程不變，來源自測可單獨用 `--source-only` 執行。[CI 與發布細節](RELEASING.md)

Windows CI 的合成測試將 `TEMP`、`TMP`、`TMPDIR` 指向該 job 的 `runner.temp` 工作磁碟；各測試仍建立獨立暫存目錄，保留真實 SQLite／WAL／FULL 同步與原子檔案寫入。測試失敗或例外會立即輸出名稱與 traceback，完整日誌、JUnit 與測試名單仍照常保存；工作時間上限及完整驗證門檻不變。

## 清理與本機檔案

```powershell
./tools/clean.ps1

# 保留驗證日誌，並將發布 ZIP 精簡為最新兩版。
./tools/clean.ps1 -KeepValidationReports -OldReleases

# 另清除合成瀏覽器測試的快照、日誌及截圖。
./tools/clean.ps1 -KeepValidationReports -OldReleases -BrowserArtifacts

# 先列出預計清理項目。
./tools/clean.ps1 -KeepValidationReports -OldReleases -BrowserArtifacts -WhatIf
```

預設清理 `.build/`、`.ruff_cache/`、`build/` 及來源下的 Python 快取；先檢查範圍，遇到連結／junction 會停止。保留 `dist/`、`.venv/`、`.local/`、`VGHKS-bot-data/` 與 `VGHKS-calendar-data/`。`-KeepValidationReports` 保留 `.build/ci/`；`-OldReleases` 只清除正式命名的舊 ZIP，保留最新兩版，EXE、文件、校驗值與 HAR 不受影響。

`-BrowserArtifacts` 只清除 `.playwright-cli/` 與 `output/playwright/`，不清除 `output/` 的其他檔案。使用於合成 UI 驗證後的暫存清理；需留存的個人診斷或截圖放在 `.local/`。清理腳本在刪除第一項前先核對所有目標的絕對路徑與連結，`-WhatIf` 仍執行檢查但不刪除。

`.local/` 供個人備份及診斷封存，不列入 Git。HAR 擷取檔及 SDK 診斷 JSONL 也會被忽略，來源發布檢查會拒絕它們與 ZIP 建置包。版本控制只保存可分享的原始碼、合成測試、公開預設值與文件；`dist/` 的舊版本包經確認後可移除，`VGHKS-bot-data/` 不屬於建置快取。

搬回的院內資料快照及不再使用的個人診斷，應確認用途及需要保留的內容後再清除；清理腳本不處理這些資料。自動測試及 EXE 離線自測使用合成 SDK 與獨立暫存資料庫，不讀取專案根目錄的 `VGHKS-bot-data/`；正常啟動若未指定 `--data-dir`，仍會在原始碼或 EXE 同層使用或建立此資料目錄。

## 大型病歷檢閱效能驗收

`python tools/benchmark_review.py --sizes 40 1000 5000 20000` 在一次性合成資料庫量測分頁、末頁定位、單人全部 SOAP、備註及 compact workbench。工具禁止 socket 連線，報告預設寫入忽略的 `.build/review-paging-benchmark.json`；冷次全文搜尋與三次暖態中位數分開記錄，時間不含 HTTP 傳輸與真實瀏覽器繪製。

連續清單的每批讀取只回 40 位摘要、不解碼 SOAP，SQL 數不隨任務總人數逐人增加；單人詳情只讀該病人快照。量測另涵蓋前進、返回與遠處定位的視窗，以及 ready 預讀與 partial 略過；這些資料庫量測不代表瀏覽器 heap／DOM 已驗證。`tools/browser_bot_fixture.py --review-patients 1000 --local-board` 可啟動兩個合成帳號的完整舊格式檢閱，不呼叫院內服務，供瀏覽器驗收雙向捲動、全域搜尋、跨區勾選、來源收合、備註與閱讀位置。必須另檢查可見列、摘要區塊、detail cache 與預讀中的請求量均有上限，切換 scope 不採用舊回應。關閉測試伺服器後會清除暫存資料庫。

基本資料、歷年數值與醫囑索引維持按需取得；讀取檢閱、捲動及本機預讀不得為整份名單自動發出這些院內請求。單份報告須以合成 barrier 驗證第一份附件保存後、其他附件尚在讀取時已可檢閱，且後續發布不重設主檢視與比較勾選。同報告的指定附件 GET 免除額外間隔，仍逐一下載；其他查詢節奏、身分核對、取消後保存當張、快取重用與限流退避均保留。全程不讀取使用者資料庫或院內資料。

跨任務共用取得的回歸須以 barrier 製造同時 cache miss，核對實體 SDK／HTTP、前景優先權、各訂閱者取消、force 分流、登入／清除 epoch 與病人／完整就診 selector。快取與合併結果不得改寫 PRQ 病人 context，不得增加 SDK 外層重試；必要身分核對與 SDK 內部退避仍照常發生。

病人來源輸入另以 `tools/browser_bot_fixture.py --review-patients 1000 --lookup-failures --lookup-delay 5 --local-board` 驗收慢查核、失敗與批次貼上。等待時須看得見原號碼，所有開始入口均不得略過 pending／draft／失敗；來源或帳號切換後舊結果不得回填。標題列的滑鼠及鍵盤操作、設定按鈕不冒泡、344／304px 清單與 84px 虛擬列高須於桌面及窄畫面實測。測試只用合成 SDK 與一次性資料庫。

## 更新 SDK

核對上游版本與變更後，同時修改 `pyproject.toml` 和 `requirements-build.lock.txt` 的完整 commit。`build.ps1` 會核對實際安裝來源；SDK 版本相同但 commit 不符時，只重新安裝 SDK。來源檢查與直接打包也會拒絕不符的 commit，EXE 授權聲明及封裝報告保留完整 commit。

手動更新既有環境時，明確重新安裝 SDK，避免 pip 只按相同版本保留舊程式：

```powershell
$vghksSdkRequirement = Get-Content requirements-build.lock.txt | Where-Object { $_.StartsWith('vghks-sdk @ ') }
.venv/Scripts/python.exe -m pip install --no-deps --force-reinstall $vghksSdkRequirement
```

執行完整檢查、主程式 EXE 自測與封裝核對，並在院內確認真實回應。不要只改其中一處，也不要在 CI 自動追蹤 SDK 的 `main`。PRQ 相容處理使用 `core.operations.operation_spec`，不從已拆分的 Adapter 匯入私有操作常數；光照治療、病人身分及不可重送的恢復規則均需回歸。
