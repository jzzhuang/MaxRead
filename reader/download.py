"""
arXiv source download, archive extraction, and PDF-to-text conversion.
"""
import gzip
import importlib.util
import logging
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import requests

from .dir_manager import filter_formula_images

# ArXiv requires a descriptive User-Agent
USER_AGENT = "MaxRead/1.0 (+https://github.com/jzzhuang/MaxRead)"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 250 * 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_PATH_DEPTH = 20
MAX_ARCHIVE_PATH_CHARS = 1_024
MAX_PDF_PAGES = 300


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
_SAFE_ARXIV_ID_RE = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")


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
    size = path.stat().st_size
    if size == 0:
        raise IOError("Downloaded file is empty")
    if size > MAX_DOWNLOAD_BYTES:
        raise IOError(f"Downloaded file exceeds {MAX_DOWNLOAD_BYTES} bytes")
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic[:2] == b"\x1f\x8b":  # gzip / tar.gz
        try:
            count = 0
            total = 0
            with tarfile.open(path, "r|gz") as tf:
                for member in tf:
                    count += 1
                    if count > MAX_ARCHIVE_MEMBERS:
                        raise OSError("Archive contains too many members")
                    if member.isfile():
                        total += member.size
                    if member.size > MAX_MEMBER_BYTES or total > MAX_EXTRACTED_BYTES:
                        raise OSError("Archive expands beyond the configured size limit")
        except tarfile.ReadError:
            with gzip.open(path, "rb") as gz:
                gz.read(1024)
    elif magic[:2] == b"PK":  # zip
        with zipfile.ZipFile(path, "r") as zf:
            members = zf.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise OSError("Archive contains too many members")
            if any(member.file_size > MAX_MEMBER_BYTES for member in members):
                raise OSError("Archive contains an oversized member")
            if sum(member.file_size for member in members) > MAX_EXTRACTED_BYTES:
                raise OSError("Archive expands beyond the configured size limit")
    # else: PDF or plain text — extract_archive() handles it


def download_source(arxiv_id: str, *, _max_retries: int = 3) -> Path:
    """Download e-print source from arXiv; return path to the downloaded file."""
    import time

    if (
        not _SAFE_ARXIV_ID_RE.fullmatch(arxiv_id)
        or arxiv_id.startswith("/")
        or ".." in arxiv_id
    ):
        raise ValueError(f"Invalid arXiv id: {arxiv_id!r}")
    log = logging.getLogger("MaxRead")
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".arxiv") as f:
        path = Path(f.name)
    for attempt in range(_max_retries):
        try:
            with requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                timeout=(15, 240),
            ) as response:
                if response.status_code == 404:
                    raise ArxivNotFoundError(arxiv_id)
                response.raise_for_status()
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > MAX_DOWNLOAD_BYTES:
                    raise OSError(f"Download exceeds {MAX_DOWNLOAD_BYTES} bytes")
                written = 0
                with path.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise OSError(f"Download exceeds {MAX_DOWNLOAD_BYTES} bytes")
                        output.write(chunk)
                if written == 0:
                    raise OSError("Downloaded file is empty")

            _verify_archive(path)
            return path
        except ArxivNotFoundError:
            raise
        except (OSError, requests.RequestException, tarfile.ReadError) as e:
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
    if pdf_path.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise PdfExtractionError(f"PDF exceeds {MAX_DOWNLOAD_BYTES} bytes")
    try:
        import pymupdf4llm
    except ImportError:
        raise PdfExtractionError("pymupdf4llm is not installed")
    if importlib.util.find_spec("fitz") is None:
        raise PdfExtractionError("PyMuPDF (fitz) is not installed")
    import fitz

    try:
        with fitz.open(str(pdf_path)) as document:
            if document.needs_pass:
                raise PdfExtractionError("Encrypted PDFs are not supported")
            if document.page_count > MAX_PDF_PAGES:
                raise PdfExtractionError(
                    f"PDF has {document.page_count} pages; limit is {MAX_PDF_PAGES}"
                )
    except PdfExtractionError:
        raise
    except Exception as exc:
        raise PdfExtractionError(f"Invalid PDF: {exc}") from exc
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


