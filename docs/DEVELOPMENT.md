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
```

清理 `.build/`、`.ruff_cache/`、`build/` 及來源下的 Python 快取；先檢查範圍，遇到連結／junction 會停止。保留 `dist/`、`.venv/`、`.local/` 與 `VGHKS-bot-data/`。

`.local/` 供個人備份及診斷封存，不列入 Git。HAR 擷取檔及 SDK 診斷 JSONL 也會被忽略，來源發布檢查會拒絕它們與 ZIP 建置包。版本控制只保存可分享的原始碼、合成測試、公開預設值與文件；`dist/` 的舊版本包經確認後可移除，`VGHKS-bot-data/` 不屬於建置快取。

## 更新 SDK

核對上游版本與變更後，同時修改 `pyproject.toml` 和 `requirements-build.lock.txt` 的完整 commit。重新安裝依賴、執行完整流程，並在院內確認真實回應。不要只改其中一處，也不要在 CI 自動追蹤 SDK 的 `main`。
