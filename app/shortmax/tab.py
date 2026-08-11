"""ShortMax downloader tab.

Fetches episode data from Supabase `shortmax_crawl` table.
ShortMax videos already have embedded subtitles — no sub download or FFmpeg merge needed.

Episode format from raw.episodes:
    {
        "episode": 1,
        "eid": "...",
        "urlVideo": "https://...m3u8",
        "source": "api.decoded",
        "headers": {},
        "isLock": false,
        "has_play_url": true
    }
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import requests
from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import QSettings

from xemshort.models import XSEpisode, XSMovie
from xemshort.sync_movies_supabase import (
    DEFAULT_SUPABASE_KEY,
    DEFAULT_SUPABASE_URL,
    load_env_file,
)
from xemshort.tab import XemShortTab
from xemshort.workers import DramaWaveDownloadMergeWorker


_SM_APP_NAME = "XemShort GUI"
_SM_CONFIG_KEY = "ShortMax"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_TABLE = "shortmax_crawl"
_API_URL = "DB:shortmax_crawl.raw.episodes"


def _parse_shortmax_episodes(raw_episodes: list, movie_name: str) -> list[XSEpisode]:
    """Convert ShortMax raw.episodes list to XSEpisode list."""
    episodes: list[XSEpisode] = []
    for item in raw_episodes:
        if not isinstance(item, dict):
            continue
        ep_num = int(item.get("episode") or 0)
        eid = str(item.get("eid") or f"ep{ep_num}")
        url_video = str(item.get("urlVideo") or "").strip()
        is_lock = bool(item.get("isLock", False))
        has_play = bool(item.get("has_play_url", bool(url_video)))
        episodes.append(
            XSEpisode(
                id=eid,
                name=f"Tập {ep_num}",
                episode=ep_num,
                play=url_video,
                subtitle_url=None,
                is_locked=is_lock or not has_play or not url_video,
            )
        )
    episodes.sort(key=lambda e: e.episode)
    return episodes


class ShortMaxFetchWorker(QtCore.QThread):
    """Fetch ShortMax episodes from Supabase shortmax_crawl.raw.episodes."""

    success = QtCore.Signal(list, str, str, int)
    error = QtCore.Signal(str, int)
    log_msg = QtCore.Signal(str, int)

    def __init__(self, movie_id: str):
        super().__init__()
        self.movie_id = movie_id
        self.instance_id: int = uuid.uuid4().int & 0x7FFFFFFF

    def run(self) -> None:
        load_env_file(_ENV_PATH)
        supabase_url = os.environ.get("SUPABASE_URL", "").strip() or DEFAULT_SUPABASE_URL
        supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or DEFAULT_SUPABASE_KEY
        if not supabase_url or not supabase_key:
            self.error.emit(
                "SUPABASE_URL / SUPABASE_KEY chưa cấu hình trong .env",
                self.instance_id,
            )
            return

        try:
            endpoint = supabase_url.rstrip("/") + f"/rest/v1/{_TABLE}"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            response = requests.get(
                endpoint,
                headers=headers,
                params={
                    "shortPlayId": f"eq.{self.movie_id}",
                    "select": "*",
                    "limit": "1",
                },
                timeout=30,
            )
            response.raise_for_status()
            rows = response.json()

            if not isinstance(rows, list) or not rows:
                self.error.emit(
                    f"shortPlayId={self.movie_id} chưa có trong {_TABLE}.",
                    self.instance_id,
                )
                return

            row = rows[0]
            raw = row.get("raw") or {}
            raw_episodes = raw.get("episodes") or []
            if not raw_episodes:
                self.error.emit(
                    f"raw.episodes rỗng cho shortPlayId={self.movie_id}. "
                    "Hãy chạy sync script trước.",
                    self.instance_id,
                )
                return

            movie_name = str(row.get("showName") or "")
            total = row.get("total") or 0
            captured = row.get("captured") or 0

            episodes = _parse_shortmax_episodes(raw_episodes, movie_name)
            if not episodes:
                self.error.emit(
                    f"Không parse được tập nào từ {_TABLE}.raw.episodes.",
                    self.instance_id,
                )
                return

            self.log_msg.emit(
                f"DB {_TABLE}: {len(episodes)} tập, total={total}, captured={captured}",
                self.instance_id,
            )
            self.success.emit(episodes, movie_name, self.movie_id, self.instance_id)
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}", self.instance_id)


class ShortMaxDownloadWorker(DramaWaveDownloadMergeWorker):
    """Download HLS video for ShortMax — no subtitle, no FFmpeg merge."""

    subtitle_error_source = "shortmax"


class ShortMaxTab(XemShortTab):
    """ShortMax downloader — DB-only fetch, no subtitle merge."""

    def settings(self) -> QSettings:
        return QSettings(_SM_APP_NAME, _SM_CONFIG_KEY)

    def _cache_source(self) -> str:
        return "shortmax"

    def _cache_source_name(self) -> str:
        return "ShortMax"

    def _load_settings(self):
        s = self.settings()
        self.ns_save_dir_edit.setText(
            s.value("save_dir", str(Path.home() / "Downloads" / "ShortMax"))
        )
        self.ns_api_url_edit.setText(_API_URL)
        self.ns_api_url_edit.setPlaceholderText(_API_URL)
        self.ns_api_url_edit.setReadOnly(True)
        self.ns_concurrency_spin.setValue(int(s.value("concurrency", 4)))

        # ShortMax videos have embedded subs — disable sub/merge options permanently
        self.ns_sub_checkbox.setChecked(False)
        self.ns_sub_checkbox.setEnabled(False)
        self.ns_merge_checkbox.setChecked(False)
        self.ns_merge_checkbox.setEnabled(False)
        self.ns_m3u8_checkbox.setChecked(False)
        self.ns_m3u8_reencode_checkbox.setChecked(False)

        # ShortMax is DB-only — hide live API fetch button and search
        self.ns_fetch_btn.hide()
        self.ns_search_movie_btn.hide()
        self.ns_fetch_db_btn.setText("Lấy từ DB (ShortMax)")
        self.ns_fetch_db_btn.setToolTip(
            f"Lấy episodes từ Supabase {_TABLE}.raw.episodes"
        )

        # Hide sub style controls (irrelevant for ShortMax)
        for widget in (
            self.ns_sub_font_combo,
            self.ns_sub_size_spin,
            self.ns_sub_margin_v_spin,
            self.ns_sub_color_combo,
            self.ns_sub_bold_cb,
            self.ns_sub_italic_cb,
            self.ns_sub_outline_spin,
        ):
            widget.hide()

    def _ns_on_fetch(self):
        self._ns_on_fetch_from_db()

    def _ns_on_fetch_from_db(self) -> None:
        movie_id = self.ns_movie_id_edit.text().strip()
        if not movie_id:
            QtWidgets.QMessageBox.warning(self, "Thiếu input", "Vui lòng nhập Movie ID (shortPlayId).")
            return

        self.ns_fetch_db_btn.setEnabled(False)
        self.ns_fetch_btn.setEnabled(False)
        self.ns_status.setText(f"Đang lấy ShortMax từ DB: {movie_id}...")
        self._log(f"[DB] Fetching từ {_TABLE}: shortPlayId={movie_id}...")

        worker = ShortMaxFetchWorker(movie_id)
        self._fetch_instance_id = worker.instance_id
        self._fetch_workers.append(worker)

        worker.success.connect(self._ns_on_fetch_success)
        worker.error.connect(self._ns_on_fetch_error)
        worker.log_msg.connect(lambda msg, _: self._log(f"[DB] {msg}"))
        worker.finished.connect(lambda: self.ns_fetch_db_btn.setEnabled(True))
        worker.finished.connect(lambda: self.ns_fetch_btn.setEnabled(True))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(
            lambda w=worker: self._fetch_workers.remove(w) if w in self._fetch_workers else None
        )
        worker.start()

    def _ns_on_fetch_success(
        self,
        episodes: list[XSEpisode],
        movie_name: str,
        movie_id: str,
        instance_id: int,
    ):
        if instance_id != self._fetch_instance_id:
            return
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        self.ns_status.setText(f"Fetched ShortMax {len(episodes)} tập.")
        self._log(f"Fetched ShortMax {len(episodes)} tập.")
        self._ns_show_picker(episodes, name, movie_id)

    def _create_download_worker(self, movie: XSMovie, **kwargs) -> ShortMaxDownloadWorker:
        # Force no sub download and no merge — video already has embedded subs
        kwargs["download_sub"] = False
        kwargs["do_merge"] = False
        return ShortMaxDownloadWorker(movie, **kwargs)
