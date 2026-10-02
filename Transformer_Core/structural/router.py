"""Route Hub: extension selects a protocol; it never asserts valid content."""

from pathlib import Path

ROUTES = {
    "text": {".txt", ".md", ".log"},
    "json": {".json"},
    "csv": {".csv", ".tsv"},
    "pdf": {".pdf"},
    "docx": {".docx"},
    "xlsx": {".xlsx"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"},
    "wav": {".wav"},
    "media": {".mp3", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".webm", ".avi", ".mov"},
}


def route(filename: str) -> str:
    # Preserve the original marker and extension; normalization is local routing.
    suffix = Path(filename).suffix.lower()
    return next((name for name, extensions in ROUTES.items() if suffix in extensions), "unknown")
