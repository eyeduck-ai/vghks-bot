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

瀏覽器 fixture 會印出帶本機登入 token 的 URL；開啟後以「結束程式」離開。進階工具目前仍使用嵌入的 `/tools` 比較頁，因此 `index.html`、`app.js`、`analysis.js` 等檔案仍是正式程式的一部分。可用 `tools/browser_analysis_fixture.py` 單獨驗證比較頁，`tools/sheet_validation.py` 提供合成刀表資料。

新增檔案先以 `git add <明確路徑>` 納入，來源邊界檢查才會檢查到它。不要加入真實資料庫或病歷 fixture。失敗重現請建立合成資料。

## 單檔 EXE

```powershell
./build.ps1

# 已安裝依賴時，直接使用與 CI 相同的流程。
.venv/Scripts/python.exe tools/run_checks.py --build
```

流程包括：來源範圍與版本核對 → pip／Ruff／JS 語法 → Python 測試 → 原始碼自測 → PyInstaller → EXE 自測 → 靜態檔及預設值核對 → ZIP 與 SHA-256。

- `dist/VGHKS-bot.exe`：Windows x64 單檔。
- `dist/VGHKS-bot-v<版本>-windows-x64.zip`：EXE、文件、第三方聲明及驗證報告。
- `dist/SHA256SUMS.txt`：EXE 與 ZIP 校驗值。
- `.build/ci/`：測試、建置日誌及 JSON 自測結果。

只打包、不跑完整檢查的開發命令是 `python tools/build_exe.py`；它不是 Release 流程。CI 一律使用公開預設值，帳密留白。

## 清理與本機檔案

```powershell
./tools/clean.ps1

# 保留驗證日誌，並將發布 ZIP 精簡為最新兩版。
./tools/clean.ps1 -KeepValidationReports -OldReleases

# 先列出預計清理項目。
./tools/clean.ps1 -KeepValidationReports -OldReleases -WhatIf
```

預設清理 `.build/`、`.ruff_cache/`、`build/` 及來源下的 Python 快取；先檢查範圍，遇到連結／junction 會停止。保留 `dist/`、`.venv/`、`.local/` 與 `VGHKS-bot-data/`。`-KeepValidationReports` 保留 `.build/ci/`；`-OldReleases` 只清除正式命名的舊 ZIP，保留最新兩版，EXE、文件、校驗值與 HAR 不受影響。

`.local/` 供個人備份及診斷封存，不列入 Git。HAR 擷取檔及 SDK 診斷 JSONL 也會被忽略，來源發布檢查會拒絕它們與 ZIP 建置包。版本控制只保存可分享的原始碼、合成測試、公開預設值與文件；`dist/` 的舊版本包經確認後可移除，`VGHKS-bot-data/` 不屬於建置快取。

搬回的院內資料快照及不再使用的個人診斷，應確認用途及需要保留的內容後再清除；清理腳本不處理這些資料。自動測試及 EXE 離線自測使用合成 SDK 與獨立暫存資料庫，不讀取專案根目錄的 `VGHKS-bot-data/`；正常啟動若未指定 `--data-dir`，仍會在原始碼或 EXE 同層使用或建立此資料目錄。

## 更新 SDK

核對上游版本與變更後，同時修改 `pyproject.toml` 和 `requirements-build.lock.txt` 的完整 commit。重新安裝依賴、執行完整流程，並在院內確認真實回應。不要只改其中一處，也不要在 CI 自動追蹤 SDK 的 `main`。
