# Excel Auto-Uploader (Local 端部署)

## 功能
1. **Watchdog 即時監控** — 檔案新增/修改後 5 秒自動上傳
2. **定時全量掃描** — 每天 00:30 全部重新上傳確保不遺漏

## 部署步驟 (Windows)

### 1. 安裝 Python 3.10+
```
https://www.python.org/downloads/
```

### 2. 安裝套件
```cmd
cd local-uploader
pip install -r requirements.txt
```

### 3. 修改設定
編輯 `config.json`:
- `watch_dirs`: 改成你的本機資料夾路徑 (支援 UNC 網路路徑)
- `scheduled_time`: 定時上傳時間
- `api_url`: 如果 server IP 變更需更新

### 4. 測試執行
```cmd
python excel_uploader.py
```

### 5. 設為 Windows 開機自啟 (二擇一)

#### 方法 A: 工作排程器 (Task Scheduler)
1. 開啟「工作排程器」
2. 建立基本工作 → 觸發: 使用者登入時
3. 動作: 啟動程式 → `pythonw.exe excel_uploader.py`
4. 勾選「不論使用者是否登入都要執行」

#### 方法 B: NSSM 服務 (推薦)
```cmd
nssm install ExcelUploader "C:\Python310\python.exe" "C:\path\to\excel_uploader.py"
nssm set ExcelUploader AppDirectory "C:\path\to\local-uploader"
nssm start ExcelUploader
```

## 設定說明

| 欄位 | 說明 | 預設 |
|------|------|------|
| `api_url` | QC Web 後端 upload API | `https://52-192-28-39.sslip.io/qc-web/api/...` |
| `watch_dirs` | 要監控的資料夾 (可多個) | SMB share 路徑 |
| `scheduled_time` | 每日全量掃描時間 | `00:30` |
| `debounce_seconds` | 同一檔案修改後等幾秒才上傳 | `5` |
| `chunk_size` | 一次上傳幾個檔案 | `10` |
| `current_year_only` | 只上傳當年度檔案 | `true` |

## Log
執行後產生 `excel_uploader.log`，可查看上傳歷史與錯誤。