def _safe_member_path(out_dir: Path, raw_name: str) -> Path:
    """Resolve an archive member below *out_dir* or reject it."""
    normalized = raw_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if not normalized or member.is_absolute() or ".." in member.parts:
        raise ValueError(f"Unsafe archive path: {raw_name!r}")
    if member.parts and ":" in member.parts[0]:
        raise ValueError(f"Unsafe archive path: {raw_name!r}")
    if len(normalized) > MAX_ARCHIVE_PATH_CHARS or len(member.parts) > MAX_ARCHIVE_PATH_DEPTH:
        raise ValueError(f"Archive path is too deep or long: {raw_name!r}")
    if any(len(part) > 255 for part in member.parts):
        raise ValueError(f"Archive path component is too long: {raw_name!r}")
    base = out_dir.resolve()
    target = (base / Path(*member.parts)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Archive path escapes output directory: {raw_name!r}") from exc
    return target


def _copy_limited(source, destination: Path, limit: int) -> int:
    """Copy at most *limit* bytes, raising before writing excess data."""
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("wb") as output:
            while True:
                chunk = source.read(min(1024 * 1024, limit - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"Extracted member exceeds {limit} bytes")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return written


def _extract_tar(archive_path: Path, out_dir: Path) -> None:
    with tarfile.open(archive_path, "r|gz") as archive:
        total = 0
        count = 0
        for member in archive:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Archive contains too many members")
            target = _safe_member_path(out_dir, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"Unsupported tar member type: {member.name!r}")
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise ValueError(f"Archive member is too large: {member.name!r}")
            if total + member.size > MAX_EXTRACTED_BYTES:
                raise ValueError("Archive expands beyond the configured size limit")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Unable to read tar member: {member.name!r}")
            with source:
                total += _copy_limited(source, target, MAX_MEMBER_BYTES)
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("Archive expands beyond the configured size limit")


def _extract_zip(archive_path: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(archive_path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Archive contains too many members")
        declared = sum(member.file_size for member in members)
        if declared > MAX_EXTRACTED_BYTES:
            raise ValueError("Archive expands beyond the configured size limit")
        total = 0
        for member in members:
            target = _safe_member_path(out_dir, member.filename)
            unix_mode = (member.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ValueError(f"Zip symlinks are not supported: {member.filename!r}")
            if member.flag_bits & 0x1:
                raise ValueError(f"Encrypted zip member is not supported: {member.filename!r}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"Archive member is too large: {member.filename!r}")
            with archive.open(member, "r") as source:
                total += _copy_limited(source, target, MAX_MEMBER_BYTES)
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("Archive expands beyond the configured size limit")


def extract_archive(archive_path: Path, out_dir: Path) -> None:
    """Safely extract supported arXiv payloads into *out_dir*."""
    if archive_path.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"Archive exceeds {MAX_DOWNLOAD_BYTES} bytes")
    out_dir.mkdir(parents=True, exist_ok=True)
    with archive_path.open("rb") as stream:
        data = stream.read(4096)
    if data[:2] == b"\x1f\x8b":  # gzip
        try:
            _extract_tar(archive_path, out_dir)
        except tarfile.ReadError:
            with gzip.open(archive_path, "rb") as source:
                temp_path = out_dir / "paper.txt"
                size = _copy_limited(source, temp_path, MAX_MEMBER_BYTES)
            prefix = temp_path.read_bytes()[:4096]
            if b"\\documentclass" in prefix or b"\\begin{" in prefix:
                temp_path.replace(out_dir / "main.tex")
            if size == 0:
                raise ValueError("Gzip payload is empty")
    elif data[:2] == b"PK":
        _extract_zip(archive_path, out_dir)
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
