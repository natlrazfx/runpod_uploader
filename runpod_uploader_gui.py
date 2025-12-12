import os
import sys
import webbrowser
from dataclasses import dataclass

from dotenv import load_dotenv
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from PySide6.QtCore import Qt, QDir
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QWidget, QVBoxLayout, QTreeView,
    QFileSystemModel, QToolBar, QFileDialog, QInputDialog, QLineEdit,
    QMessageBox, QDialog, QFormLayout, QDialogButtonBox, QLineEdit as QLE,
    QLabel, QStatusBar, QProgressBar, QPushButton, QSizePolicy,
    QAbstractItemView, QTableWidget, QTableWidgetItem, QComboBox, QHBoxLayout
)

ENV_FILE = ".env"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # где лежит скрипт
ENV_FILE = os.path.join(BASE_DIR, ".env")


# ---------- CONFIG ----------

@dataclass
class S3Config:
    access_key: str
    secret_key: str
    bucket: str
    endpoint: str
    region: str
    local_root: str     # какая папка открывается слева по умолчанию


def load_config() -> S3Config:
    # читаем .env рядом со скриптом, даже если VS Code запускает из другой директории
    load_dotenv(ENV_FILE, override=False)

    return S3Config(
        access_key=os.getenv("RUNPOD_S3_ACCESS_KEY", "").strip(),
        secret_key=os.getenv("RUNPOD_S3_SECRET_KEY", "").strip(),
        bucket=os.getenv("RUNPOD_BUCKET", "").strip(),
        endpoint=os.getenv("RUNPOD_ENDPOINT", "https://s3api-eu-cz-1.runpod.io").strip(),
        region=os.getenv("RUNPOD_REGION", "eu-cz-1").strip(),
        local_root=os.getenv("LOCAL_ROOT", "").strip().strip('"').strip("'"),
    )



