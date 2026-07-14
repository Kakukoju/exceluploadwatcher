"""
Excel Auto-Uploader (Local Windows/Mac)
========================================
部署在有 SMB 存取權限的 local 端電腦，監控 Excel 資料夾並自動上傳到 QC Web Server。

功能:
  1. Watchdog — 檔案新增/修改時即時上傳 (debounce 5 秒)
  2. 定時全量掃描 — 每天指定時間全部重新上傳 (確保不遺漏)

用法:
  pip install watchdog requests schedule
  python excel_uploader.py

設定:
  修改下方 config 或建立 config.json 覆寫
"""

import os
import sys
import json
import time
import glob
import logging
import threading
from pathlib import Path
from datetime import datetime

import requests
import schedule
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# LINE 通知
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from line_notify import send_line_message
except ImportError:
    def send_line_message(msg, source="default"):
        return False

# ─── AI Gateway + Auto-Remediation ────────────────────────────────────────────

import re
import subprocess

AI_GATEWAY_URL = "https://52-192-28-39.sslip.io/ai-gateway/v1/chat/completions"
AI_GATEWAY_API_KEY = "beadsops-ai-2026"
AI_GATEWAY_MODEL = "claude-haiku"

# 白名單動作：這些動作 AI 可以直接執行，不需人工審批
WHITELISTED_ACTIONS = {"retry_upload", "kill_process", "wait"}

# 服務名稱（用於 systemctl --user）
SERVICE_NAME = "excel-uploader.service"

# 防循環：記錄最後一次 AI remediation 的時間，避免 restart 後立即再次觸發
_LAST_AI_REMEDIATION_FILE = Path(__file__).parent / ".ai_remediation_ts"
_AI_COOLDOWN_SECONDS = 600  # 10 分鐘內不重複觸發 AI 修復


def _is_in_cooldown() -> bool:
    """檢查是否在 AI 修復冷卻期內（防止 restart_service 造成無限循環）。"""
    try:
        if _LAST_AI_REMEDIATION_FILE.exists():
            last_ts = float(_LAST_AI_REMEDIATION_FILE.read_text().strip())
            return (time.time() - last_ts) < _AI_COOLDOWN_SECONDS
    except Exception:
        pass
    return False


def _mark_remediation():
    """記錄 AI 修復觸發時間。"""
    try:
        _LAST_AI_REMEDIATION_FILE.write_text(str(time.time()))
    except Exception:
        pass


