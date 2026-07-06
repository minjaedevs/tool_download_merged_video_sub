"""XemShort — package for the XemShort downloader tab."""
from .models import NSMovie, NSEpisode, XSEpisode, XSMovie
from .workers import (
    DramaWaveDownloadMergeWorker,
    DramaWaveFetchWorker,
    NSDownloadMergeWorker,
    NSFetchWorker,
    XSDownloadMergeWorker,
    XSFetchFromSupabaseWorker,
    XSFetchWorker,
)
from .dialogs import (
    XSDetailDialog,
    XSEpisodePickerDialog,
    XSPasteJsonDialog,
    XSVideoPopup,
    XSVttEditorDialog,
    # backward-compat aliases
    NSDetailDialog,
    NSEpisodePickerDialog,
    NSPasteJsonDialog,
    NSVideoPopup,
    NSVttEditorDialog,
)
from .tab import XemShortTab
from .dramawave_tab import DramaWaveTab
from .movie_manager_tab import MovieManagerTab
from .request_queue_tab import RequestQueueTab

__all__ = [
    # models
    "NSEpisode", "NSMovie", "XSEpisode", "XSMovie",
    # workers
    "NSFetchWorker", "NSDownloadMergeWorker",
    "XSFetchWorker", "XSFetchFromSupabaseWorker", "XSDownloadMergeWorker",
    "DramaWaveFetchWorker", "DramaWaveDownloadMergeWorker",
    # dialogs (XS = current, NS = backward-compat)
    "XSDetailDialog", "XSEpisodePickerDialog", "XSPasteJsonDialog",
    "XSVideoPopup", "XSVttEditorDialog",
    "NSDetailDialog", "NSEpisodePickerDialog", "NSPasteJsonDialog",
    "NSVideoPopup", "NSVttEditorDialog",
    # tabs
    "XemShortTab", "DramaWaveTab", "MovieManagerTab", "RequestQueueTab",
]
