from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# LINE 通知
try:
    from line_notify import send_line_message
except ImportError:
    def send_line_message(msg, source="default"):
        return False


ASSAYPROCESS_RE = re.compile(r"^AssayProcess_\d{14}\.csv$", re.IGNORECASE)


@dataclass(frozen=True)
class Config:
    watch_roots: list[Path]
    upload_url: str
    manifest_path: Path
    stable_check_seconds: int = 2
    debounce_seconds: int = 5
    request_timeout_seconds: int = 120
    rescan_interval_seconds: int = 300


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_text()}] {msg}", flush=True)


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    with path.open("r", encoding="utf-8-sig") as fp:
        raw = json.load(fp)

    config_dir = path.parent
    manifest_path = Path(raw.get("manifest_path", "upload_manifest.json"))
    if not manifest_path.is_absolute():
        manifest_path = config_dir / manifest_path

    # Support both "watch_root" (single) and "watch_roots" (list)
    if "watch_roots" in raw:
        roots = [Path(r) for r in raw["watch_roots"]]
    else:
        roots = [Path(raw["watch_root"])]

    return Config(
        watch_roots=roots,
        upload_url=str(raw["upload_url"]),
        manifest_path=manifest_path,
        stable_check_seconds=int(raw.get("stable_check_seconds", 2)),
        debounce_seconds=int(raw.get("debounce_seconds", 5)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 120)),
        rescan_interval_seconds=int(raw.get("rescan_interval_seconds", 300)),
    )


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log(f"Manifest read failed, using empty manifest: {exc}")
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


def is_assayprocess_timestamp_csv(path: Path) -> bool:
    return path.is_file() and ASSAYPROCESS_RE.match(path.name) is not None


def file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "hash": digest.hexdigest(),
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
    }


def is_stable_file(path: Path, wait_seconds: int) -> bool:
    try:
        for attempt in range(3):
            first_size = path.stat().st_size
            time.sleep(wait_seconds)
            second_size = path.stat().st_size
            if first_size > 0 and second_size == first_size:
                return True
            log(f"unstable attempt {attempt+1}: {path.name} size {first_size}->{second_size}")
        return False
    except OSError as exc:
        log(f"Stability check failed: {path} {exc}")
        return False


def should_skip(path: Path, signature: dict[str, Any], manifest: dict[str, Any]) -> bool:
    previous = manifest.get(str(path))
    if not previous:
        return False
    return (
        previous.get("Hash") == signature["hash"]
        and int(previous.get("Size", -1)) == int(signature["size"])
        and bool(previous.get("Baseline", False)) == is_baseline_file(path)
    )


def is_baseline_file(path: Path) -> bool:
    return any(part.endswith("建線") for part in path.parent.parts)


def update_manifest(path: Path, signature: dict[str, Any], manifest: dict[str, Any], backend_result: dict[str, Any]) -> None:
    baseline = is_baseline_file(path)
    manifest[str(path)] = {
        "FullName": str(path),
        "Hash": signature["hash"],
        "Size": signature["size"],
        "LastWriteTime": signature["mtime"],
        "Baseline": baseline,
        "UploadedAt": utc_iso(),
        "BackendStatus": backend_result.get("status"),
        "RowsRead": backend_result.get("rows_read"),
        "RowsInserted": backend_result.get("rows_inserted"),
        "RowsIgnored": backend_result.get("rows_ignored"),
        "NaturalKeyEnabled": backend_result.get("natural_key_enabled"),
    }


def upload_file(path: Path, signature: dict[str, Any], config: Config) -> tuple[bool, str, dict[str, Any] | None]:
    try:
        baseline = is_baseline_file(path)
        with path.open("rb") as fp:
            files = {"file": (path.name, fp, "text/csv")}
            data = {
                "source_file": str(path),
                "source_file_name": path.name,
                "file_mtime": signature["mtime"],
                "baseline": "true" if baseline else "false",
            }
            response = requests.post(
                config.upload_url,
                files=files,
                data=data,
                timeout=config.request_timeout_seconds,
            )
        body = response.text
        if not response.ok:
            return False, f"HTTP {response.status_code}: {body[:500]}", None

        result = response.json()
        if not result.get("ok"):
            return False, f"backend check failed: {result.get('error') or result}", result

        status = result.get("status", "unknown")
        if status == "skipped":
            return True, "backend skipped unchanged", result

        inserted = result.get("rows_inserted")
        ignored = result.get("rows_ignored")
        rows_read = result.get("rows_read")
        return True, f"baseline={baseline} backend {status}: rows_read={rows_read} inserted={inserted} ignored={ignored}", result
    except Exception as exc:
        return False, str(exc), None


