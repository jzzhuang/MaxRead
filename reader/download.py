"""
arXiv source download, archive extraction, and PDF-to-text conversion.
"""
import gzip
import logging
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .dir_manager import filter_formula_images

# ArXiv requires a descriptive User-Agent
USER_AGENT = "MaxRead-arxiv-summarize/1.0 (mailto:research@example.com)"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ArxivNotFoundError(Exception):
    """Raised when the arXiv e-print returns a 404."""
    def __init__(self, arxiv_id: str) -> None:
        self.arxiv_id = arxiv_id
        super().__init__(f"arXiv e-print not found: {arxiv_id}")


class PdfExtractionError(Exception):
    """Raised when a PDF-only paper cannot be converted to readable text."""
    pass


# ---------------------------------------------------------------------------
# arXiv ID parsing
# ---------------------------------------------------------------------------
# Bare arXiv id: YYMM.NNNNN or YYMM.NNNNNvN
_ARXIV_BARE_RE = re.compile(r"(?:^|[^\w.])(\d{4}\.\d{4,5}(?:v\d+)?)(?=[^\d]|$)")
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf|e-print)/([a-zA-Z0-9._/-]+)", re.I)

# Suffixes that are part of the URL path but not part of the arXiv id
_URL_STRIP_SUFFIXES = re.compile(r"(?:\.pdf|\.html?)$", re.I)


def _clean_arxiv_id(raw: str) -> str:
    """Strip URL artifacts like trailing .pdf from a captured arXiv id."""
    return _URL_STRIP_SUFFIXES.sub("", raw).strip()


def arxiv_id_from_url(url: str) -> str | None:
    """Extract arXiv id from abs/pdf/e-print URL. E.g. 2301.12345 or 2301.12345v2."""
    m = _ARXIV_URL_RE.search(url)
    return _clean_arxiv_id(m.group(1)) if m else None


def extract_arxiv_ids(text: str) -> list[str]:
    """Extract all arXiv ids from text: full URLs and bare ids like 2301.12345."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _ARXIV_URL_RE.finditer(text):
        aid = _clean_arxiv_id(m.group(1))
        if aid and aid not in seen:
            seen.add(aid)
            out.append(aid)
    for m in _ARXIV_BARE_RE.finditer(text):
        aid = m.group(1).strip()
        if aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


# ---------------------------------------------------------------------------
# Archive download & extraction
# ---------------------------------------------------------------------------
def _verify_archive(path: Path) -> None:
    """Trial-open the downloaded archive to verify it's not truncated."""
    data = path.read_bytes()
    if len(data) == 0:
        raise IOError("Downloaded file is empty")
    if data[:2] == b"\x1f\x8b":  # gzip / tar.gz
        try:
            with tarfile.open(path, "r:gz") as tf:
                tf.getmembers()
        except tarfile.ReadError:
            with gzip.open(path, "rb") as gz:
                gz.read(1024)
    elif data[:2] == b"PK":  # zip
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise IOError(f"Corrupt entry in zip: {bad}")
    # else: PDF or plain text — extract_archive() handles it


def download_source(arxiv_id: str, *, _max_retries: int = 3) -> Path:
    """Download e-print source from arXiv; return path to the downloaded file."""
    import time

    log = logging.getLogger("MaxRead")
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".arxiv") as f:
        path = Path(f.name)
    for attempt in range(_max_retries):
        try:
            result = subprocess.run(
                [
                    "curl", "-fL",
                    "--max-time", "700",
                    "--speed-limit", "10240",
                    "--speed-time", "240",
                    "-H", f"User-Agent: {USER_AGENT}",
                    "-o", str(path),
                    url,
                ],
                capture_output=True,
                timeout=750,
            )
            if result.returncode == 22:  # curl -f returns 22 for HTTP 4xx/5xx
                stderr = result.stderr.decode(errors="replace")
                if "404" in stderr:
                    path.unlink(missing_ok=True)
                    raise ArxivNotFoundError(arxiv_id)
                path.unlink(missing_ok=True)
                raise IOError(f"HTTP error downloading {arxiv_id}: {stderr.strip()}")
            if result.returncode != 0:
                raise IOError(
                    f"curl exited with code {result.returncode}: "
                    f"{result.stderr.decode(errors='replace').strip()}"
                )
            if not path.exists() or path.stat().st_size == 0:
                raise IOError("Downloaded file is empty")

            _verify_archive(path)
            return path
        except ArxivNotFoundError:
            raise
        except (IOError, subprocess.TimeoutExpired, tarfile.ReadError) as e:
            log.warning(
                "[%s] Download attempt %d/%d failed: %s",
                arxiv_id, attempt + 1, _max_retries, e,
            )
            if attempt < _max_retries - 1:
                time.sleep(2 ** attempt * 5)  # 5s, 10s
                continue
            path.unlink(missing_ok=True)
            raise
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return path  # unreachable, keeps type checkers happy


def _pdf_to_text(pdf_path: Path, out_dir: Path) -> str:
    """Extract text and images from a PDF as Markdown. Returns Markdown with inline image refs or raises PdfExtractionError."""
    try:
        import pymupdf4llm
    except ImportError:
        raise PdfExtractionError("pymupdf4llm is not installed")
    try:
        import fitz
    except ImportError:
        raise PdfExtractionError("PyMuPDF (fitz) is not installed")
    try:
        img_dir = out_dir / "images"
        img_dir.mkdir(exist_ok=True)

        md_text = pymupdf4llm.to_markdown(
            str(pdf_path),
            write_images=True,
            image_path=str(img_dir),
            image_format="png",
            image_size_limit=0,
            show_progress=False,
        )
        md_text = md_text.replace(str(img_dir) + "/", "images/")
        md_text = filter_formula_images(img_dir, md_text)
    except PdfExtractionError:
        raise
    except Exception as e:
        raise PdfExtractionError(f"Failed to read PDF: {e}")
    if len(md_text) < 200:
        raise PdfExtractionError(f"Extracted text too short ({len(md_text)} chars), likely a scanned PDF")
    return md_text


def extract_archive(archive_path: Path, out_dir: Path) -> None:
    """Extract .tar.gz, .zip, gzipped single file, PDF, or plain TeX to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = archive_path.read_bytes()
    if data[:2] == b"\x1f\x8b":  # gzip
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(out_dir)
        except tarfile.ReadError:
            with gzip.open(archive_path, "rb") as gz:
                content = gz.read()
            name = "main.tex" if b"\\documentclass" in content or b"\\begin{" in content else "paper.txt"
            (out_dir / name).write_bytes(content)
    elif data[:2] == b"PK":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(out_dir)
    elif data[:4] == b"%PDF":
        shutil.copy2(archive_path, out_dir / "paper.pdf")
        text = _pdf_to_text(out_dir / "paper.pdf", out_dir)
        (out_dir / "paper.md").write_text(text, encoding="utf-8")
    elif b"\\documentclass" in data[:4096] or b"\\begin{" in data[:4096]:
        (out_dir / "main.tex").write_bytes(data)
    else:
        shutil.copy2(archive_path, out_dir / "paper.bin")
        logging.getLogger("MaxRead").warning(
            "Unknown archive format (magic=%r), saved as paper.bin", data[:4],
        )
