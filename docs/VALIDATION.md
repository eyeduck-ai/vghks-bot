# 驗證範圍

公開測試只使用合成患者、一次性測試金鑰及暫存資料庫。CI 的完整結果與發布產物可在 [Windows verification and build](https://github.com/eyeduck-ai/vghks-bot/actions/workflows/windows.yml) 追溯。

## 自動驗證

- 真實 SDK 搭配合成 HTTP：登入恢復、病人／就診身分、醫囑與附件、未知及部分解析、快取、續跑與不可重送操作。
- 帳號隔離、資料庫交易／唯讀快照、任務排程、離線來源、備註、報表與前端延遲回應。
- GAS 真實匯出的欄位、純日期、院區刀日、報到時間、永久 ID、跨月搬列及原生封存案例。
- 新月份患者醫院欄留白；SOAP 更新與搬列保留手填值、公式及兩個 ID，日期／院區仍以刀日為準。
- 主程式完整來源與凍結 EXE 自測；內嵌檔案、空白憑證、SDK／共用核心完整 commit、ZIP 邊界及雜湊。
- 四組 Windows 測試彙總須完整、不重複且全部成功；Release 須來自同一 commit 的成功 main 產物。

日曆網路寫入、去重、刪除確認、服務重啟與常駐排程由獨立 [vghks-sheet2cal](https://github.com/eyeduck-ai/vghks-sheet2cal) 驗證。主程式保留共用解析／識別與 Calendar 資料模型的相容性檢查。

## 實際環境

院內帳號權限、院方回應及內嵌刀房需在院內網路驗收。Google 金鑰、實際分享權限、人工保護範圍及 API 配額需以授權環境核對；合成測試不代表已取得這些權限。

寫入功能先在測試副本驗證預覽、套用、回讀、斷線及恢復；封存再驗證備份、複製核對與再次確認移除來源。正式資料不作為合成新增或搬列測試對象。

```powershell
.venv/Scripts/python.exe tools/run_checks.py --build --gas-repo .build/gas-contract
```

詳見[發布流程](RELEASING.md)、[刀表規則](SHEET_SYNC.md)及[效率量測](PERFORMANCE.md)。