def save_config(cfg: S3Config):
    lines = [
        f"RUNPOD_S3_ACCESS_KEY={cfg.access_key}",
        f"RUNPOD_S3_SECRET_KEY={cfg.secret_key}",
        f"RUNPOD_BUCKET={cfg.bucket}",
        f"RUNPOD_ENDPOINT={cfg.endpoint}",
        f"RUNPOD_REGION={cfg.region}",
        f"LOCAL_ROOT={cfg.local_root}",
    ]
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


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

    def list_prefix(self, prefix: str):
        """Возвращает (dirs, files) для заданного префикса."""
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        paginator = self.client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self.cfg.bucket,
            Prefix=prefix,
            Delimiter="/",
        )

        dirs = set()
        files = []

        for page in pages:
            # --- 1. папки, возвращённые через CommonPrefixes
            for cp in page.get("CommonPrefixes", []):
                name = cp["Prefix"][len(prefix):].strip("/")
                if name:
                    dirs.add(name + "/")

            # --- 2. объекты (файлы + папки-маркеры)
        for obj in page.get("Contents", []):
            key = obj["Key"]

            # пропускаем сам корневой префикс
            if key == prefix:
                continue

            name = key[len(prefix):]
            if not name:
                continue

            # --- ВАЖНО! "folder/" = папка, а не файл ---
            if name.endswith("/"):
                dirs.add(name)
                continue  # НЕ добавлять в files

            # если вдруг API вернул вложенный путь без delimiter, считаем его папкой
            if "/" in name:
                first = name.split("/", 1)[0].strip()
                if first:
                    dirs.add(first + "/")
                continue

            # runpod иногда возвращает пустой объект без слеша как «папку»
            if obj.get("Size", 0) == 0 and "/" not in name and "." not in name:
                dirs.add(name + "/")
                continue

            # обычный файл
            files.append({
                "Key": key,
                "Name": name,
                "Size": obj.get("Size", 0),
                "LastModified": obj.get("LastModified"),
            })

        return sorted(dirs), files



    def object_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.cfg.bucket, Key=key)
            return True
        except self.client.exceptions.NoSuchKey:
            return False
        except Exception:
            # 404/403 и т.п. – считаем, что нет
            return False

    def upload(self, local_path: str, key: str, progress_callback=None):
        if not key:
            raise ValueError("Empty key")
        filesize = os.path.getsize(local_path) or 1
        tracker = ProgressTracker(filesize, progress_callback) if progress_callback else None
        part_mb = max(self._int_env("RUNPOD_PART_SIZE_MB", 64), 8)  # larger parts => fewer parts to finalize
        part_size = part_mb * 1024 * 1024
        max_conc = max(self._int_env("RUNPOD_MAX_CONCURRENCY", 4), 1)
        config = TransferConfig(
            multipart_chunksize=part_size,
            multipart_threshold=part_size,
            max_concurrency=max_conc,
        )
        self.client.upload_file(
            Filename=local_path,
            Bucket=self.cfg.bucket,
            Key=key,
            Callback=tracker,
            Config=config,
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

    def rename(self, old_key: str, new_key: str):
        self.client.copy_object(
            Bucket=self.cfg.bucket,
            CopySource={"Bucket": self.cfg.bucket, "Key": old_key},
            Key=new_key,
        )
        self.client.delete_object(Bucket=self.cfg.bucket, Key=old_key)

    # <<< NEW: создание "папки" (префикса) в S3
    def create_folder(self, key: str):
        """
        Создает пустой объект с ключом, оканчивающимся на '/',
        чтобы он отображался как папка.
        """
        if not key.endswith("/"):
            key = key + "/"
        self.client.put_object(Bucket=self.cfg.bucket, Key=key)


class ProgressTracker:
    """Коллбек для boto3, обновляет прогрессбар в процентах."""

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


# ---------- REMOTE BROWSER (TABLE) ----------

class RemoteBrowser(QWidget):
    """Правая панель: список объектов в RunPod storage."""

    def __init__(self, s3: RunPodS3 | None, parent=None):
        super().__init__(parent)
        self.s3 = s3
        self.current_prefix = ""  # например 'ComfyUI/models/vae'

        layout = QVBoxLayout(self)
        # небольшой зазор от левого края, чтобы не прилипал к splitter
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

    def refresh(self):
        if self.s3 is None:
            return

        # если по текущему префиксу лежит файл, предложим конвертировать его в папку
        raw_prefix = self.current_prefix.strip("/")
        if raw_prefix and self.s3.object_exists(raw_prefix):
            msg = QMessageBox.question(
                self,
                "Path is a file",
                f"'{raw_prefix}' Convert it to the folder??\n",
                QMessageBox.Yes | QMessageBox.No,
            )
            if msg == QMessageBox.Yes:
                try:
                    self.s3.delete(raw_prefix)
                    self.s3.create_folder(raw_prefix + "/")
                except Exception as e:
                    QMessageBox.critical(self, "Convert failed", str(e))
                    return
            else:
                self.current_prefix = "/".join(p for p in raw_prefix.split("/")[:-1] if p)
                return self.refresh()

        dirs, files = self.s3.list_prefix(self.current_prefix)

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
        # сортировка по Name A-Z
        self.table.sortItems(0, Qt.AscendingOrder)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)

    def selected_entries(self):
        """Возвращает список (name, type) для выделенных строк."""
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
                self.refresh()
        elif typ == "DIR":
            new_prefix = self.current_prefix + "/" + name.strip("/") if self.current_prefix else name.strip("/")
            self.current_prefix = new_prefix
            self.refresh()


