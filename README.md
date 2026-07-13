# Local Windows Python Watcher

此 watcher 不解析 CSV，只負責監控網路芳鄰資料夾並將符合規則的 `AssayProcess_YYYYMMDDHHMMSS.csv` 上傳到 EC2 backend。CSV 解析、欄位補齊、natural key 去重都由 EC2 backend 負責。

## 功能

- 遞迴掃描：

```text
\\fls341\MBBU_FAB\MB_QA\Disc 首件檢查\Tutti\1. 生化盤
```

- 只接受 timestamp 檔名：

```text
AssayProcess_20260505121541.csv
```

- 忽略非 timestamp 檔名，例如：

```text
AssayProcess_merge.csv
AssayProcess_test.csv
```

- 第一次啟動會做 initial scan，但會依照 `upload_manifest.json` 跳過未變更檔案。
- 常駐後會 watch 資料夾異動，只上傳新增/修改的 matching CSV，不重新全量上傳。
- 如果檔案位在「資料夾名稱以 `建線` 結尾」的路徑下，例如 `A建線`，上傳時會加上 `baseline=true`。
- 其他資料夾上傳時會加上 `baseline=false`。

## 安裝

在 Windows PowerShell：

```powershell
cd <local-watcher 資料夾>
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r .\requirements.txt
Copy-Item .\config.example.json .\config.json
notepad .\config.json
```

確認 `config.json`：

```json
{
  "watch_root": "\\\\fls341\\MBBU_FAB\\MB_QA\\Disc 首件檢查\\Tutti\\1. 生化盤",
  "upload_url": "https://52-192-28-39.sslip.io/api/assayprocess/upload-assay-process-csv",
  "manifest_path": "upload_manifest.json",
  "stable_check_seconds": 2,
  "debounce_seconds": 5,
  "request_timeout_seconds": 120,
  "rescan_interval_seconds": 300
}
```

## 執行

啟動 watcher：

```powershell
.\.venv\Scripts\Activate.ps1
python .\watch_assayprocess.py
```

如果只想先跑一次 initial scan，上傳 queue 清空後離開：

```powershell
python .\watch_assayprocess.py --once
```

如果已經做過全量上傳，只想直接常駐監控新異動：

```powershell
python .\watch_assayprocess.py --no-initial-scan
```

如果只想補某一個資料夾或單一檔案：

```powershell
python .\watch_assayprocess.py --path "\\fls341\MBBU_FAB\MB_QA\Disc 首件檢查\Tutti\1. 生化盤\某資料夾建線"
```

## 輸出說明

成功上傳會顯示：

```text
uploaded: AssayProcess_20260505121541.csv baseline=True backend imported: rows_read=32 inserted=32 ignored=0
```

重複資料由 backend 略過時：

```text
uploaded: AssayProcess_20260505121541.csv baseline=True backend imported: rows_read=32 inserted=0 ignored=32
```

HTTP/網路失敗會顯示：

```text
upload failed: ... timeout
```

檔名不符合 `AssayProcess_YYYYMMDDHHMMSS.csv` 的檔案不會上傳。

## 重新補 baseline

如果之前已經全量上傳過，現在要補上 baseline 欄位，請重新跑一次 initial scan：

```powershell
python .\watch_assayprocess.py --once
```

原因：

- 本機 `upload_manifest.json` 舊紀錄沒有 `Baseline` 欄位。
- Python watcher 會重新上傳一次，backend 會用 natural key 去重，不會新增重複資料。
- 對已存在的資料，backend 會更新 `baseline=true/false`。
- 補完後 manifest 會記錄 Baseline，下一次就不會重複上傳未變更檔案。

## Watch 與手動觸發

程式常駐時使用 Windows 原生檔案異動事件監控資料夾。若需要手動要求它掃描某個路徑，可在 watcher 同資料夾建立 `trigger.txt`，內容放單一檔案或資料夾路徑；內容留空則做 full rescan。

## Legacy PowerShell

舊的 `watch_assayprocess.ps1` 保留作為備用；新的建議流程是使用 `watch_assayprocess.py`。
