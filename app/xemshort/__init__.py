"""XemShort — package for the XemShort downloader tab."""
from .models import NSMovie, NSEpisode, XSEpisode, XSMovie
from .workers import NSDownloadMergeWorker, NSFetchWorker, XSDownloadMergeWorker, XSFetchWorker
from .dialogs import (
    NSDetailDialog,
    NSEpisodePickerDialog,
    NSPasteJsonDialog,
    NSVideoPopup,
)
from .tab import XemShortTab

__all__ = [
    # models
    "NSEpisode", "NSMovie", "XSEpisode", "XSMovie",
    # workers
    "NSFetchWorker", "NSDownloadMergeWorker",
    "XSFetchWorker", "XSDownloadMergeWorker",
    # dialogs
    "NSDetailDialog", "NSEpisodePickerDialog", "NSPasteJsonDialog", "NSVideoPopup",
    # tab
    "XemShortTab",
]
