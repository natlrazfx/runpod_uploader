# RunPod Uploader v1.9 Natalia Raz

import os
import sys
import subprocess
import shutil
import webbrowser
from dataclasses import dataclass

from dotenv import load_dotenv
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from PySide6.QtCore import Qt, QDir, QObject, Signal, QThread, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QWidget, QVBoxLayout, QTreeView,
    QFileSystemModel, QToolBar, QInputDialog, QLineEdit,
    QMessageBox, QDialog, QFormLayout, QDialogButtonBox, QLineEdit as QLE,
    QLabel, QStatusBar, QProgressBar, QPushButton, QSizePolicy,
    QAbstractItemView, QTableWidget, QTableWidgetItem, QComboBox, QFileDialog
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # script directory
ENV_FILE = os.path.join(BASE_DIR, ".env")
COFFEE_URL = "https://buymeacoffee.com/natlrazfx"
APP_VERSION = "v1.9"


# ---------- CONFIG ----------

@dataclass
class S3Config:
    access_key: str
    secret_key: str
    bucket: str
    endpoint: str
    region: str
    local_root: str     # default local folder shown on the left


def load_config() -> S3Config:
    # Load .env next to the script even if launched from another working directory.
    load_dotenv(ENV_FILE, override=False)

    return S3Config(
        access_key=os.getenv("RUNPOD_S3_ACCESS_KEY", "").strip(),
        secret_key=os.getenv("RUNPOD_S3_SECRET_KEY", "").strip(),
        bucket=os.getenv("RUNPOD_BUCKET", "").strip(),
        endpoint=os.getenv("RUNPOD_ENDPOINT", "").strip(),
        region=os.getenv("RUNPOD_REGION", "").strip(),
        local_root=os.getenv("LOCAL_ROOT", "").strip().strip('"').strip("'"),
    )



def save_config(cfg: S3Config):
    new_values = {
        "RUNPOD_S3_ACCESS_KEY": cfg.access_key,
        "RUNPOD_S3_SECRET_KEY": cfg.secret_key,
        "RUNPOD_BUCKET": cfg.bucket,
        "RUNPOD_ENDPOINT": cfg.endpoint,
        "RUNPOD_REGION": cfg.region,
        "LOCAL_ROOT": cfg.local_root,
    }

    existing_lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    output_lines = []
    seen_keys = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output_lines.append(line.rstrip("\n"))
            continue
        key, sep, _ = stripped.partition("=")
        if sep and key in new_values:
            output_lines.append(f"{key}={new_values[key]}")
            seen_keys.add(key)
        else:
            output_lines.append(line.rstrip("\n"))

    for key, value in new_values.items():
        if key not in seen_keys:
            output_lines.append(f"{key}={value}")

    content = "\n".join(output_lines).rstrip("\n") + "\n"
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(content)


# ---------- S3 WRAPPER ----------