def ask_ai_gateway(error_info: str, filepaths: list[str]) -> dict:
    """
    送出上傳錯誤資訊給 AI Gateway 分析，回傳結構化修復動作。
    回傳格式:
    {
        "analysis": "錯誤分析",
        "actions": [
            {"type": "retry_upload", "wait_seconds": 10, "skip_files": []},
            {"type": "kill_process", "pattern": "excel_uploader"},
            {"type": "restart_service"},
            {"type": "modify_code", "file": "excel_uploader.py", "description": "修改說明", "patch": "..."},
            {"type": "custom_command", "command": "some shell command", "reason": "原因"}
        ]
    }
    """
    file_list = "\n".join(f"  - {os.path.basename(fp)}" for fp in filepaths[:20])
    prompt = (
        "你是一個檔案上傳系統的錯誤分析與自動修復助手。以下是上傳失敗的資訊。\n"
        "請分析錯誤原因，並給出修復動作。\n\n"
        f"## 系統資訊\n"
        f"- 服務: excel-uploader.service (systemd user service)\n"
        f"- 程式: /home/harryhrguo/local-watcher/local-uploader/excel_uploader.py\n"
        f"- API: {CONFIG['api_url']}\n\n"
        f"## 錯誤資訊\n{error_info}\n\n"
        f"## 相關檔案\n{file_list}\n\n"
        "## 回覆格式（純 JSON，不要 markdown code block）\n"
        "{\n"
        '  "analysis": "簡短分析錯誤原因",\n'
        '  "actions": [\n'
        "    // 可用的 action types:\n"
        '    // {"type": "wait", "seconds": 10}  — 等待\n'
        '    // {"type": "retry_upload", "skip_files": ["檔名"]}  — 重試上傳\n'
        '    // {"type": "kill_process", "pattern": "程序名稱"}  — 殺掉卡住的程序\n'
        '    // {"type": "restart_service"}  — 重啟 excel-uploader service\n'
        '    // {"type": "modify_code", "file": "檔名", "description": "修改說明", "patch": "diff 內容"}  — 修改程式碼\n'
        '    // {"type": "custom_command", "command": "shell 指令", "reason": "原因"}  — 自訂指令\n'
        "  ]\n"
        "}\n\n"
        "## 判斷原則\n"
        "- 網路逾時/連線失敗/5xx → wait + retry_upload\n"
        "- 程序卡死/timeout 過久 → kill_process + restart_service\n"
        "- 程式 bug 導致錯誤 → modify_code（提供 patch）\n"
        "- 其他需要特殊處理 → custom_command\n"
        "- 可以組合多個 actions，按順序執行\n"
    )

    try:
        resp = requests.post(
            AI_GATEWAY_URL,
            headers={
                "X-API-Key": AI_GATEWAY_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": AI_GATEWAY_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

        # 嘗試解析 JSON
        json_match = re.search(r'\{.*"actions"\s*:\s*\[.*\].*\}', content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(content)
    except Exception as e:
        # AI Gateway 不可用時，預設簡單重試
        return {
            "analysis": f"AI Gateway 無法回應 ({e})，預設等待後重試",
            "actions": [
                {"type": "wait", "seconds": 10},
                {"type": "retry_upload", "skip_files": []},
            ],
        }


# ─── Action Executor ──────────────────────────────────────────────────────────


def execute_actions(actions: list[dict], filepaths: list[str], context: str) -> dict:
    """
    執行 AI 建議的動作列表。
    白名單動作直接執行，非白名單動作送 Teams 通知等待人工審批。
    restart_service 特殊處理：只在重試成功或沒有重試時執行，避免循環。
    回傳 {"executed": [...], "pending_approval": [...], "retry_result": ..., "should_restart": bool}
    """
    executed = []
    pending_approval = []
    retry_result = None
    should_restart = False

    for action in actions:
        action_type = action.get("type", "unknown")

        # restart_service 延後處理（避免在重試失敗時 restart 造成循環）
        if action_type == "restart_service":
            should_restart = True
            continue

        if action_type not in WHITELISTED_ACTIONS:
            # 非白名單 → 通知 Teams 等待審批
            pending_approval.append(action)
            log.info(f"[Action] 非白名單動作，送審: {action_type}")
            continue

        # === 白名單動作，直接執行 ===
        try:
            if action_type == "wait":
                seconds = action.get("seconds", 10)
                log.info(f"[Action] 等待 {seconds} 秒...")
                time.sleep(seconds)
                executed.append({"type": "wait", "seconds": seconds, "status": "done"})

            elif action_type == "retry_upload":
                skip_files = set(action.get("skip_files", []))
                retry_files = [
                    fp for fp in filepaths
                    if os.path.basename(fp) not in skip_files
                ]
                if retry_files:
                    log.info(f"[Action] 重試上傳 {len(retry_files)} 個檔案...")
                    retry_result = upload_files(retry_files)
                    executed.append({
                        "type": "retry_upload",
                        "files_count": len(retry_files),
                        "status": "success" if (retry_result and not retry_result.get("errors")) else "failed",
                    })
                else:
                    executed.append({"type": "retry_upload", "status": "skipped", "reason": "no files to retry"})

            elif action_type == "kill_process":
                pattern = action.get("pattern", "excel_uploader")
                log.info(f"[Action] Kill process matching: {pattern}")
                result = subprocess.run(
                    ["pkill", "-f", pattern],
                    capture_output=True, text=True, timeout=10,
                )
                executed.append({
                    "type": "kill_process",
                    "pattern": pattern,
                    "status": "done" if result.returncode == 0 else "no_match",
                })

        except Exception as e:
            log.error(f"[Action] 執行 {action_type} 失敗: {e}")
            executed.append({"type": action_type, "status": f"error: {e}"})

    # === 非白名單動作送 Teams 通知 ===
    if pending_approval:
        _notify_pending_approval(pending_approval, context)

    return {
        "executed": executed,
        "pending_approval": pending_approval,
        "retry_result": retry_result,
        "should_restart": should_restart,
    }


def _notify_pending_approval(actions: list[dict], context: str):
    """將需要人工審批的動作送到 Teams 通知。"""
    action_descriptions = []
    for action in actions:
        action_type = action.get("type", "unknown")
        if action_type == "modify_code":
            desc = (
                f"📝 修改程式碼: {action.get('file', '?')}\n"
                f"   說明: {action.get('description', 'N/A')}\n"
                f"   Patch: {action.get('patch', 'N/A')[:200]}"
            )
        elif action_type == "custom_command":
            desc = (
                f"⚙️ 執行指令: {action.get('command', '?')}\n"
                f"   原因: {action.get('reason', 'N/A')}"
            )
        else:
            desc = f"❓ 未知動作: {json.dumps(action, ensure_ascii=False)[:200]}"
        action_descriptions.append(desc)

    message = (
        f"🔐 [{context}] AI 建議以下動作需要您的核准：\n\n"
        + "\n\n".join(action_descriptions)
        + "\n\n請確認後手動執行，或回覆核准。"
    )

    send_line_message(message, source="excel_uploader_approval")


# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    # 遠端 API
    "api_url": "https://52-192-28-39.sslip.io/qc-web-api/api/excel-import/upload-batch",

    # 監控的資料夾 (可多個)
    "watch_dirs": [
        r"\\fls341\MBBU_FAB\MB_QA\Dora\6.QBi Beads IPQC",
        r"\\fls341\MBBU_FAB\MB_QA\Dora\2.Disk A",
    ],

    # 定時全量上傳 (每天幾點執行, 24h 格式)
    "scheduled_time": "00:30",

    # Watchdog debounce 秒數 (同一檔案短時間多次修改只上傳一次)
    "debounce_seconds": 5,

    # 上傳 chunk size
    "chunk_size": 10,

    # 只處理當年度 (True) 或所有年度 (False)
    "current_year_only": True,

    # log 檔路徑
    "log_file": "excel_uploader.log",
}


def load_config():
    config = DEFAULT_CONFIG.copy()
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config.update(json.load(f))
    return config


CONFIG = load_config()

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("excel_uploader")

# ─── Upload Logic ─────────────────────────────────────────────────────────────


def is_valid_xlsx(filepath: str) -> bool:
    """Check if file matches the expected YYYY-*.xlsx pattern."""
    name = os.path.basename(filepath)
    if name.startswith("~$"):
        return False
    if not name.endswith(".xlsx"):
        return False
    if CONFIG["current_year_only"]:
        year = str(datetime.now().year)
        return name.startswith(f"{year}-")
    # Accept any year pattern: 20XX-*.xlsx
    return bool(name[:4].isdigit() and name[4] == "-")


def upload_files(filepaths: list[str]) -> dict | None:
    """Upload a list of xlsx files to the API endpoint."""
    if not filepaths:
        return None

    url = CONFIG["api_url"]
    chunk_size = CONFIG["chunk_size"]
    total_result = {"total_files": 0, "imported_files": 0, "total_sheets": 0, "errors": []}

    for i in range(0, len(filepaths), chunk_size):
        chunk = filepaths[i : i + chunk_size]
        files_payload = []
        for fp in chunk:
            try:
                files_payload.append(("files", (os.path.basename(fp), open(fp, "rb"))))
            except OSError as e:
                log.warning(f"Cannot open {fp}: {e}")
                continue

        if not files_payload:
            continue

        try:
            resp = requests.post(url, files=files_payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()
            total_result["total_files"] += data.get("total_files", 0)
            total_result["imported_files"] += data.get("imported_files", 0)
            total_result["total_sheets"] += data.get("total_sheets", 0)
            log.info(
                f"Chunk {i // chunk_size + 1}: uploaded {len(chunk)} files, "
                f"imported={data.get('imported_files', 0)}, sheets={data.get('total_sheets', 0)}"
            )
        except Exception as e:
            log.error(f"Upload failed for chunk {i // chunk_size + 1}: {e}")
            total_result["errors"].append(str(e))
        finally:
            for _, (_, fh) in files_payload:
                fh.close()

    return total_result


def collect_all_xlsx() -> list[str]:
    """Scan all watch_dirs and collect valid xlsx files."""
    files = []
    year = str(datetime.now().year)

    for watch_dir in CONFIG["watch_dirs"]:
        if not os.path.isdir(watch_dir):
            # Try appending year subdirectory
            for subdir in [year, f"{year}年度IPQC化學特性批次紀錄"]:
                candidate = os.path.join(watch_dir, subdir)
                if os.path.isdir(candidate):
                    watch_dir = candidate
                    break

        if not os.path.isdir(watch_dir):
            log.warning(f"Directory not accessible: {watch_dir}")
            continue

        for f in glob.glob(os.path.join(watch_dir, "*.xlsx")):
            if is_valid_xlsx(f):
                files.append(f)

        # Also check year subdirectories
        year_subdir = os.path.join(watch_dir, year)
        if os.path.isdir(year_subdir):
            for f in glob.glob(os.path.join(year_subdir, "*.xlsx")):
                if is_valid_xlsx(f):
                    files.append(f)

    return list(set(files))


# ─── AI-Assisted Retry Logic ──────────────────────────────────────────────────


def upload_with_ai_retry(filepaths: list[str], context: str = "排程上傳") -> dict:
    """
    上傳檔案，失敗時透過 AI Gateway 分析錯誤並執行修復動作。
    白名單動作自動執行，非白名單動作通知 Teams 等待審批。
    最終結果透過 LINE + Teams 通知。
    """
    result = upload_files(filepaths)

    if not result or not result.get("errors"):
        # 全部成功，不需通知
        return result

    # === 防循環檢查 ===
    if _is_in_cooldown():
        log.info("[AI] 在冷卻期內，跳過 AI 修復，直接通知錯誤")
        send_line_message(
            f"⚠️ [{context}] 上傳有 {len(result['errors'])} 個錯誤（AI 冷卻中，不重複修復）\n"
            f"成功: {result.get('imported_files', 0)}/{result.get('total_files', 0)} 檔\n"
            f"錯誤: {result['errors'][0][:80]}",
            source="excel_uploader",
        )
        return result

    # === 有錯誤，送 AI Gateway 分析 ===
    error_info = "\n".join(result["errors"][:5])
    log.info(f"[AI] 上傳有錯誤，送 AI Gateway 分析...")
    _mark_remediation()  # 標記觸發時間

    ai_response = ask_ai_gateway(error_info, filepaths)
    analysis = ai_response.get("analysis", "N/A")
    actions = ai_response.get("actions", [])
    log.info(f"[AI] 分析: {analysis}")
    log.info(f"[AI] 建議動作: {[a.get('type') for a in actions]}")

    if not actions:
        # AI 沒有建議任何動作
        send_line_message(
            f"⚠️ [{context}] 上傳失敗，AI 無修復建議\n"
            f"🤖 分析: {analysis}\n"
            f"錯誤: {result['errors'][0][:80]}\n"
            f"成功: {result.get('imported_files', 0)}/{result.get('total_files', 0)} 檔",
            source="excel_uploader",
        )
        return result

    # === 執行 AI 建議的動作 ===
    exec_result = execute_actions(actions, filepaths, context)
    executed = exec_result["executed"]
    pending = exec_result["pending_approval"]
    retry_result = exec_result["retry_result"]
    should_restart = exec_result["should_restart"]

    # === 根據執行結果通知 ===
    if retry_result and not retry_result.get("errors"):
        # 重試成功
        executed_types = [a["type"] for a in executed]
        send_line_message(
            f"✅ [{context}] AI 自動修復成功\n"
            f"🤖 分析: {analysis}\n"
            f"執行動作: {', '.join(executed_types)}\n"
            f"結果: {retry_result.get('imported_files', 0)} 檔上傳成功",
            source="excel_uploader",
        )
        return retry_result

    elif retry_result and retry_result.get("errors"):
        # 重試仍失敗
        retry_errors = retry_result.get("errors", [])
        msg = (
            f"❌ [{context}] AI 自動修復後仍失敗\n"
            f"🤖 分析: {analysis}\n"
            f"已執行: {', '.join(a['type'] for a in executed)}\n"
            f"重試錯誤: {retry_errors[0][:80] if retry_errors else '未知'}"
        )
        if pending:
            msg += f"\n⏳ 待審批動作: {', '.join(a.get('type') for a in pending)}"
        if should_restart:
            msg += "\n🔄 AI 建議 restart，但重試失敗故不執行（避免循環）"
        send_line_message(msg, source="excel_uploader")
        return retry_result

    else:
        # 沒有 retry 動作（可能只有 kill 或全部需審批）
        msg = (
            f"⚠️ [{context}] 上傳失敗，AI 已執行部分修復\n"
            f"🤖 分析: {analysis}\n"
            f"已執行: {', '.join(a['type'] for a in executed) if executed else '無'}\n"
            f"錯誤: {result['errors'][0][:80]}"
        )
        if pending:
            msg += f"\n⏳ 待審批動作: {', '.join(a.get('type') for a in pending)}"
        send_line_message(msg, source="excel_uploader")

        # 只有在沒有 retry（或 retry 成功）時才允許 restart
        if should_restart:
            log.info(f"[Action] 重啟服務: {SERVICE_NAME}")
            subprocess.Popen(
                ["systemctl", "--user", "restart", SERVICE_NAME],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    return result


# ─── Scheduled Full Upload ────────────────────────────────────────────────────


def scheduled_full_upload():
    """Full scan + upload all matching files."""
    log.info("=== Scheduled full upload started ===")
    files = collect_all_xlsx()
    if not files:
        log.info("No matching xlsx files found.")
        return
    log.info(f"Found {len(files)} files to upload")
    result = upload_with_ai_retry(files, context="排程上傳")
    log.info(f"=== Scheduled upload done: {result} ===")


# ─── Watchdog Handler ─────────────────────────────────────────────────────────


class ExcelChangeHandler(FileSystemEventHandler):
    """Watches for new/modified xlsx files and uploads after debounce."""

    def __init__(self):
        super().__init__()
        self._pending: dict[str, float] = {}  # filepath → timestamp
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _schedule_flush(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(CONFIG["debounce_seconds"], self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        with self._lock:
            now = time.time()
            ready = [
                fp
                for fp, ts in self._pending.items()
                if now - ts >= CONFIG["debounce_seconds"]
            ]
            for fp in ready:
                del self._pending[fp]

        if not ready:
            return

        # Filter: only valid xlsx that still exist
        valid = [fp for fp in ready if os.path.isfile(fp) and is_valid_xlsx(fp)]
        if not valid:
            return

        log.info(f"[Watchdog] Uploading {len(valid)} changed file(s): {[os.path.basename(f) for f in valid]}")
        result = upload_with_ai_retry(valid, context="即時監控")
        log.info(f"[Watchdog] Upload result: {result}")

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle(event.src_path)

    def _handle(self, filepath: str):
        if not is_valid_xlsx(filepath):
            return
        with self._lock:
            self._pending[filepath] = time.time()
        self._schedule_flush()


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    log.info("=" * 60)
    log.info("Excel Auto-Uploader starting")
    log.info(f"  API: {CONFIG['api_url']}")
    log.info(f"  Watch dirs: {CONFIG['watch_dirs']}")
    log.info(f"  Scheduled time: {CONFIG['scheduled_time']}")
    log.info("=" * 60)

    # 1. Setup scheduled job
    schedule.every().day.at(CONFIG["scheduled_time"]).do(scheduled_full_upload)

    # 2. Setup watchdog observers
    handler = ExcelChangeHandler()
    observer = Observer()
    watched = 0
    for watch_dir in CONFIG["watch_dirs"]:
        if os.path.isdir(watch_dir):
            observer.schedule(handler, watch_dir, recursive=True)
            log.info(f"[Watchdog] Watching: {watch_dir}")
            watched += 1
        else:
            log.warning(f"[Watchdog] Cannot watch (not accessible): {watch_dir}")

    if watched > 0:
        observer.start()
        log.info(f"[Watchdog] Started monitoring {watched} directories")
    else:
        log.warning("[Watchdog] No directories accessible — only scheduled mode active")

    # 3. Run initial upload on startup
    log.info("Running initial full upload...")
    scheduled_full_upload()

    # 4. Main loop
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        if watched > 0:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main()
