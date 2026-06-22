"""phimngan.tv downloader tab cloned from NetShort with source-specific fetch."""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtWidgets
from PySide6.QtCore import QSettings

from xemshort.models import XSEpisode, XSMovie
from xemshort.tab import XemShortTab

from .workers import PhimNganDownloadMergeWorker, PhimNganFetchWorker
from .movie_search_dialog import PhimNganMovieSearchDialog


_PN_APP_NAME = "XemShort GUI"
_PN_CONFIG_KEY = "PhimNgan"
PHIMNGAN_API_URL = "https://phimngan.tv/movies/{movie_id}?_rsc=h1khq"


class PhimNganTab(XemShortTab):
    """phimngan.tv variant of the XemShort downloader UI."""

    def settings(self) -> QSettings:
        return QSettings(_PN_APP_NAME, _PN_CONFIG_KEY)

    def _cache_source(self) -> str:
        return "phimngan"

    def _cache_source_name(self) -> str:
        return "phimngan.tv"

    def _load_settings(self):
        s = self.settings()
        self.ns_save_dir_edit.setText(
            s.value("save_dir", str(Path.home() / "Downloads" / "PhimNgan")))
        self.ns_api_url_edit.setText(s.value("api_url", PHIMNGAN_API_URL))
        self.ns_api_url_edit.setPlaceholderText(PHIMNGAN_API_URL)
        self.ns_movie_id_edit.setPlaceholderText(
            "phan-quan-vuong-phi-du-dan hoặc https://phimngan.tv/movies/..."
        )
        self.ns_search_movie_btn.setText("Tim kiem")
        self.ns_concurrency_spin.setValue(int(s.value("concurrency", 4)))
        self.ns_sub_checkbox.setChecked(self._setting_bool(s, "download_sub", True))
        self.ns_merge_checkbox.setChecked(self._setting_bool(s, "do_merge", True))
        self.ns_m3u8_checkbox.setChecked(False)
        self.ns_m3u8_reencode_checkbox.setChecked(False)
        self.ns_crf_spin.setValue(int(s.value("crf", 20)))
        self.ns_merge_threads_spin.setValue(int(s.value("merge_threads", 1)))
        self.ns_encode_threads_spin.setValue(int(s.value("encode_threads", 3)))
        self.ns_sub_font_combo.setCurrentText(s.value("sub_font", "UTM Alter Gothic"))
        self.ns_sub_size_spin.setValue(int(s.value("sub_size", 15)))
        self.ns_sub_margin_v_spin.setValue(int(s.value("sub_margin_v", 70)))
        default_color = self.ns_sub_color_combo.itemText(0) or "White"
        self.ns_sub_color_combo.setCurrentText(s.value("sub_color", default_color))
        self.ns_sub_bold_cb.setChecked(self._setting_bool(s, "sub_bold", True))
        self.ns_sub_italic_cb.setChecked(self._setting_bool(s, "sub_italic", False))
        self.ns_sub_outline_spin.setValue(float(s.value("sub_outline", 2.0)))
        self.ns_force_api_cb.setChecked(self._setting_bool(s, "force_api", False))

    def _build_ui(self):
        # Create force_api checkbox BEFORE super() because _load_settings() inside
        # super()._build_ui() will try to access self.ns_force_api_cb.
        self.ns_force_api_cb = QtWidgets.QCheckBox("Goi API truc tiep từ phimngan.tv")
        self.ns_force_api_cb.setToolTip(
            "Neu check: goi API phimngan.tv truc tiep, khong tra cuu Supabase.\n"
            "Bo check (mac dinh): tra cuu Supabase truoc, chi goi API khi khong tim thay."
        )
        self.ns_force_api_cb.setStyleSheet(
            "QCheckBox { color: #0D0D0E; font-weight: bold; spacing: 6px; }"
            "QCheckBox::indicator { width: 16px; height: 16px; }"
        )

        super()._build_ui()

        # Inject checkbox into the "Thêm phim" groupbox between row1 and row2
        for child in self.children():
            if isinstance(child, QtWidgets.QGroupBox) and child.title() == "Thêm phim":
                inp_layout = child.layout()
                if inp_layout is not None and inp_layout.count() >= 2:
                    api_row = QtWidgets.QHBoxLayout()
                    api_row.setSpacing(10)
                    api_row.addWidget(self.ns_force_api_cb)
                    api_row.addStretch()
                    inp_layout.addLayout(api_row)
                break

        # Load saved checkbox state
        self.ns_force_api_cb.setChecked(
            self._setting_bool(self.settings(), "force_api", False)
        )
        self.ns_force_api_cb.toggled.connect(lambda *_: self._save_settings())

    def _ns_on_search_movie(self):
        dlg = PhimNganMovieSearchDialog(self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            slug, name = dlg.get_result()
            if slug:
                self.ns_movie_id_edit.setText(slug)
                self.ns_movie_id_edit.setFocus()
                self.ns_status.setText(f"Da chon: {name} ({slug})")
                self._log(f"[search] selected slug={slug}, name={name}")

    def _ns_on_fetch(self):
        slug = self.ns_movie_id_edit.text().strip()
        if not slug:
            QtWidgets.QMessageBox.warning(self, "Thieu input", "Vui long nhap slug phim.")
            return
        api_url = self.ns_api_url_edit.text().strip()
        if not api_url.startswith(("http://", "https://")):
            QtWidgets.QMessageBox.warning(
                self,
                "API URL",
                "API URL phai bat dau bang http:// hoac https://.",
            )
            return

        self.ns_fetch_btn.setEnabled(False)
        self.ns_status.setText(f"Dang fetch phimngan.tv {slug}...")
        self._log(f"Fetching phimngan.tv {slug}...")

        worker = PhimNganFetchWorker(api_url, slug, force_api=self.ns_force_api_cb.isChecked())
        self._fetch_instance_id = worker.instance_id
        self._fetch_workers.append(worker)

        worker.success.connect(self._ns_on_fetch_success)
        worker.cache_hit.connect(self._ns_on_fetch_cache_hit)
        worker.error.connect(self._ns_on_fetch_error)
        worker.finished.connect(
            lambda: self.ns_fetch_btn.setEnabled(not (self.nsworker and self.nsworker.isRunning())))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(
            lambda w=worker: self._fetch_workers.remove(w) if w in self._fetch_workers else None
        )
        worker.start()

    def _ns_on_fetch_success(self, episodes: list[XSEpisode], movie_name: str, movie_id: str, instance_id: int):
        if instance_id != self._fetch_instance_id:
            return
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        self.ns_status.setText(f"Fetched phimngan.tv {len(episodes)} tap.")
        self._log(f"Fetched phimngan.tv {len(episodes)} tap.")
        self._ns_show_picker(episodes, name, movie_id)

    def _create_download_worker(self, movie: XSMovie, **kwargs) -> PhimNganDownloadMergeWorker:
        return PhimNganDownloadMergeWorker(movie, **kwargs)
