# -*- coding: utf-8 -*-
"""
Tutti Pur Baseline Watcher
===========================
監控多個資料夾/檔案，各自上傳到不同 S3 prefix，
並透過 EC2 AI Gateway 分析後寫入 EC2 SQLite (tutti_pur_baseline.db)。

Watch Targets:
  1. Tutti_各marker給線Template → S3 Tutti/marker_pre_eqn/ → table tuttiprequn
  2. 生產給線 → S3 Tutti/Pur_baseline/ → table tuttiprueqn
  3. IDEXX 外測值總表*.xlsx → S3 Tutti/real_assign/ → table tuttirealassign

Usage:
    python tutti_pur_baseline_watcher.py
    python tutti_pur_baseline_watcher.py --once
    python tutti_pur_baseline_watcher.py --config tutti_pur_baseline_config.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

# LINE 通知
try:
    from line_notify import send_line_message
except ImportError:
    def send_line_message(msg, source="default"):
        return False


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class WatchTarget:
    name: str
    watch_dir: Path
    s3_prefix: str
    db_table: str
    file_pattern: re.Pattern | None = None  # If set, only files matching this regex
    recursive: bool = True


@dataclass
class Config:
    targets: list[WatchTarget]
    s3_upload_url: str
    ai_gateway_url: str
    s3_bucket: str
    manifest_path: Path
    ec2_host: str
    db_name: str
    stable_check_seconds: int = 3
    debounce_seconds: int = 5
    request_timeout_seconds: int = 120
    rescan_interval_seconds: int = 300


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Missing config: {path}")
    with path.open("r", encoding="utf-8-sig") as fp:
        raw = json.load(fp)

    config_dir = path.parent
    manifest_path = Path(raw.get("manifest_path", "tutti_pur_baseline_manifest.json"))
    if not manifest_path.is_absolute():
        manifest_path = config_dir / manifest_path

    targets = []
    for t in raw.get("watch_targets", []):
        pattern = None
        if t.get("file_pattern"):
            pattern = re.compile(t["file_pattern"], re.IGNORECASE)
        # If file_pattern is set and watch_dir is a parent, don't recurse into subdirs
        recursive = t.get("recursive", True if not t.get("file_pattern") else False)
        targets.append(WatchTarget(
            name=t["name"],
            watch_dir=Path(t["watch_dir"]),
            s3_prefix=t["s3_prefix"],
            db_table=t["db_table"],
            file_pattern=pattern,
            recursive=recursive,
        ))

    return Config(
        targets=targets,
        s3_upload_url=raw["s3_upload_url"],
        ai_gateway_url=raw["ai_gateway_url"],
        s3_bucket=raw.get("s3_bucket", "beads-photos-harry"),
        manifest_path=manifest_path,
        ec2_host=raw.get("ec2_host", "52-192-28-39.sslip.io"),
        db_name=raw.get("db_name", "tutti_pur_baseline.db"),
        stable_check_seconds=int(raw.get("stable_check_seconds", 3)),
        debounce_seconds=int(raw.get("debounce_seconds", 5)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 120)),
        rescan_interval_seconds=int(raw.get("rescan_interval_seconds", 300)),
    )


# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).resolve().parent / "tutti_pur_baseline_watcher.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tutti_pur_baseline")


# ─── Manifest (local dedup tracking) ─────────────────────────────────────────

def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning(f"Manifest read failed, using empty: {exc}")
        return {}


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2, sort_keys=True)
    for attempt in range(5):
        try:
            temp_path.replace(path)
            return
        except PermissionError:
            time.sleep(0.5)
    temp_path.replace(path)


# ─── File Utilities ───────────────────────────────────────────────────────────

def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compute_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_mtime_iso(path: Path) -> str:
    stat = path.stat()
    return datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat()


def is_valid_file(path: Path) -> bool:
    """Basic file validity check (skip temp/hidden/system files)."""
    if not path.is_file():
        return False
    name = path.name
    if name.startswith("~$") or name.startswith("."):
        return False
    if name in ("Thumbs.db", "desktop.ini", "sync.ffs_db"):
        return False
    return True


def file_matches_target(path: Path, target: WatchTarget) -> bool:
    """Check if a file belongs to this target and matches its pattern."""
    try:
        path.relative_to(target.watch_dir)
    except ValueError:
        return False
    if target.file_pattern:
        return bool(target.file_pattern.search(path.name))
    return True


def is_stable_file(path: Path, wait_seconds: int) -> bool:
    """Check file is not being written to."""
    try:
        for attempt in range(3):
            first_size = path.stat().st_size
            first_mtime = path.stat().st_mtime_ns
            time.sleep(wait_seconds)
            second_size = path.stat().st_size
            second_mtime = path.stat().st_mtime_ns
            if first_size > 0 and second_size == first_size and second_mtime == first_mtime:
                return True
            log.debug(f"unstable attempt {attempt+1}: {path.name}")
        return False
    except OSError as exc:
        log.warning(f"Stability check failed: {path} - {exc}")
        return False


def should_skip(path: Path, file_hash: str, manifest: dict[str, Any]) -> bool:
    """Check if file was already successfully processed (same hash)."""
    previous = manifest.get(str(path))
    if not previous:
        return False
    return previous.get("hash") == file_hash and previous.get("status") == "success"


def find_target_for_file(path: Path, targets: list[WatchTarget]) -> WatchTarget | None:
    """Determine which watch target a file belongs to."""
    for target in targets:
        if file_matches_target(path, target):
            return target
    return None


# ─── S3 Upload via EC2 ────────────────────────────────────────────────────────

def upload_to_s3(
    file_path: Path, target: WatchTarget, config: Config
) -> tuple[bool, str, str, str]:
    """
    Upload file to S3 via EC2 API endpoint.
    Returns: (success, s3_key, s3_url, error_message)
    """
    try:
        relative_path = file_path.relative_to(target.watch_dir)
    except ValueError:
        relative_path = Path(file_path.name)

    s3_key = target.s3_prefix + str(relative_path).replace("\\", "/")
    s3_url = f"https://{config.s3_bucket}.s3.ap-northeast-1.amazonaws.com/{s3_key}"

    try:
        with file_path.open("rb") as fp:
            files = {"file": (file_path.name, fp)}
            data = {
                "s3_bucket": config.s3_bucket,
                "s3_key": s3_key,
                "source_path": str(file_path),
                "file_name": file_path.name,
            }
            response = requests.post(
                config.s3_upload_url,
                files=files,
                data=data,
                timeout=config.request_timeout_seconds,
            )
        if not response.ok:
            return False, s3_key, s3_url, f"HTTP {response.status_code}: {response.text[:300]}"

        result = response.json()
        if result.get("ok"):
            return True, s3_key, s3_url, ""
        else:
            return False, s3_key, s3_url, result.get("error", "Unknown error")
    except Exception as exc:
        return False, s3_key, s3_url, str(exc)


# ─── AI Gateway Analysis (EC2 writes to SQLite) ──────────────────────────────

def analyze_with_ai(
    file_path: Path,
    file_hash: str,
    s3_key: str,
    s3_url: str,
    target: WatchTarget,
    config: Config,
) -> tuple[bool, str]:
    """
    Send file to EC2 AI gateway for content analysis.
    EC2 will parse and store results in the appropriate SQLite table.
    Returns: (success, message)
    """
    try:
        with file_path.open("rb") as fp:
            files = {"file": (file_path.name, fp)}
            data = {
                "s3_key": s3_key,
                "s3_url": s3_url,
                "file_name": file_path.name,
                "file_hash": file_hash,
                "source_path": str(file_path),
                "db_name": config.db_name,
                "db_table": target.db_table,
                "target_name": target.name,
            }
            response = requests.post(
                config.ai_gateway_url,
                files=files,
                data=data,
                timeout=config.request_timeout_seconds,
            )
        if not response.ok:
            return False, f"HTTP {response.status_code}: {response.text[:300]}"

        result = response.json()
        if result.get("ok"):
            rows = result.get("rows_inserted", "?")
            return True, f"stored {rows} rows in {target.db_table}"
        else:
            return False, result.get("error", "AI analysis failed")
    except Exception as exc:
        return False, str(exc)


# ─── Worker ───────────────────────────────────────────────────────────────────

class UploadWorker:
    def __init__(self, config: Config, manifest: dict[str, Any]):
        self.config = config
        self.manifest = manifest
        self.queue: queue.Queue[Path] = queue.Queue()
        self.pending: dict[str, float] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        # Backoff
        self._failure_count = 0
        self._max_failures = 3
        self._in_backoff = False

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.queue.put(Path("__STOP__"))
        self.thread.join(timeout=10)

    def enqueue(self, path: Path) -> None:
        if not is_valid_file(path):
            return
        # Must belong to at least one target
        target = find_target_for_file(path, self.config.targets)
        if not target:
            return
        key = str(path)
        now = time.time()
        with self.lock:
            last = self.pending.get(key, 0)
            if now - last < self.config.debounce_seconds:
                return
            self.pending[key] = now
        log.info(f"enqueued [{target.name}]: {path.name}")
        self.queue.put(path)

    def process_path(self, path: Path) -> None:
        if not is_valid_file(path):
            return

        target = find_target_for_file(path, self.config.targets)
        if not target:
            return

        if not is_stable_file(path, self.config.stable_check_seconds):
            log.warning(f"skip unstable: {path}")
            return

        try:
            file_hash = compute_file_hash(path)
            file_size = path.stat().st_size
            mtime = file_mtime_iso(path)
        except OSError as exc:
            log.warning(f"Cannot read file: {path} - {exc}")
            return

        # Check manifest for dedup
        if should_skip(path, file_hash, self.manifest):
            log.debug(f"already processed (manifest): {path.name}")
            return

        # Step 1: Upload to S3
        log.info(f"[{target.name}] uploading to S3: {path.name} ({file_size} bytes)")
        ok, s3_key, s3_url, err = upload_to_s3(path, target, self.config)

        if not ok:
            log.error(f"[{target.name}] S3 upload FAILED: {path.name} - {err}")
            self._handle_failure(path, f"S3: {err}")
            return

        log.info(f"[{target.name}] S3 OK: {s3_key}")

        # Step 2: AI Analysis → EC2 writes to SQLite
        log.info(f"[{target.name}] AI → {target.db_table}: {path.name}")
        ai_ok, ai_msg = analyze_with_ai(
            path, file_hash, s3_key, s3_url, target, self.config
        )

        if ai_ok:
            log.info(f"[{target.name}] AI OK: {path.name} - {ai_msg}")
        else:
            log.warning(f"[{target.name}] AI FAILED: {path.name} - {ai_msg}")

        # Update local manifest
        self.manifest[str(path)] = {
            "hash": file_hash,
            "size": file_size,
            "mtime": mtime,
            "s3_key": s3_key,
            "s3_url": s3_url,
            "target": target.name,
            "db_table": target.db_table,
            "status": "success",
            "ai_status": "success" if ai_ok else "failed",
            "processed_at": utc_iso(),
        }
        save_manifest(self.config.manifest_path, self.manifest)

        # Reset backoff on success
        if self._in_backoff:
            log.info("[backoff] 恢復正常")
            send_line_message(
                f"✅ Tutti Watcher 恢復正常\n檔案: {path.name}",
                source="tutti_pur_baseline",
            )
        self._failure_count = 0
        self._in_backoff = False

    def _handle_failure(self, path: Path, error_msg: str) -> None:
        self._failure_count += 1
        if self._failure_count >= self._max_failures and not self._in_backoff:
            self._in_backoff = True
            log.warning(f"[backoff] 連續失敗 {self._failure_count} 次")
            send_line_message(
                f"⚠️ Tutti Watcher 連續失敗 {self._failure_count} 次\n"
                f"檔案: {path.name}\n"
                f"錯誤: {error_msg[:100]}",
                source="tutti_pur_baseline",
            )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                path = self.queue.get(timeout=2)
            except queue.Empty:
                continue
            try:
                if self.stop_event.is_set():
                    return
                self.process_path(path)
            except Exception as exc:
                log.exception(f"Unexpected error processing {path}: {exc}")
            finally:
                self.queue.task_done()


# ─── Watchdog Handler ─────────────────────────────────────────────────────────

class TuttiFileHandler(FileSystemEventHandler):
    def __init__(self, worker: UploadWorker):
        self.worker = worker

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._scan_directory(Path(event.src_path))
        else:
            self.worker.enqueue(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.worker.enqueue(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._scan_directory(Path(event.dest_path))
        else:
            self.worker.enqueue(Path(event.dest_path))

    def _scan_directory(self, directory: Path) -> None:
        try:
            for root, _, filenames in os.walk(directory):
                for filename in filenames:
                    path = Path(root) / filename
                    self.worker.enqueue(path)
        except OSError as exc:
            log.warning(f"scan directory failed: {directory} - {exc}")


# ─── Initial Scan ────────────────────────────────────────────────────────────

def initial_scan(config: Config, worker: UploadWorker) -> None:
    start = time.time()
    scanned = 0
    matched = 0

    for target in config.targets:
        if not target.watch_dir.exists():
            log.warning(f"[{target.name}] watch dir not reachable: {target.watch_dir}")
            continue

        log.info(f"[{target.name}] scanning: {target.watch_dir}")

        if target.recursive:
            for root, _, filenames in os.walk(target.watch_dir):
                for filename in filenames:
                    scanned += 1
                    path = Path(root) / filename
                    if is_valid_file(path) and file_matches_target(path, target):
                        matched += 1
                        worker.enqueue(path)
        else:
            # Non-recursive: only scan the watch_dir itself
            for item in target.watch_dir.iterdir():
                if item.is_file():
                    scanned += 1
                    if is_valid_file(item) and file_matches_target(item, target):
                        matched += 1
                        worker.enqueue(item)

    elapsed = int(time.time() - start)
    log.info(f"scan done: scanned={scanned} matched={matched} elapsed={elapsed}s")


# ─── Main ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="監控 Tutti 資料夾，上傳到 S3 並透過 EC2 AI Gateway 分析寫入 SQLite"
    )
    parser.add_argument(
        "--config",
        default="tutti_pur_baseline_config.json",
        help="Path to config JSON",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只執行一次掃描後結束",
    )
    parser.add_argument(
        "--no-initial-scan",
        action="store_true",
        help="啟動時不做全量掃描",
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="直接處理單一檔案或資料夾",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = script_dir / config_path

    if not config_path.exists():
        log.error(f"Missing config: {config_path}")
        return 1

    config = load_config(config_path)
    manifest = load_manifest(config.manifest_path)

    # Init worker
    worker = UploadWorker(config, manifest)
    worker.start()

    log.info("=" * 60)
    log.info("Tutti Pur Baseline Watcher started")
    for target in config.targets:
        status = "OK" if target.watch_dir.exists() else "NOT REACHABLE"
        pattern_info = f" (pattern: {target.file_pattern.pattern})" if target.file_pattern else ""
        log.info(
            f"  [{target.name}] {target.watch_dir} [{status}]"
            f" → S3 {target.s3_prefix} → DB {target.db_table}{pattern_info}"
        )
    log.info(f"  S3 bucket: {config.s3_bucket}")
    log.info(f"  AI Gateway: {config.ai_gateway_url}")
    log.info(f"  EC2 DB: {config.db_name}")
    log.info(f"  Manifest: {config.manifest_path}")
    log.info("=" * 60)

    # Single path mode
    if args.path:
        target_path = Path(args.path)
        if target_path.is_file():
            worker.process_path(target_path)
        elif target_path.is_dir():
            for root, _, filenames in os.walk(target_path):
                for filename in filenames:
                    p = Path(root) / filename
                    if is_valid_file(p):
                        worker.process_path(p)
        else:
            log.error(f"Path not found: {target_path}")
        worker.queue.join()
        worker.stop()
        return 0

    # Once mode
    if args.once:
        if not args.no_initial_scan:
            initial_scan(config, worker)
        worker.queue.join()
        worker.stop()
        return 0

    # Continuous watching mode (use PollingObserver for SMB/CIFS)
    # Collect unique watch dirs to observe
    watch_dirs_to_observe: dict[str, WatchTarget] = {}
    for target in config.targets:
        if target.watch_dir.exists():
            watch_dirs_to_observe[str(target.watch_dir)] = target

    if not watch_dirs_to_observe:
        log.error("No watch directories accessible")
        log.error("請先執行 setup-smb-reagent-rd.sh 掛載 SMB share")
        worker.stop()
        return 1

    observer = PollingObserver(timeout=config.rescan_interval_seconds)
    handler = TuttiFileHandler(worker)
    for watch_dir_str in watch_dirs_to_observe:
        target = watch_dirs_to_observe[watch_dir_str]
        observer.schedule(handler, watch_dir_str, recursive=target.recursive)
        log.info(f"PollingObserver scheduled: {watch_dir_str} (recursive={target.recursive})")
    observer.start()
    log.info(f"PollingObserver started (interval={config.rescan_interval_seconds}s)")

    if not args.no_initial_scan:
        initial_scan(config, worker)

    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("Stopping...")
    finally:
        observer.stop()
        observer.join(timeout=10)
        worker.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
