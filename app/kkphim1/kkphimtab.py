"""kkphim1 tab built on top of the M3U8 Pro yt-dlp download flow."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests
from PySide6 import QtCore, QtGui, QtWidgets

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
except Exception:
    QAudioOutput = None
    QMediaPlayer = None
    QVideoWidget = None

from m3u8.m3utab import (
    DEFAULT_CONCURRENCY,
    _APP_NAME,
    _dark_btn,
    _dark_input,
    _sanitize_filename,
    M3U8DownloadWorker,
    M3U8Item,
    M3U8ProTab,
    YtDlpM3U8DownloadWorker,
)

_KKPHIM_CONFIG_KEY = "kkphim1"


def _kkphim_slug_from_input(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[-1].lower() in {"phim", "xem-phim"}:
            return ""
        return parts[-1].strip() if parts else ""

    if "/" in text:
        parsed_path = urlparse(text).path
        parts = [part for part in parsed_path.split("/") if part]
        if parts and parts[-1].lower() in {"phim", "xem-phim"}:
            return ""
        return parts[-1].strip() if parts else ""

    return text.strip().strip("/")


def _ascii_token(text: str, join_words: bool = False) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("đ", "d").replace("Đ", "D")
    words = re.findall(r"[A-Za-z0-9]+", text)
    if not words:
        return ""
    if join_words:
        return "".join(w[:1].upper() + w[1:] for w in words)
    return "_".join(words)


def _episode_output_name(server_name: str, episode_name: str) -> str:
    ep = _ascii_token(episode_name) or "Tap"
    clean_server = re.sub(r"^[#\s]+", "", server_name or "")
    parts = re.findall(r"[^\(\)]+", clean_server)
    server = "_".join(
        token for token in (_ascii_token(part, join_words=True) for part in parts) if token
    )
    return f"{ep}_{server}" if server else ep


class KkPhimFetchWorker(QtCore.QThread):
    finished_ok = QtCore.Signal(dict)
    failed = QtCore.Signal(str)

    def __init__(self, slug: str):
        super().__init__()
        self.slug = _kkphim_slug_from_input(slug)

    def run(self):
        if not self.slug:
            self.failed.emit("Vui lòng nhập slug phim.")
            return
        try:
            response = requests.get(f"https://phimapi.com/phim/{self.slug}", timeout=20)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            self.failed.emit(f"Không fetch được API: {e}")
            return
        if not data.get("status"):
            self.failed.emit(str(data.get("msg") or "API trả về status=false"))
            return
        self.finished_ok.emit(data)


class KkPhimPreviewDialog(QtWidgets.QDialog):
    """Small video preview dialog for an episode M3U8 URL."""

    def __init__(self, title: str, url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Preview - {title}")
        self.setMinimumSize(860, 520)
        self._player = None
        self._audio = None
        self._seeking = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        lbl = QtWidgets.QLabel(url)
        lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setStyleSheet("color: #6b7280; font-size: 11px;")
        lay.addWidget(lbl)

        if QMediaPlayer is None or QVideoWidget is None or QAudioOutput is None:
            msg = QtWidgets.QLabel(
                "Máy hiện không có QtMultimedia để preview trong app.\n"
                "Bấm Mở ngoài để xem bằng player/trình duyệt mặc định."
            )
            msg.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            msg.setStyleSheet("color: #374151; background: #f3f4f6; padding: 24px;")
            lay.addWidget(msg, stretch=1)
        else:
            video = QVideoWidget()
            video.setStyleSheet("background: #000000;")
            lay.addWidget(video, stretch=1)

            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._audio.setVolume(0.8)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(video)
            self._player.setSource(QtCore.QUrl(url))
            self._player.errorOccurred.connect(self._on_player_error)
            self._player.positionChanged.connect(self._on_position_changed)
            self._player.durationChanged.connect(self._on_duration_changed)

        seek_lay = QtWidgets.QHBoxLayout()
        self._lbl_time = QtWidgets.QLabel("00:00 / 00:00")
        self._lbl_time.setFixedWidth(100)
        self._lbl_time.setStyleSheet("color: #374151; font-size: 11px;")
        self._seek = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._seek.setRange(0, 0)
        self._seek.sliderPressed.connect(self._on_seek_pressed)
        self._seek.sliderReleased.connect(self._on_seek_released)
        self._seek.sliderMoved.connect(self._on_seek_moved)
        self._seek.setEnabled(self._player is not None)
        seek_lay.addWidget(self._lbl_time)
        seek_lay.addWidget(self._seek, stretch=1)
        lay.addLayout(seek_lay)

        controls = QtWidgets.QHBoxLayout()
        self._btn_play = QtWidgets.QPushButton("Play")
        self._btn_pause = QtWidgets.QPushButton("Pause")
        btn_open = QtWidgets.QPushButton("Mở ngoài")
        btn_close = QtWidgets.QPushButton("Đóng")

        self._btn_play.setStyleSheet(_dark_btn("#16a34a", "#15803d", padding="5px 14px"))
        self._btn_pause.setStyleSheet(_dark_btn("#6b7280", "#4b5563", padding="5px 14px"))
        btn_open.setStyleSheet(_dark_btn("#2563eb", "#1d4ed8", padding="5px 14px"))
        btn_close.setStyleSheet(_dark_btn("#dc2626", "#b91c1c", padding="5px 14px"))

        self._btn_play.clicked.connect(self._play)
        self._btn_pause.clicked.connect(self._pause)
        btn_open.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)))
        btn_close.clicked.connect(self.close)

        if self._player is None:
            self._btn_play.setEnabled(False)
            self._btn_pause.setEnabled(False)

        controls.addWidget(self._btn_play)
        controls.addWidget(self._btn_pause)
        controls.addWidget(btn_open)
        controls.addStretch(1)
        controls.addWidget(btn_close)
        lay.addLayout(controls)

        QtCore.QTimer.singleShot(100, self._play)

    def _play(self):
        if self._player is not None:
            self._player.play()

    def _pause(self):
        if self._player is not None:
            self._player.pause()

    @staticmethod
    def _fmt_ms(value: int) -> str:
        seconds = max(0, int(value // 1000))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _update_time_label(self, position: int | None = None):
        if self._player is None:
            return
        pos = self._player.position() if position is None else position
        self._lbl_time.setText(f"{self._fmt_ms(pos)} / {self._fmt_ms(self._player.duration())}")

    def _on_duration_changed(self, duration: int):
        self._seek.setRange(0, max(0, duration))
        self._seek.setEnabled(duration > 0)
        self._update_time_label()

    def _on_position_changed(self, position: int):
        if not self._seeking:
            self._seek.setValue(position)
        self._update_time_label(position)

    def _on_seek_pressed(self):
        self._seeking = True

    def _on_seek_moved(self, position: int):
        self._update_time_label(position)

    def _on_seek_released(self):
        if self._player is not None:
            self._player.setPosition(self._seek.value())
        self._seeking = False

    def _on_player_error(self, error, error_string: str):
        if error_string:
            QtWidgets.QMessageBox.warning(self, "Preview lỗi", error_string)

    def closeEvent(self, event):
        if self._player is not None:
            self._player.stop()
        event.accept()


class KkPhimEpisodeDialog(QtWidgets.QDialog):
    def __init__(self, movie_name: str, episodes: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn tập")
        self.setMinimumSize(620, 620)
        self._checks: list[QtWidgets.QCheckBox] = []

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QtWidgets.QLabel(f"<b>{movie_name}</b>")
        title.setStyleSheet("font-size: 15px; color: #111827;")
        lay.addWidget(title)

        tools = QtWidgets.QHBoxLayout()
        btn_all = QtWidgets.QPushButton("Chọn tất cả")
        btn_none = QtWidgets.QPushButton("Bỏ chọn tất cả")
        btn_all.setStyleSheet(_dark_btn("#2563eb", "#1d4ed8", padding="5px 12px"))
        btn_none.setStyleSheet(_dark_btn("#6b7280", "#4b5563", padding="5px 12px"))
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        tools.addWidget(btn_all)
        tools.addWidget(btn_none)
        tools.addStretch(1)
        lay.addLayout(tools)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        body_lay = QtWidgets.QVBoxLayout(body)
        body_lay.setContentsMargins(4, 4, 4, 4)
        body_lay.setSpacing(8)

        for group in episodes:
            server_name = str(group.get("server_name") or "").strip()
            server_data = group.get("server_data") or []
            if server_name:
                lbl = QtWidgets.QLabel(server_name)
                lbl.setStyleSheet("font-weight: bold; color: #1f2937; padding-top: 6px;")
                body_lay.addWidget(lbl)

            for ep in server_data:
                ep_name = str(ep.get("name") or ep.get("slug") or "Tập").strip()
                url = str(ep.get("link_m3u8") or ep.get("link_embed") or "").strip()
                if not url:
                    continue
                cb = QtWidgets.QCheckBox(ep_name)
                cb.setChecked(True)
                cb.setStyleSheet("padding-left: 18px; color: #111827;")
                cb.setProperty("episode", ep)
                cb.setProperty("server_name", server_name)
                cb.setToolTip(url)
                self._checks.append(cb)

                row = QtWidgets.QWidget()
                row_lay = QtWidgets.QHBoxLayout(row)
                row_lay.setContentsMargins(0, 0, 0, 0)
                row_lay.setSpacing(6)
                row_lay.addWidget(cb, stretch=1)

                btn_preview = QtWidgets.QPushButton("Preview")
                btn_preview.setFixedWidth(78)
                btn_preview.setToolTip("Xem thử tập này")
                btn_preview.setStyleSheet(_dark_btn("#0f766e", "#0d9488", padding="4px 8px"))
                btn_preview.clicked.connect(
                    lambda *_, title=ep_name, media_url=url: self._show_preview(title, media_url)
                )
                row_lay.addWidget(btn_preview)
                body_lay.addWidget(row)

        body_lay.addStretch(1)
        scroll.setWidget(body)
        lay.addWidget(scroll, stretch=1)

        btn_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btn_ok = btn_box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        btn_cancel = btn_box.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        btn_ok.setText("Add xác nhận")
        btn_cancel.setText("Hủy")
        btn_ok.setStyleSheet(_dark_btn("#16a34a", "#15803d", padding="6px 16px"))
        btn_cancel.setStyleSheet(_dark_btn("#dc2626", "#b91c1c", padding="6px 16px"))
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        lay.addWidget(btn_box)

    def _set_all(self, checked: bool):
        for cb in self._checks:
            cb.setChecked(checked)

    def _show_preview(self, title: str, url: str):
        media_url = M3U8DownloadWorker.normalize_media_url(url)
        dlg = KkPhimPreviewDialog(title, media_url, self)
        dlg.exec()

    def selected(self) -> list[tuple[str, dict]]:
        result: list[tuple[str, dict]] = []
        for cb in self._checks:
            if cb.isChecked():
                result.append((str(cb.property("server_name") or ""), cb.property("episode")))
        return result


class KkPhimTab(M3U8ProTab):
    """Fetch kkphim1 episodes from phimapi and download with yt-dlp."""

    def __init__(self):
        self._fetch_worker: KkPhimFetchWorker | None = None
        self._current_movie_name = ""
        super().__init__()

    def settings(self) -> QtCore.QSettings:
        return QtCore.QSettings(_APP_NAME, _KKPHIM_CONFIG_KEY)

    def _build_add_bar(self) -> QtWidgets.QWidget:
        grp = QtWidgets.QGroupBox("kkphim1")
        lay = QtWidgets.QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        lay.addWidget(QtWidgets.QLabel("Slug phim:"))
        self._kk_slug = QtWidgets.QLineEdit()
        self._kk_slug.setPlaceholderText("ngoi-truong-xac-song hoặc https://kkphim1.com/phim/...")
        self._kk_slug.setStyleSheet(_dark_input())
        self._kk_slug.returnPressed.connect(self._on_fetch_clicked)
        lay.addWidget(self._kk_slug, stretch=1)

        self._btn_fetch = QtWidgets.QPushButton("Fetch")
        self._btn_fetch.setStyleSheet(_dark_btn("#2563eb", "#1d4ed8", padding="5px 16px"))
        self._btn_fetch.clicked.connect(self._on_fetch_clicked)
        lay.addWidget(self._btn_fetch)
        return grp

    def _load_settings(self):
        s = self.settings()
        self._cfg_save_dir.setText(str(s.value("save_dir", "") or ""))
        self._cfg_concurrency.setValue(int(s.value("concurrency", DEFAULT_CONCURRENCY)))
        self._cfg_fragments.setCurrentText(str(s.value("fragments", "4")))
        mode = str(s.value("container_mode", "mp4"))
        idx = self._cfg_container.findData(mode)
        self._cfg_container.setCurrentIndex(idx if idx >= 0 else 0)
        self._kk_slug.setText(s.value("slug", ""))

    def _save_settings(self):
        super()._save_settings()
        if hasattr(self, "_kk_slug"):
            self.settings().setValue("slug", _kkphim_slug_from_input(self._kk_slug.text()))

    def _sync_save_dir_lock(self):
        locked = bool(self.items)
        self._cfg_save_dir.setReadOnly(locked)
        if hasattr(self, "_btn_browse_save_dir"):
            self._btn_browse_save_dir.setEnabled(not locked)
        self._cfg_save_dir.setToolTip(
            "Đang có tập trong danh sách. Xóa/reset danh sách để đổi thư mục lưu."
            if locked else "Chọn thư mục lưu video."
        )
        if hasattr(self, "_btn_browse_save_dir"):
            self._btn_browse_save_dir.setToolTip(
                "Xóa/reset danh sách để đổi thư mục lưu."
                if locked else "Chọn thư mục"
            )

    def _can_change_save_dir(self) -> bool:
        return not bool(self.items)

    def _update_action_buttons(self):
        super()._update_action_buttons()
        if hasattr(self, "_cfg_save_dir"):
            self._sync_save_dir_lock()

    def _on_fetch_clicked(self):
        slug = _kkphim_slug_from_input(self._kk_slug.text())
        if not slug:
            QtWidgets.QMessageBox.information(self, "Thiếu slug", "Vui lòng nhập slug phim.")
            return
        save_dir = self._validated_save_dir()
        if save_dir is None:
            QtWidgets.QMessageBox.information(self, "Thiếu thư mục", "Vui lòng chọn thư mục lưu trước.")
            return
        if self._fetch_worker and self._fetch_worker.isRunning():
            return
        self._save_settings()
        self._kk_slug.setText(slug)
        self._btn_fetch.setEnabled(False)
        self._btn_fetch.setText("Đang fetch...")
        self._log(f"Fetch phimapi: {slug}")
        self._fetch_worker = KkPhimFetchWorker(slug)
        self._fetch_worker.finished_ok.connect(self._on_fetch_ok)
        self._fetch_worker.failed.connect(self._on_fetch_failed)
        self._fetch_worker.finished.connect(self._on_fetch_done)
        self._fetch_worker.start()
        self._update_action_buttons()

    def _on_fetch_done(self):
        self._btn_fetch.setEnabled(True)
        self._btn_fetch.setText("Fetch")
        self._update_action_buttons()

    def _on_fetch_failed(self, msg: str):
        self._log(f"Fetch lỗi: {msg}")
        QtWidgets.QMessageBox.warning(self, "Fetch lỗi", msg)

    def _on_fetch_ok(self, data: dict):
        movie = data.get("movie") or {}
        movie_name = str(movie.get("name") or movie.get("slug") or self._kk_slug.text().strip())
        episodes = data.get("episodes") or []
        total = sum(len(group.get("server_data") or []) for group in episodes)
        if not total:
            QtWidgets.QMessageBox.information(self, "Không có tập", "API không trả về tập có link tải.")
            return

        dlg = KkPhimEpisodeDialog(movie_name, episodes, self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        selected = dlg.selected()
        if not selected:
            self._log("Không có tập nào được chọn.")
            return
        added = self._add_selected_episodes(movie_name, selected)
        self._current_movie_name = movie_name
        self._log(f"Đã thêm {added} tập từ phim: {movie_name}")

    def _movie_save_dir(self, movie_name: str) -> Path | None:
        root = self._validated_save_dir(create=False)
        if root is None:
            return None
        return root / _sanitize_filename(movie_name)

    def _unique_queue_name(self, base_name: str, save_dir: Path) -> str:
        existing = {it.name.strip().lower() for it in self.items}
        ext = ".ts" if str(self._cfg_container.currentData()) == "ts" else ".mp4"
        candidate = base_name
        n = 1
        while candidate.strip().lower() in existing or (save_dir / f"{candidate}{ext}").exists():
            candidate = f"{base_name} ({n})"
            n += 1
        return candidate

    def _add_selected_episodes(self, movie_name: str, selected: list[tuple[str, dict]]) -> int:
        movie_dir = self._movie_save_dir(movie_name)
        if movie_dir is None:
            return 0
        added = 0
        for server_name, ep in selected:
            url = str(ep.get("link_m3u8") or ep.get("link_embed") or "").strip()
            url = M3U8DownloadWorker.normalize_media_url(url)
            if not url:
                continue
            ep_name = str(ep.get("name") or ep.get("slug") or "Tập").strip()
            base_name = _episode_output_name(server_name, ep_name)
            name = self._unique_queue_name(base_name, movie_dir)
            item = M3U8Item(
                id=self._next_id,
                url=url,
                name=name,
                save_dir=movie_dir,
                fmt="m3u8",
                status="pending",
            )
            self._next_id += 1
            self.items.append(item)
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._row_for_id[item.id] = row
            self._fill_row(row, item)
            added += 1
        self._update_count_label()
        self._save_settings()
        return added

    def _start_item(self, item: M3U8Item):
        if item.status == "downloading":
            return
        save_dir = item.save_dir or self._movie_save_dir(self._current_movie_name or "kkphim1")
        if save_dir is None:
            return
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self,
                "Thư mục không hợp lệ",
                f"Không thể tạo hoặc truy cập thư mục lưu:\n{save_dir}\n\n{e}",
            )
            return

        item.status = "downloading"
        item.progress = 0.0
        item.speed = ""
        item.eta = ""
        item.error_msg = ""
        item.save_dir = save_dir
        self._fill_row(self._row_for_id[item.id], item)
        self._update_action_buttons()

        worker = YtDlpM3U8DownloadWorker(
            url=item.url,
            save_dir=item.save_dir,
            name=item.name,
            fmt=item.fmt,
            fragments=int(self._cfg_fragments.currentText()),
            container_mode=str(self._cfg_container.currentData()),
        )
        item.instance_id = worker.instance_id
        self.workers[item.id] = worker
        worker.log_msg.connect(self._on_worker_log)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.output_ready.connect(self._on_worker_output_ready)
        worker.start()
        self._update_action_buttons()
        self._log(
            f"[{item.name}] Bắt đầu tải kkphim1 bằng yt-dlp -N {self._cfg_fragments.currentText()} "
            f"({self._cfg_container.currentText()})..."
        )