class RunPodS3:
    def __init__(self, cfg: S3Config):
        self.cfg = cfg
        self.read_timeout = self._int_env("RUNPOD_READ_TIMEOUT", 7200)   # allow long uploads (2h+)
        self.connect_timeout = self._int_env("RUNPOD_CONNECT_TIMEOUT", 30)
        self.session = boto3.session.Session()
        client_cfg = Config(
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            retries={"max_attempts": 10, "mode": "standard"},
        )
        self.client = self.session.client(
            service_name="s3",
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            endpoint_url=cfg.endpoint,
            region_name=cfg.region,
            config=client_cfg,
        )

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        val = os.getenv(name)
        if val is None:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        val = os.getenv(name)
        if val is None:
            return default
        val = val.strip().lower()
        if val in ("0", "false", "no", "off"):
            return False
        if val in ("1", "true", "yes", "on"):
            return True
        return default

    def _iter_list_objects_pages(self, prefix: str, delimiter: str | None = None):
        """
        Resilient pagination for S3-compatible endpoints.
        Some providers occasionally return a repeated continuation token.
        """
        token = None
        start_after = None
        seen_tokens = set()
        seen_markers = set()
        while True:
            params = {
                "Bucket": self.cfg.bucket,
                "Prefix": prefix,
            }
            if delimiter:
                params["Delimiter"] = delimiter
            if token:
                params["ContinuationToken"] = token
            elif start_after:
                params["StartAfter"] = start_after

            page = self.client.list_objects_v2(**params)
            yield page

            if not page.get("IsTruncated"):
                break

            next_token = page.get("NextContinuationToken")
            if not next_token or next_token in seen_tokens:
                # Fallback path for buggy providers: continue by StartAfter marker.
                markers = []
                for obj in page.get("Contents", []):
                    key = obj.get("Key")
                    if key:
                        markers.append(key)
                for cp in page.get("CommonPrefixes", []):
                    pfx = cp.get("Prefix")
                    if pfx:
                        markers.append(pfx)

                if not markers:
                    break

                marker = max(markers)
                if marker in seen_markers:
                    break
                seen_markers.add(marker)
                token = None
                start_after = marker
                continue

            seen_tokens.add(next_token)
            token = next_token
            start_after = None

    def list_prefix(self, prefix: str):
        """Return (dirs, files) for the given prefix."""
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        dirs = set()
        files = []

        for page in self._iter_list_objects_pages(prefix=prefix, delimiter="/"):
            # --- 1) folders returned via CommonPrefixes
            for cp in page.get("CommonPrefixes", []):
                name = cp["Prefix"][len(prefix):].strip("/")
                if name:
                    dirs.add(name + "/")

            # --- 2) objects (files + folder markers)
            for obj in page.get("Contents", []):
                key = obj["Key"]

                # Skip the prefix object itself.
                if key == prefix:
                    continue

                name = key[len(prefix):]
                if not name:
                    continue

                # IMPORTANT: "folder/" is a folder marker, not a file.
                if name.endswith("/"):
                    dirs.add(name)
                    continue

                # If API returned a nested path without delimiter, treat it as a folder.
                if "/" in name:
                    first = name.split("/", 1)[0].strip()
                    if first:
                        dirs.add(first + "/")
                    continue

                # RunPod may return a zero-size object without a slash as a folder marker.
                if obj.get("Size", 0) == 0 and "/" not in name and "." not in name:
                    dirs.add(name + "/")
                    continue

                files.append({
                    "Key": key,
                    "Name": name,
                    "Size": obj.get("Size", 0),
                    "LastModified": obj.get("LastModified"),
                })

        return sorted(dirs), files


    def _build_transfer_config(self, filesize: int, force_single_thread: bool = False, bump_part: bool = False) -> TransferConfig:
        """
        Build TransferConfig with controlled part size/threads.
        RUNPOD_PART_SIZE_MB - fixed part size (MB). If set, use it (min 8 MB, max 5 GB - 8 MB).
        RUNPOD_MAX_CONCURRENCY - thread count.
        RUNPOD_UPLOAD_USE_THREADS - '0'/'1' (default 1).
        If bump_part=True, double part size but still cap at 5 GB.
        """
        max_part_mb = 5120 - 8  # just under 5 GB to avoid 413
        env_part_mb = self._int_env("RUNPOD_PART_SIZE_MB", 0)

        if env_part_mb > 0:
            part_mb = env_part_mb
        else:
            target_parts = 3000
            auto_part_mb = max(int((filesize + target_parts - 1) / target_parts / (1024 * 1024)), 8)
            part_mb = max(auto_part_mb, 64)

        if bump_part:
            part_mb *= 2

        part_mb = max(8, min(part_mb, max_part_mb))
        part_size = part_mb * 1024 * 1024

        max_conc = max(self._int_env("RUNPOD_MAX_CONCURRENCY", 4), 1)
        use_threads_env = os.getenv("RUNPOD_UPLOAD_USE_THREADS", "1").strip().lower()
        use_threads = use_threads_env not in ("0", "false", "no", "off")
        if force_single_thread:
            use_threads = False
            max_conc = 1

        return TransferConfig(
            multipart_chunksize=part_size,
            multipart_threshold=part_size,
            max_concurrency=max_conc,
            use_threads=use_threads,
        )

    def object_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.cfg.bucket, Key=key)
            return True
        except self.client.exceptions.NoSuchKey:
            return False
        except Exception:
            # Treat 404/403 and similar as missing.
            return False

    def upload(self, local_path: str, key: str, progress_callback=None):
        if not key:
            raise ValueError("Empty key")
        filesize = os.path.getsize(local_path) or 1
        tracker = ProgressTracker(filesize, progress_callback) if progress_callback else None
        config = self._build_transfer_config(filesize)
        try:
            self.client.upload_file(
                Filename=local_path,
                Bucket=self.cfg.bucket,
                Key=key,
                Callback=tracker,
                Config=config,
            )
        except Exception:
            # fallback: single-threaded; optionally bump part size if explicitly enabled
            bump = self._bool_env("RUNPOD_UPLOAD_FALLBACK_BUMP", False)
            fallback_cfg = self._build_transfer_config(filesize, force_single_thread=True, bump_part=bump)
            self.client.upload_file(
                Filename=local_path,
                Bucket=self.cfg.bucket,
                Key=key,
                Callback=tracker,
                Config=fallback_cfg,
            )

    def download(self, key: str, local_path: str, progress_callback=None):
        head = self.client.head_object(Bucket=self.cfg.bucket, Key=key)
        filesize = head.get("ContentLength", 1)
        tracker = ProgressTracker(filesize, progress_callback) if progress_callback else None
        self.client.download_file(
            Bucket=self.cfg.bucket,
            Key=key,
            Filename=local_path,
            Callback=tracker,
        )

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.cfg.bucket, Key=key)

    def list_all_keys(self, prefix: str) -> list[str]:
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        keys = []
        for page in self._iter_list_objects_pages(prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if key:
                    keys.append(key)
        return keys

    def list_tree_files(self, prefix: str) -> list[str]:
        """
        Recursively list only file keys under prefix by traversing folder levels.
        Uses list_prefix() semantics, which are stable with RunPod listing behavior.
        """
        root = prefix.strip("/")
        pending = [root]
        visited = set()
        file_keys = []

        while pending:
            current = pending.pop(0).strip("/")
            if current in visited:
                continue
            visited.add(current)

            dirs, files = self.list_prefix(current)
            for f in files:
                key = f.get("Key")
                if key:
                    file_keys.append(key)

            for d in dirs:
                dname = d.strip("/")
                if not dname:
                    continue
                child = f"{current}/{dname}" if current else dname
                if child not in visited:
                    pending.append(child)

        return file_keys

    def rename(self, old_key: str, new_key: str):
        self.client.copy_object(
            Bucket=self.cfg.bucket,
            CopySource={"Bucket": self.cfg.bucket, "Key": old_key},
            Key=new_key,
        )
        self.client.delete_object(Bucket=self.cfg.bucket, Key=old_key)

    # Create a folder marker (prefix) in S3.
    def create_folder(self, key: str):
        """
        Create an empty object with a trailing '/' so it shows as a folder.
        """
        if not key.endswith("/"):
            key = key + "/"
        self.client.put_object(Bucket=self.cfg.bucket, Key=key)


class ProgressTracker:
    """boto3 callback that updates the progress bar in percent."""

    def __init__(self, filesize: int, cb):
        self.filesize = max(int(filesize), 1)
        self.seen = 0
        self.cb = cb

    def __call__(self, bytes_amount: int):
        self.seen += bytes_amount
        percent = int(self.seen * 100 / self.filesize)
        percent = min(max(percent, 0), 100)
        if self.cb:
            self.cb(percent)


# ---------- SETTINGS DIALOG ----------

class SettingsDialog(QDialog):
    def __init__(self, cfg: S3Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RunPod S3 Settings")
        self.cfg = cfg

        form = QFormLayout(self)

        self.le_access = QLE(self.cfg.access_key)
        self.le_secret = QLE(self.cfg.secret_key)
        self.le_secret.setEchoMode(QLE.Password)
        self.le_bucket = QLE(self.cfg.bucket)
        self.le_endpoint = QLE(self.cfg.endpoint)
        self.le_region = QLE(self.cfg.region)
        self.le_local_root = QLE(self.cfg.local_root)

        form.addRow("S3 Access Key:", self.le_access)
        form.addRow("S3 Secret Key:", self.le_secret)
        form.addRow("Bucket / Volume ID:", self.le_bucket)
        form.addRow("Endpoint URL:", self.le_endpoint)
        form.addRow("Region:", self.le_region)
        form.addRow("Default local folder:", self.le_local_root)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def get_config(self) -> S3Config:
        return S3Config(
            access_key=self.le_access.text().strip(),
            secret_key=self.le_secret.text().strip(),
            bucket=self.le_bucket.text().strip(),
            endpoint=self.le_endpoint.text().strip(),
            region=self.le_region.text().strip() or "eu-cz-1",
            local_root=self.le_local_root.text().strip(),
        )

    def accept(self):
        cfg = self.get_config()
        if not all([cfg.access_key, cfg.secret_key, cfg.bucket, cfg.endpoint, cfg.region]):
            QMessageBox.critical(self, "Error", "S3 fields must not be empty.")
            return
        self.cfg = cfg
        super().accept()


# ---------- ASYNC LISTING ----------

class ListWorker(QObject):
    finished = Signal(str, list, list)
    error = Signal(str, str)

    def __init__(self, s3: RunPodS3, prefix: str):
        super().__init__()
        self.s3 = s3
        self.prefix = prefix

    def run(self):
        try:
            dirs, files = self.s3.list_prefix(self.prefix)
        except Exception as exc:
            self.error.emit(self.prefix, str(exc))
            return
        self.finished.emit(self.prefix, dirs, files)


# ---------- REMOTE BROWSER (TABLE) ----------

class RemoteBrowser(QWidget):
    """Right panel: list of objects in RunPod storage."""

    def __init__(self, s3: RunPodS3 | None, parent=None):
        super().__init__(parent)
        self.s3 = s3
        self.current_prefix = ""  # e.g. 'ComfyUI/models/vae'
        self._refresh_thread: QThread | None = None
        self._refresh_worker: ListWorker | None = None
        self._pending_refresh = False

        layout = QVBoxLayout(self)
        # Add a small left gap so it does not stick to the splitter.
        layout.setContentsMargins(12, 0, 8, 0)

        self.path_label = QLabel("RunPod storage")
        self.path_label.setStyleSheet("color:#f0f0f0; font-weight:bold;")
        self.path_label.setContentsMargins(0, 4, 0, 4)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Size", "Modified"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)

        layout.addWidget(self.path_label)
        layout.addWidget(self.table)

        self.table.doubleClicked.connect(self.on_double_click)

    @staticmethod
    def human_size(sz: int) -> str:
        if sz is None:
            return ""
        if sz > 1024 * 1024 * 1024:
            return f"{sz/(1024*1024*1024):.2f} GB"
        if sz > 1024 * 1024:
            return f"{sz/(1024*1024):.2f} MB"
        if sz > 1024:
            return f"{sz/1024:.2f} KB"
        return f"{sz} B"

    def populate(self, dirs, files):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        row = 0

        if self.current_prefix:
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(".."))
            self.table.setItem(row, 1, QTableWidgetItem("UP"))
            row += 1

        for d in dirs:
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(d))
            self.table.setItem(row, 1, QTableWidgetItem("DIR"))
            row += 1

        for f in files:
            size_str = self.human_size(f["Size"])
            lm = f["LastModified"]
            lm_str = lm.strftime("%Y-%m-%d %H:%M") if lm else ""
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(f["Name"]))
            self.table.setItem(row, 1, QTableWidgetItem("FILE"))
            self.table.setItem(row, 2, QTableWidgetItem(size_str))
            self.table.setItem(row, 3, QTableWidgetItem(lm_str))
            row += 1

        self.path_label.setText(
            f"RunPod storage: /{self.current_prefix}" if self.current_prefix else "RunPod storage: /"
        )

        self.table.setSortingEnabled(True)
        # Default sort by name.
        self.table.sortItems(0, Qt.AscendingOrder)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)

    def _ensure_prefix_folder(self) -> tuple[bool, bool]:
        # If the current prefix points to a file, offer to convert it to a folder.
        raw_prefix = self.current_prefix.strip("/")
        if not raw_prefix or self.s3 is None:
            return True, False
        if self.s3.object_exists(raw_prefix):
            msg = QMessageBox.question(
                self,
                "Path is a file",
                f"'{raw_prefix}' is a file. Convert it to a folder?\n",
                QMessageBox.Yes | QMessageBox.No,
            )
            if msg == QMessageBox.Yes:
                try:
                    self.s3.delete(raw_prefix)
                    self.s3.create_folder(raw_prefix + "/")
                except Exception as e:
                    QMessageBox.critical(self, "Convert failed", str(e))
                    return False, False
            else:
                self.current_prefix = "/".join(p for p in raw_prefix.split("/")[:-1] if p)
                return True, True
        return True, False

    def refresh(self):
        if self.s3 is None:
            return
        ok, changed = self._ensure_prefix_folder()
        if not ok:
            return
        if changed:
            return self.refresh()
        dirs, files = self.s3.list_prefix(self.current_prefix)
        self.populate(dirs, files)

    def refresh_async(self):
        if self.s3 is None:
            return
        if self._refresh_thread is not None:
            self._pending_refresh = True
            return
        ok, changed = self._ensure_prefix_folder()
        if not ok:
            return
        if changed:
            return self.refresh_async()

        self._pending_refresh = False
        thread = QThread(self)
        worker = ListWorker(self.s3, self.current_prefix)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_refresh_finished)
        worker.error.connect(self._on_refresh_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_refresh_worker)

        self._refresh_thread = thread
        self._refresh_worker = worker
        thread.start()

    def _on_refresh_finished(self, prefix: str, dirs, files):
        if prefix != self.current_prefix:
            return
        self.populate(dirs, files)

    def _on_refresh_error(self, prefix: str, message: str):
        if prefix != self.current_prefix:
            return
        QMessageBox.critical(self, "Error", f"Failed to list storage:\n{message}")

    def _clear_refresh_worker(self):
        self._refresh_thread = None
        self._refresh_worker = None
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh_async()

    def selected_entries(self):
        """Return a list of (name, type) for selected rows."""
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        entries = []
        for r in rows:
            name_item = self.table.item(r, 0)
            type_item = self.table.item(r, 1)
            if not name_item or not type_item:
                continue
            entries.append((name_item.text(), type_item.text()))
        return entries

    def full_key_for_name(self, name: str, typ: str) -> str | None:
        if typ == "UP":
            return None
        prefix = self.current_prefix.strip("/")
        if prefix:
            return prefix + "/" + name.strip("/")
        return name.strip("/")

    def on_double_click(self, index):
        row = index.row()
        name_item = self.table.item(row, 0)
        type_item = self.table.item(row, 1)
        if not name_item or not type_item:
            return
        name, typ = name_item.text(), type_item.text()

        if typ == "UP":
            if self.current_prefix:
                parts = self.current_prefix.split("/")
                parts = parts[:-1]
                self.current_prefix = "/".join([p for p in parts if p])
                self.refresh_async()
        elif typ == "DIR":
            new_prefix = self.current_prefix + "/" + name.strip("/") if self.current_prefix else name.strip("/")
            self.current_prefix = new_prefix
            self.refresh_async()