class UploadWorker:
    def __init__(self, config: Config, manifest: dict[str, Any]):
        self.config = config
        self.manifest = manifest
        self.queue: queue.Queue[Path] = queue.Queue()
        self.pending: dict[str, float] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        # Backoff 追蹤
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
        if not ASSAYPROCESS_RE.match(path.name):
            return
        key = str(path)
        # Fast skip: if manifest already has this file with matching size and baseline flag, skip early.
        previous = self.manifest.get(key)
        if previous:
            try:
                current_size = path.stat().st_size
                if (
                    int(previous.get("Size", -1)) == current_size
                    and bool(previous.get("Baseline", False)) == is_baseline_file(path)
                ):
                    return
            except OSError:
                pass
        now = time.time()
        with self.lock:
            last = self.pending.get(key, 0)
            if now - last < self.config.debounce_seconds:
                return
            self.pending[key] = now
        log(f"enqueued: {path}")
        self.queue.put(path)

    def process_path(self, path: Path) -> None:
        if not is_assayprocess_timestamp_csv(path):
            return
        if not is_stable_file(path, self.config.stable_check_seconds):
            log(f"skip unstable: {path}")
            return

        try:
            signature = file_signature(path)
        except OSError as exc:
            log(f"signature failed: {path} {exc}")
            return

        if should_skip(path, signature, self.manifest):
            return

        ok, message, result = upload_file(path, signature, self.config)
        if ok:
            log(f"uploaded: {path.name} {message}")
            update_manifest(path, signature, self.manifest, result or {})
            save_manifest(self.config.manifest_path, self.manifest)
            # 成功 → 重置
            if self._in_backoff:
                log("[backoff] 上傳恢復正常")
                send_line_message(
                    f"✅ AssayProcess 上傳恢復正常\n檔案: {path.name}",
                    source="assayprocess",
                )
            self._failure_count = 0
            self._in_backoff = False
        else:
            log(f"upload failed: {path} {message}")
            self._failure_count += 1
            if self._failure_count >= self._max_failures and not self._in_backoff:
                self._in_backoff = True
                log(f"[backoff] 連續失敗 {self._failure_count} 次")
                send_line_message(
                    f"⚠️ AssayProcess 上傳連續失敗 {self._failure_count} 次\n"
                    f"檔案: {path.name}\n"
                    f"錯誤: {message[:100]}",
                    source="assayprocess",
                )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            path = self.queue.get()
            try:
                if self.stop_event.is_set():
                    return
                self.process_path(path)
            finally:
                self.queue.task_done()


class AssayProcessEventHandler(FileSystemEventHandler):
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
                    if ASSAYPROCESS_RE.match(filename):
                        path = Path(root) / filename
                        log(f"found in new dir: {path}")
                        self.worker.enqueue(path)
        except OSError as exc:
            log(f"scan new directory failed: {directory} {exc}")


def initial_scan(config: Config, worker: UploadWorker) -> None:
    start = time.time()
    scanned = 0
    matched = 0
    for watch_root in config.watch_roots:
        if not watch_root.exists():
            log(f"scan skipped (not reachable): {watch_root}")
            continue
        log(f"scan started: {watch_root}")
        for root, _, filenames in os.walk(watch_root):
            for filename in filenames:
                scanned += 1
                if scanned % 5000 == 0:
                    log(f"scanning... {scanned} files checked, {matched} matched")
                if not ASSAYPROCESS_RE.match(filename):
                    continue
                path = Path(root) / filename
                matched += 1
                worker.enqueue(path)
    elapsed = int(time.time() - start)
    log(f"scan done: scanned={scanned} matched={matched} elapsed={elapsed}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch and upload AssayProcess timestamp CSV files.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--no-initial-scan", action="store_true", help="Start watcher without initial recursive scan.")
    parser.add_argument("--once", action="store_true", help="Run initial scan and exit after the upload queue drains.")
    parser.add_argument("--path", type=str, default=None, help="Process a single file or folder path directly (skip full scan).")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = script_dir / config_path

    if not config_path.exists():
        print(f"Missing {config_path}. Copy config.example.json to config.json first.")
        return 1

    config = load_config(config_path)
    reachable = [r for r in config.watch_roots if r.exists()]
    if not reachable:
        print(f"No watch roots reachable: {config.watch_roots}")
        return 1

    manifest = load_manifest(config.manifest_path)
    worker = UploadWorker(config, manifest)
    worker.start()

    log("watcher started")
    for r in config.watch_roots:
        status = "OK" if r.exists() else "NOT REACHABLE"
        print(f"  WatchRoot: {r} [{status}]", flush=True)
    print(f"UploadUrl: {config.upload_url}", flush=True)
    print(f"Pattern: AssayProcess_YYYYMMDDHHMMSS.csv", flush=True)

    if args.path:
        target = Path(args.path)
        if target.is_file():
            worker.process_path(target)
        elif target.is_dir():
            for root, _, filenames in os.walk(target):
                for filename in filenames:
                    p = Path(root) / filename
                    if is_assayprocess_timestamp_csv(p):
                        worker.process_path(p)
        else:
            print(f"Path not found: {target}")
        worker.stop()
        return 0

    if args.once:
        if not args.no_initial_scan:
            initial_scan(config, worker)
        worker.queue.join()
        worker.stop()
        return 0

    log("watching changes. Press Ctrl+C to stop.")
    print(f"Trigger: echo <path> > trigger.txt to scan immediately", flush=True)

    observer = Observer()
    handler = AssayProcessEventHandler(worker)
    for watch_root in config.watch_roots:
        if watch_root.exists():
            observer.schedule(handler, str(watch_root), recursive=True)
            log(f"observer scheduled: {watch_root}")
    observer.start()
    log("native observer started")

    trigger_path = script_dir / "trigger.txt"
    if not args.no_initial_scan:
        initial_scan(config, worker)
    try:
        while True:
            time.sleep(2)
            if trigger_path.exists():
                try:
                    content = trigger_path.read_text(encoding="utf-8-sig").strip()
                    trigger_path.unlink()
                    if content:
                        target = Path(content)
                        log(f"triggered scan: {target}")
                        if target.is_file():
                            worker.enqueue(target)
                        elif target.is_dir():
                            for root, _, filenames in os.walk(target):
                                for filename in filenames:
                                    if ASSAYPROCESS_RE.match(filename):
                                        worker.enqueue(Path(root) / filename)
                    else:
                        log("triggered full rescan")
                        initial_scan(config, worker)
                except Exception as exc:
                    log(f"trigger error: {exc}")
            now = datetime.now()
            if now.hour == 0 and now.minute == 0 and now.second < 3:
                log("midnight full scan...")
                initial_scan(config, worker)
                time.sleep(60)
    except KeyboardInterrupt:
        log("stopping...")
    finally:
        observer.stop()
        observer.join(timeout=10)
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
