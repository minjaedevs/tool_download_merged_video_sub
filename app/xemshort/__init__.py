"""XemShort — package for the XemShort downloader tab."""
from .models import NSMovie, NSEpisode, XSEpisode, XSMovie
from .workers import NSDownloadMergeWorker, NSFetchWorker, XSDownloadMergeWorker, XSFetchWorker
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

__all__ = [
    # models
    "NSEpisode", "NSMovie", "XSEpisode", "XSMovie",
    # workers
    "NSFetchWorker", "NSDownloadMergeWorker",
    "XSFetchWorker", "XSDownloadMergeWorker",
    # dialogs (XS = current, NS = backward-compat)
    "XSDetailDialog", "XSEpisodePickerDialog", "XSPasteJsonDialog",
    "XSVideoPopup", "XSVttEditorDialog",
    "NSDetailDialog", "NSEpisodePickerDialog", "NSPasteJsonDialog",
    "NSVideoPopup", "NSVttEditorDialog",
    # tab
    "XemShortTab",
]