# ---------- MAIN WINDOW ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RunPod Uploader")
        self.resize(1200, 700)

        self.cfg = load_config()
        self.s3 = None
        if self.cfg.access_key and self.cfg.secret_key and self.cfg.bucket:
            self.s3 = RunPodS3(self.cfg)

        self.drive_combo: QComboBox | None = None

        splitter = QSplitter()

        # ----- левая панель: локальный компьютер -----
        self.local_model = QFileSystemModel()

        root_path = self._resolve_local_root(self.cfg.local_root)


        self.local_model.setRootPath(root_path)

        self.local_view = QTreeView()
        self.local_view.setModel(self.local_model)
        self.local_view.setRootIndex(self.local_model.index(root_path))
        self.local_view.setSortingEnabled(True)
        self.local_view.sortByColumn(0, Qt.AscendingOrder)
        self.local_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.local_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_view.setEditTriggers(QTreeView.NoEditTriggers)

        for col in range(1, 4):
            self.local_view.resizeColumnToContents(col)
        self.local_view.setColumnWidth(0, 260)

        local_container = QWidget()
        local_layout = QVBoxLayout(local_container)
        # небольшой правый margin чтобы отделить от правой панели
        local_layout.setContentsMargins(4, 0, 2, 0)

        local_label = QLabel("Local storage")
        local_label.setStyleSheet("color:#f0f0f0; font-weight:bold;")
        local_label.setContentsMargins(6, 4, 0, 4)

        local_layout.addWidget(local_label)
        local_layout.addWidget(self.local_view)

        # ----- правая панель: RunPod storage -----
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

        # статус бар + прогресс
        self.status = QStatusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)
        self.setStatusBar(self.status)

        self.init_toolbar()
        self.apply_dark_style()

        # выставляем диск в комбобоксе согласно root_path
        self.init_drive_combo(root_path)

        if not (self.cfg.access_key and self.cfg.secret_key and self.cfg.bucket):
            self.edit_settings()
        else:
            self.remote_browser.refresh()

    def _resolve_local_root(self, path: str) -> str:
        """
        Приводим LOCAL_ROOT к рабочему виду:
        - разворачиваем ~
        - нормализуем слеши
        - если не существует, пробуем корень диска (D:/)
        - если вообще ничего, берём QDir.rootPath()
        """
        if not path:
            return QDir.rootPath()

        path = os.path.expanduser(path)
        path = os.path.normpath(path)

        if os.path.isdir(path):
            return path

        # если путь типа D:\что-то, а папки нет – хотя бы диск открыть
        drive, _ = os.path.splitdrive(path)
        if drive:
            drive_root = drive + os.path.sep
            if os.path.isdir(drive_root):
                return drive_root

        return QDir.rootPath()

    # ----- стиль -----
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
            QPushButton#DonateButton {
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

    # ----- тулбар -----
    def init_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        # Settings
        act_settings = QAction("Settings", self)
        act_settings.triggered.connect(self.edit_settings)
        tb.addAction(act_settings)

        # Drive selector сразу после Settings
        tb.addSeparator()
        tb.addWidget(QLabel("Drive:"))
        self.drive_combo = QComboBox()
        tb.addWidget(self.drive_combo)
        self.drive_combo.currentIndexChanged.connect(self.change_drive)

        # Upload (левая -> правая) рядом с Drive
        tb.addSeparator()
        act_upload = QAction("Upload →", self)
        act_upload.triggered.connect(self.upload_from_local)
        tb.addAction(act_upload)

        # <<< NEW: блок для центральной ссылки RunPod
        spacer_left = QWidget()
        spacer_left.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer_left)

        runpod_label = QLabel(
            '<a style="color:#a6c8ff; font-weight:bold;" '
            'href="https://console.runpod.io/deploy">RunPod Console</a>'
        )
        runpod_label.setOpenExternalLinks(True)
        tb.addWidget(runpod_label)

        spacer_right = QWidget()
        spacer_right.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer_right)

        # Правый блок: операции над RunPod (правой панелью)
        act_download = QAction("← Download", self)
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

        # подпись Natalia Raz | GitHub | Vimeo рядом с Donate
        info_label = QLabel(
            '<span style="font-size:10px; color:#cccccc;">'
            '<a style="color:#a6c8ff;" href="https://www.linkedin.com/in/natalia-raz-0b8329120/">Natalia Raz</a> &nbsp;|&nbsp; '
            '<a style="color:#a6c8ff;" href="https://github.com/natlrazfx">GitHub</a> &nbsp;|&nbsp; '
            '<a style="color:#a6c8ff;" href="https://vimeo.com/552106671">Vimeo</a>'
            '</span>'
        )
        info_label.setOpenExternalLinks(True)
        tb.addWidget(info_label)

        donate_btn = QPushButton("Donate")
        donate_btn.setObjectName("DonateButton")
        donate_btn.clicked.connect(lambda: webbrowser.open(DONATE_URL))
        tb.addWidget(donate_btn)

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
        dlg.setStyleSheet(self.styleSheet())  # тёмный стиль
        if dlg.exec() == QDialog.Accepted:
            updated_cfg = dlg.cfg
            updated_cfg.local_root = updated_cfg.local_root.strip().strip('"').strip("'")
            self.cfg = updated_cfg
            save_config(self.cfg)
            try:
                self.s3 = RunPodS3(self.cfg)
                self.remote_browser.s3 = self.s3
                # обновляем левый корень
                root_path = self._resolve_local_root(self.cfg.local_root)
                self.local_model.setRootPath(root_path)
                self.local_view.setRootIndex(self.local_model.index(root_path))
                self.init_drive_combo(root_path)
                self.remote_browser.current_prefix = ""
                self.remote_browser.refresh()
                self.status.showMessage("Settings updated", 4000)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to init S3 client:\n{e}")

    def remote_refresh(self):
        try:
            self.remote_browser.refresh()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to list storage:\n{e}")

    def change_drive(self, idx: int):
        if idx < 0 or self.drive_combo is None:
            return
        path = self.drive_combo.itemText(idx)
        if not path:
            return
        self.local_model.setRootPath(path)
        self.local_view.setRootIndex(self.local_model.index(path))

    def _progress_cb(self, percent: int):
        self.progress.setVisible(True)
        self.progress.setValue(percent)
        QApplication.processEvents()

    def reset_progress(self):
        self.progress.setVisible(False)
        self.progress.setValue(0)

    def current_local_dir(self) -> str:
        """Куда сохранять скачанные файлы."""
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
        """Список выбранных файлов слева (мультивыбор)."""
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

    # ----- диалог конфликтов имён -----

    def ask_name_conflict(self, context: str, path: str) -> str:
        """
        Возвращает одно из:
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
        Убеждаемся, что каждый уровень prefix не занят файлом.
        Если файл встречается, предлагаем удалить и заменить его на папку.
        """
        if not prefix:
            return True

        parts = [p for p in prefix.strip("/").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            # если существует как файл (без слеша) – конфликт
            if self.s3.object_exists(current):
                res = QMessageBox.question(
                    self,
                    "Path is a file",
                    f"'{current}' существует как файл. Заменить на папку?\n"
                    f"Файл будет удалён и заменён пустой папкой.",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if res != QMessageBox.Yes:
                    return False
                try:
                    self.s3.delete(current)
                except Exception as e:
                    QMessageBox.critical(self, "Delete failed", str(e))
                    return False
            # пытаемся создать папку (если уже есть - S3 проигнорирует)
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

            # проверка конфликта
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
                # replace – просто перезатираем

            try:
                self.status.showMessage(f"Uploading {fname} → {key} ...")
                self._progress_cb(0)
                self.s3.upload(local_path, key, progress_callback=self._progress_cb)
                self.reset_progress()
            except Exception as e:
                self.reset_progress()
                QMessageBox.critical(self, "Upload failed", str(e))
                return

        self.status.showMessage("Upload completed", 4000)
        self.remote_browser.refresh()

    def download_to_local(self):
        if self.s3 is None:
            QMessageBox.warning(self, "No config", "Configure RunPod S3 in Settings first.")
            return

        entries = self.remote_browser.selected_entries()
        if not entries:
            QMessageBox.information(self, "Remote file", "Select one or more FILEs on the right side.")
            return

        target_dir = self.current_local_dir()

        for name, typ in entries:
            if typ != "FILE":
                continue
            key = self.remote_browser.full_key_for_name(name, typ)
            if not key:
                continue
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
                # replace – просто скачиваем поверх

            try:
                self.status.showMessage(f"Downloading {key} → {local_path} ...")
                self._progress_cb(0)
                self.s3.download(key, local_path, progress_callback=self._progress_cb)
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
            QMessageBox.information(self, "Delete", "Select at least one file on the right side.")
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
                self.s3.delete(key)
            except Exception as e:
                QMessageBox.critical(self, "Delete failed", str(e))
                return

        self.status.showMessage("Deleted", 3000)
        self.remote_browser.refresh()

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

        # если такой key уже есть – спрашиваем
        if self.s3.object_exists(new_key):
            action = self.ask_name_conflict("Rename on RunPod", new_key)
            if action == "skip":
                return
            elif action == "copy":
                new_key = self.make_copy_key(new_key)
            elif action == "rename":
                # ещё одно окно для имени
                nn, ok2 = QInputDialog.getText(
                    self, "Rename again", "New file name:", QLineEdit.Normal, new_name
                )
                if not ok2 or not nn.strip():
                    return
                nn = nn.strip()
                new_key = f"{prefix}/{nn}" if prefix else nn
                new_key = new_key.lstrip("/")
            # replace – просто продолжаем

        try:
            self.status.showMessage(f"Renaming {key} → {new_key} ...")
            self.s3.rename(key, new_key)
            self.status.showMessage("Rename completed", 4000)
            self.remote_browser.refresh()
        except Exception as e:
            QMessageBox.critical(self, "Rename failed", str(e))


# ---------- MAIN ----------

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