# ---------- MAIN WINDOW ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"RunPod Uploader {APP_VERSION}")
        self.resize(1200, 700)
        if sys.platform == "darwin":
            self.setUnifiedTitleAndToolBarOnMac(False)

        self.cfg = load_config()
        self.s3 = None
        if self.cfg.access_key and self.cfg.secret_key and self.cfg.bucket:
            self.s3 = RunPodS3(self.cfg)

        self.drive_combo: QComboBox | None = None

        splitter = QSplitter()

        # ----- left panel: local computer -----
        self.local_model = QFileSystemModel()

        root_path = self._resolve_local_root(self.cfg.local_root)


        self.local_view = QTreeView()
        self.local_view.setModel(self.local_model)
        self.set_local_root(root_path)
        self.local_view.setSortingEnabled(True)
        self.local_view.sortByColumn(0, Qt.AscendingOrder)
        self.local_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.local_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_view.setEditTriggers(QTreeView.NoEditTriggers)
        self.local_view.doubleClicked.connect(self.on_local_double_click)

        for col in range(1, 4):
            self.local_view.resizeColumnToContents(col)
        self.local_view.setColumnWidth(0, 260)

        local_container = QWidget()
        local_layout = QVBoxLayout(local_container)
        # Small right margin to separate from the right panel.
        local_layout.setContentsMargins(4, 0, 2, 0)

        local_label = QLabel("Local storage")
        local_label.setStyleSheet("color:#f0f0f0; font-weight:bold;")
        local_label.setContentsMargins(6, 4, 0, 4)

        local_layout.addWidget(local_label)
        local_layout.addWidget(self.local_view)

        # ----- right panel: RunPod storage -----
        self.remote_browser = RemoteBrowser(self.s3)

        splitter.addWidget(local_container)
        splitter.addWidget(self.remote_browser)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(4, 0, 4, 0)
        central_layout.addWidget(splitter)
        self.setCentralWidget(central)

        # Status bar + progress.
        self.status = QStatusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)
        self.setStatusBar(self.status)

        self.init_toolbar()
        self.apply_dark_style()

        if not (self.cfg.access_key and self.cfg.secret_key and self.cfg.bucket):
            self.edit_settings()
        else:
            QTimer.singleShot(0, self.remote_browser.refresh_async)

    def _resolve_local_root(self, path: str) -> str:
        """
        Normalize LOCAL_ROOT:
        - expand ~
        - normalize slashes
        - if missing, fall back to drive root (Windows)
        - if still missing, use QDir.rootPath()
        """
        if not path:
            return QDir.rootPath()

        path = os.path.expanduser(path)
        path = os.path.normpath(path)

        if os.path.isdir(path):
            return path

        # If it looks like a drive path but the folder is missing, open the drive root.
        drive, _ = os.path.splitdrive(path)
        if drive:
            drive_root = drive + os.path.sep
            if os.path.isdir(drive_root):
                return drive_root

        return QDir.rootPath()

    # ----- style -----
    def apply_dark_style(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #202020;
                color: #f0f0f0;
            }
            QTreeView, QTableWidget {
                background-color: #252525;
                color: #f0f0f0;
                gridline-color: #404040;
                selection-background-color: #3a3a3a;
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background-color: #303030;
                color: #f0f0f0;
                padding: 4px;
                border: 1px solid #404040;
            }
            QToolBar {
                background-color: #262626;
                spacing: 4px;
            }
            QToolBar::separator {
                background-color: #3a3a3a;
                width: 1px;
                margin: 4px 6px;
            }
            QToolBar QToolButton {
                background-color: #e0e0e0;
                color: #202020;
                border: 1px solid #b0b0b0;
                padding: 4px 8px;
                border-radius: 4px;
            }
            QToolBar QToolButton:hover {
                background-color: #d0d0d0;
            }
            QStatusBar {
                background-color: #262626;
                color: #f0f0f0;
            }
            QPushButton, QToolBar QToolButton, QComboBox {
                background-color: #444444;
                color: #f0f0f0;
                border: 1px solid #555555;
                padding: 4px 8px;
                border-radius: 4px;
            }
            QPushButton:hover, QToolBar QToolButton:hover {
                background-color: #666666;
            }
            QPushButton#CoffeeButton {
                background-color: #c45bff;
                color: white;
                border: none;
                padding: 4px 14px;
            }
            QProgressBar {
                border: 1px solid #444444;
                background-color: #3a3a3a;
                color: #f0f0f0;
            }
            QProgressBar::chunk {
                background-color: #c45bff;
            }
        """)

    # ----- toolbar -----
    def init_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar { background-color: #262626; }")
        self.addToolBar(tb)

        # Settings
        act_settings = QAction("Settings", self)
        act_settings.triggered.connect(self.edit_settings)
        tb.addAction(act_settings)

        tb.addSeparator()
        act_local_up = QAction("Local Up", self)
        act_local_up.triggered.connect(self.local_up)
        tb.addAction(act_local_up)

        act_local_pick = QAction("Local Folder", self)
        act_local_pick.triggered.connect(self.pick_local_folder)
        tb.addAction(act_local_pick)

        file_browser_name = "Finder" if sys.platform == "darwin" else "File Explorer" if os.name == "nt" else "File Browser"
        act_local_open_root = QAction(f"Open in {file_browser_name}", self)
        act_local_open_root.triggered.connect(self.open_local_root_in_file_browser)
        tb.addAction(act_local_open_root)

        act_local_open_selected = QAction("Open Selected", self)
        act_local_open_selected.triggered.connect(self.open_selected_local_item)
        tb.addAction(act_local_open_selected)

        act_local_delete = QAction("Delete Local", self)
        act_local_delete.triggered.connect(self.delete_local_items)
        tb.addAction(act_local_delete)

        # Drive selector (Windows only).
        if os.name == "nt":
            tb.addSeparator()
            tb.addWidget(QLabel("Drive:"))
            self.drive_combo = QComboBox()
            tb.addWidget(self.drive_combo)
            self.drive_combo.currentIndexChanged.connect(self.change_drive)

        # Upload (left -> right) next to Drive.
        tb.addSeparator()
        act_upload = QAction("Upload", self)
        act_upload.triggered.connect(self.upload_from_local)
        tb.addAction(act_upload)

        # Center block with RunPod link.
        spacer_left = QWidget()
        spacer_left.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer_left)

        link_color = "#1f1f1f" if sys.platform == "darwin" else "#a6c8ff"
        runpod_label = QLabel(
            f'<a style="color:{link_color}; font-weight:bold;" '
            'href="https://console.runpod.io/deploy">RunPod Console</a>'
        )
        runpod_label.setOpenExternalLinks(True)
        tb.addWidget(runpod_label)

        spacer_right = QWidget()
        spacer_right.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer_right)

        # Right block: operations for RunPod (right panel).
        act_download = QAction("Download", self)
        act_download.triggered.connect(self.download_to_local)
        tb.addAction(act_download)

        act_delete = QAction("Delete", self)
        act_delete.triggered.connect(self.delete_remote)
        tb.addAction(act_delete)

        act_rename = QAction("Rename", self)
        act_rename.triggered.connect(self.rename_remote)
        tb.addAction(act_rename)

        act_refresh = QAction("Refresh", self)
        act_refresh.triggered.connect(self.remote_refresh)
        tb.addAction(act_refresh)

        # Attribution line near the Donate button.
        text_color = "#333333" if sys.platform == "darwin" else "#cccccc"
        info_label = QLabel(
            f'<span style="font-size:10px; color:{text_color};">'
            f'<a style="color:{link_color};" href="https://www.linkedin.com/in/natalia-raz-0b8329120/">Natalia Raz</a> &nbsp;|&nbsp; '
            f'<a style="color:{link_color};" href="https://github.com/natlrazfx">GitHub</a> &nbsp;|&nbsp; '
            f'<a style="color:{link_color};" href="https://vimeo.com/552106671">Vimeo</a>'
            '</span>'
        )
        info_label.setOpenExternalLinks(True)
        tb.addWidget(info_label)

        coffee_btn = QPushButton("Coffee ☕")
        coffee_btn.setObjectName("CoffeeButton")
        coffee_btn.clicked.connect(lambda: webbrowser.open(COFFEE_URL))
        coffee_btn.setToolTip(COFFEE_URL)
        tb.addWidget(coffee_btn)

    def init_drive_combo(self, root_path: str):
        if self.drive_combo is None:
            return
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()
        drives = QDir.drives()
        idx_to_select = 0
        root_norm = os.path.normcase(os.path.abspath(root_path)).rstrip("\\/")
        for i, d in enumerate(drives):
            path = d.absoluteFilePath()
            self.drive_combo.addItem(path)
            drive_norm = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
            if root_norm.startswith(drive_norm):
                idx_to_select = i
        self.drive_combo.setCurrentIndex(idx_to_select)
        self.drive_combo.blockSignals(False)

    # ----- helpers -----
    def edit_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        dlg.setStyleSheet(self.styleSheet())  # keep app styling
        if dlg.exec() == QDialog.Accepted:
            updated_cfg = dlg.cfg
            updated_cfg.local_root = updated_cfg.local_root.strip().strip('"').strip("'")
            self.cfg = updated_cfg
            save_config(self.cfg)
            try:
                self.s3 = RunPodS3(self.cfg)
                self.remote_browser.s3 = self.s3
                # Update local root.
                self.set_local_root(self.cfg.local_root)
                self.remote_browser.current_prefix = ""
                self.remote_browser.refresh_async()
                self.status.showMessage("Settings updated", 4000)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to init S3 client:\n{e}")

    def remote_refresh(self):
        self.remote_browser.refresh_async()

    def change_drive(self, idx: int):
        if idx < 0 or self.drive_combo is None:
            return
        path = self.drive_combo.itemText(idx)
        if not path:
            return
        self.set_local_root(path)

    def set_local_root(self, path: str):
        path = self._resolve_local_root(path)
        self.local_model.setRootPath(path)
        self.local_view.setRootIndex(self.local_model.index(path))
        self.init_drive_combo(path)

    def local_up(self):
        current_root = self.local_model.rootPath()
        if not current_root:
            return
        qdir = QDir(current_root)
        if qdir.cdUp():
            self.set_local_root(qdir.absolutePath())

    def pick_local_folder(self):
        start_dir = self.local_model.rootPath() or QDir.homePath()
        path = QFileDialog.getExistingDirectory(self, "Select local folder", start_dir)
        if path:
            self.set_local_root(path)

    @staticmethod
    def open_path_in_system(path: str) -> bool:
        if not path:
            return False
        target = os.path.abspath(path)
        if not os.path.exists(target):
            return False
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
                return True
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    @staticmethod
    def reveal_path_in_file_browser(path: str) -> bool:
        if not path:
            return False
        target = os.path.abspath(path)
        if not os.path.exists(target):
            return False
        try:
            if sys.platform == "darwin":
                if os.path.isfile(target):
                    subprocess.Popen(["open", "-R", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            if os.name == "nt":
                if os.path.isfile(target):
                    subprocess.Popen(["explorer", "/select,", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.Popen(["explorer", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def open_local_root_in_file_browser(self):
        target = self.first_selected_local_path() or self.local_model.rootPath() or QDir.homePath()
        if not self.reveal_path_in_file_browser(target):
            QMessageBox.critical(self, "Open folder failed", f"Cannot open in file browser:\n{target}")

    def first_selected_local_path(self) -> str | None:
        sm = self.local_view.selectionModel()
        if sm:
            for idx in sm.selectedIndexes():
                if idx.column() == 0:
                    return self.local_model.filePath(idx)
        current = self.local_view.currentIndex()
        if current.isValid():
            return self.local_model.filePath(current.siblingAtColumn(0))
        return None

    def open_selected_local_item(self):
        path = self.first_selected_local_path()
        if not path:
            QMessageBox.information(self, "Open local item", "Select a file or folder on the left side first.")
            return
        if not self.open_path_in_system(path):
            QMessageBox.critical(self, "Open failed", f"Cannot open:\n{path}")

    def on_local_double_click(self, index):
        if not index.isValid() or index.column() != 0:
            return
        path = self.local_model.filePath(index)
        if os.path.isfile(path):
            self.open_selected_local_item()

    def _progress_cb(self, percent: int):
        self.progress.setVisible(True)
        self.progress.setValue(percent)
        QApplication.processEvents()

    def reset_progress(self):
        self.progress.setVisible(False)
        self.progress.setValue(0)

    def current_local_dir(self) -> str:
        """Return the folder to save downloaded files."""
        indexes = self.local_view.selectionModel().selectedIndexes() if self.local_view.selectionModel() else []
        dir_path = None
        for idx in indexes:
            if idx.column() != 0:
                continue
            path = self.local_model.filePath(idx)
            if os.path.isdir(path):
                dir_path = path
                break
            else:
                dir_path = os.path.dirname(path)
                break
        if not dir_path:
            dir_path = self.local_model.rootPath()
        return dir_path

    def selected_local_files(self):
        """Selected files on the left (multi-select)."""
        if not self.local_view.selectionModel():
            return []
        paths = set()
        for idx in self.local_view.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            path = self.local_model.filePath(idx)
            if os.path.isfile(path):
                paths.add(path)
        return sorted(paths)

    def selected_local_items(self) -> list[str]:
        if not self.local_view.selectionModel():
            return []
        paths = set()
        for idx in self.local_view.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            path = self.local_model.filePath(idx)
            if path:
                paths.add(os.path.abspath(path))
        if not paths:
            current = self.local_view.currentIndex()
            if current.isValid():
                paths.add(os.path.abspath(self.local_model.filePath(current.siblingAtColumn(0))))
        # Avoid duplicated recursive deletes: if parent dir selected, skip children.
        ordered = sorted(paths, key=lambda p: (p.count(os.sep), len(p)))
        filtered = []
        for p in ordered:
            if any(p == parent or p.startswith(parent + os.sep) for parent in filtered if os.path.isdir(parent)):
                continue
            filtered.append(p)
        return filtered

    def delete_local_items(self):
        items = self.selected_local_items()
        if not items:
            QMessageBox.information(self, "Delete local", "Select one or more files/folders on the left side.")
            return
        names = "\n".join(items[:12])
        if len(items) > 12:
            names += f"\n... and {len(items) - 12} more"
        reply = QMessageBox.question(
            self,
            "Delete local items",
            f"Delete selected local items?\n\n{names}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        for path in items:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                QMessageBox.critical(self, "Delete failed", f"{path}\n\n{e}")
                return
        self.status.showMessage("Local items deleted", 4000)

    # ----- name conflict dialog -----

    def ask_name_conflict(self, context: str, path: str) -> str:
        """
        Returns one of:
        'replace', 'copy', 'rename', 'skip'
        """
        msg = QMessageBox(self)
        msg.setWindowTitle("Name conflict")
        msg.setIcon(QMessageBox.Warning)
        msg.setText(f"{context}:\n{path}\n\nFile with this name already exists.")
        replace_btn = msg.addButton("Replace", QMessageBox.AcceptRole)
        copy_btn = msg.addButton("Make copy", QMessageBox.ActionRole)
        rename_btn = msg.addButton("Rename", QMessageBox.ActionRole)
        skip_btn = msg.addButton("Skip", QMessageBox.RejectRole)
        msg.setDefaultButton(copy_btn)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == replace_btn:
            return "replace"
        if clicked == copy_btn:
            return "copy"
        if clicked == rename_btn:
            return "rename"
        return "skip"

    @staticmethod
    def make_copy_name(path: str) -> str:
        """file.ext -> file_copy.ext"""
        folder = os.path.dirname(path)
        base = os.path.basename(path)
        if "." in base:
            name, ext = base.rsplit(".", 1)
            new_base = f"{name}_copy.{ext}"
        else:
            new_base = base + "_copy"
        return os.path.join(folder, new_base)

    @staticmethod
    def make_copy_key(key: str) -> str:
        if "/" in key:
            prefix, base = key.rsplit("/", 1)
            folder = prefix + "/"
        else:
            folder = ""
            base = key
        if "." in base:
            name, ext = base.rsplit(".", 1)
            new_base = f"{name}_copy.{ext}"
        else:
            new_base = base + "_copy"
        return (folder + new_base).lstrip("/")

    # ----- upload / download / delete / rename / new folder -----

    def ensure_remote_folder(self, prefix: str) -> bool:
        """
        Ensure each prefix level is not occupied by a file.
        If a file is found, offer to delete and replace it with a folder marker.
        """
        if not prefix:
            return True

        parts = [p for p in prefix.strip("/").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            # If it exists as a file (no trailing slash), that is a conflict.
            if self.s3.object_exists(current):
                res = QMessageBox.question(
                    self,
                    "Path is a file",
                    f"'{current}' exists as a file. Replace it with a folder?\n"
                    "The file will be deleted and replaced with an empty folder.",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if res != QMessageBox.Yes:
                    return False
                try:
                    self.s3.delete(current)
                except Exception as e:
                    QMessageBox.critical(self, "Delete failed", str(e))
                    return False
            # Try to create the folder (S3 ignores it if it already exists).
            try:
                self.s3.create_folder(current + "/")
            except Exception:
                pass

        return True

    def upload_from_local(self):
        if self.s3 is None:
            QMessageBox.warning(self, "No config", "Configure RunPod S3 in Settings first.")
            return

        files = self.selected_local_files()
        if not files:
            QMessageBox.information(self, "Local file", "Select one or more files on the left side.")
            return

        base_prefix = self.remote_browser.current_prefix.strip("/")
        if not self.ensure_remote_folder(base_prefix):
            return

        for local_path in files:
            fname = os.path.basename(local_path)
            key = f"{base_prefix}/{fname}" if base_prefix else fname
            key = key.lstrip("/")

            # Conflict check.
            if self.s3.object_exists(key):
                action = self.ask_name_conflict("Upload to RunPod", key)
                if action == "skip":
                    continue
                elif action == "copy":
                    key = self.make_copy_key(key)
                elif action == "rename":
                    new_name, ok = QInputDialog.getText(
                        self, "Rename before upload", "New file name:", QLineEdit.Normal, fname
                    )
                    if not ok or not new_name.strip():
                        continue
                    new_name = new_name.strip()
                    prefix = self.remote_browser.current_prefix.strip("/")
                    key = f"{prefix}/{new_name}" if prefix else new_name
                    key = key.lstrip("/")
                # replace: overwrite in place

            try:
                self.status.showMessage(f"Uploading {fname} -> {key} ...")
                self._progress_cb(0)
                self.s3.upload(local_path, key, progress_callback=self._progress_cb)
                self.reset_progress()
            except Exception as e:
                self.reset_progress()
                QMessageBox.critical(self, "Upload failed", str(e))
                return

        self.status.showMessage("Upload completed", 4000)
        self.remote_browser.refresh_async()

    def download_to_local(self):
        if self.s3 is None:
            QMessageBox.warning(self, "No config", "Configure RunPod S3 in Settings first.")
            return

        entries = self.remote_browser.selected_entries()
        if not entries:
            QMessageBox.information(self, "Remote item", "Select one or more files/folders on the right side.")
            return

        target_dir = self.current_local_dir()

        for name, typ in entries:
            key = self.remote_browser.full_key_for_name(name, typ)
            if not key or typ == "UP":
                continue

            if typ == "FILE":
                local_path = os.path.join(target_dir, os.path.basename(name))
                if os.path.exists(local_path):
                    action = self.ask_name_conflict("Download to local", local_path)
                    if action == "skip":
                        continue
                    elif action == "copy":
                        local_path = self.make_copy_name(local_path)
                    elif action == "rename":
                        base = os.path.basename(local_path)
                        new_name, ok = QInputDialog.getText(
                            self, "Rename before download", "New file name:", QLineEdit.Normal, base
                        )
                        if not ok or not new_name.strip():
                            continue
                        new_name = new_name.strip()
                        local_path = os.path.join(os.path.dirname(local_path), new_name)
                    # replace: overwrite in place

                try:
                    self.status.showMessage(f"Downloading {key} -> {local_path} ...")
                    self._progress_cb(0)
                    self.s3.download(key, local_path, progress_callback=self._progress_cb)
                    self.reset_progress()
                except Exception as e:
                    self.reset_progress()
                    QMessageBox.critical(self, "Download failed", str(e))
                    return
                continue

            if typ == "DIR":
                dir_key = key.strip("/")
                base_name = os.path.basename(dir_key)
                local_base_dir = os.path.join(target_dir, base_name)
                try:
                    keys = self.s3.list_tree_files(dir_key)
                except Exception as e:
                    QMessageBox.critical(self, "Download failed", str(e))
                    return

                # Create target folder even if it has only nested folders/markers.
                os.makedirs(local_base_dir, exist_ok=True)

                prefix_with_slash = dir_key + "/"
                for child_key in keys:
                    if not child_key or child_key.endswith("/"):
                        continue
                    if child_key.startswith(prefix_with_slash):
                        rel_path = child_key[len(prefix_with_slash):]
                    else:
                        rel_path = os.path.basename(child_key)
                    local_path = os.path.join(local_base_dir, rel_path)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)

                    if os.path.exists(local_path):
                        action = self.ask_name_conflict("Download folder to local", local_path)
                        if action == "skip":
                            continue
                        elif action == "copy":
                            local_path = self.make_copy_name(local_path)
                        elif action == "rename":
                            base = os.path.basename(local_path)
                            new_name, ok = QInputDialog.getText(
                                self, "Rename before download", "New file name:", QLineEdit.Normal, base
                            )
                            if not ok or not new_name.strip():
                                continue
                            new_name = new_name.strip()
                            local_path = os.path.join(os.path.dirname(local_path), new_name)
                        # replace: overwrite in place

                    try:
                        self.status.showMessage(f"Downloading {child_key} -> {local_path} ...")
                        self._progress_cb(0)
                        self.s3.download(child_key, local_path, progress_callback=self._progress_cb)
                        self.reset_progress()
                    except Exception as e:
                        self.reset_progress()
                        QMessageBox.critical(self, "Download failed", str(e))
                        return

        self.status.showMessage("Download completed", 5000)

    def delete_remote(self):
        if self.s3 is None:
            QMessageBox.warning(self, "No config", "Configure RunPod S3 in Settings first.")
            return

        entries = self.remote_browser.selected_entries()
        if not entries:
            QMessageBox.information(self, "Delete", "Select at least one file or folder on the right side.")
            return

        names = ", ".join(n for n, t in entries)
        reply = QMessageBox.question(
            self, "Delete files",
            f"Delete selected entries?\n{names}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for name, typ in entries:
            key = self.remote_browser.full_key_for_name(name, typ)
            if not key:
                continue
            try:
                if typ == "DIR":
                    prefix = key.strip("/")
                    keys = self.s3.list_all_keys(prefix)
                    for child_key in keys:
                        self.s3.delete(child_key)
                    # Delete folder marker if present.
                    self.s3.delete(prefix + "/")
                elif typ == "FILE":
                    self.s3.delete(key)
            except Exception as e:
                QMessageBox.critical(self, "Delete failed", str(e))
                return

        self.status.showMessage("Deleted", 3000)
        self.remote_browser.refresh_async()

    def rename_remote(self):
        if self.s3 is None:
            QMessageBox.warning(self, "No config", "Configure RunPod S3 in Settings first.")
            return

        entries = self.remote_browser.selected_entries()
        if len(entries) != 1 or entries[0][1] != "FILE":
            QMessageBox.information(self, "Rename", "Select a single FILE on the right side to rename.")
            return

        name, typ = entries[0]
        key = self.remote_browser.full_key_for_name(name, typ)
        if not key:
            return

        new_name, ok = QInputDialog.getText(
            self, "Rename file", "New name:", QLineEdit.Normal, name
        )
        if not ok or not new_name.strip():
            return

        new_name = new_name.strip()
        prefix = self.remote_browser.current_prefix.strip("/")
        new_key = f"{prefix}/{new_name}" if prefix else new_name
        new_key = new_key.lstrip("/")

        # If the key already exists, ask for a resolution.
        if self.s3.object_exists(new_key):
            action = self.ask_name_conflict("Rename on RunPod", new_key)
            if action == "skip":
                return
            elif action == "copy":
                new_key = self.make_copy_key(new_key)
            elif action == "rename":
                # Ask again for a new name.
                nn, ok2 = QInputDialog.getText(
                    self, "Rename again", "New file name:", QLineEdit.Normal, new_name
                )
                if not ok2 or not nn.strip():
                    return
                nn = nn.strip()
                new_key = f"{prefix}/{nn}" if prefix else nn
                new_key = new_key.lstrip("/")
            # replace: keep going

        try:
            self.status.showMessage(f"Renaming {key} -> {new_key} ...")
            self.s3.rename(key, new_key)
            self.status.showMessage("Rename completed", 4000)
            self.remote_browser.refresh_async()
        except Exception as e:
            QMessageBox.critical(self, "Rename failed", str(e))


# ---------- MAIN ----------

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
