"""
Paper-directory size management: junk removal, PDF→PNG conversion,
formula-image detection, image shrinking, and overall directory trimming.
"""
import logging
import math
import re
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_JUNK_SUFFIXES = frozenset({
    ".aux", ".log", ".out", ".brf", ".fls", ".fdb_latexmk",
    ".synctex.gz", ".blg", ".toc", ".lof", ".lot",
    ".nav", ".snm", ".vrb",
    ".sty", ".cls", ".bst", ".def",
    ".ttf", ".otf", ".pfb", ".tfm", ".vf", ".fd", ".mf",
    ".eps", ".ps",
    ".bib",
})
_IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp", ".svg",
})
_JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})
_MAX_DIR_BYTES = 20 * 1024 * 1024   # 20 MB (leaves headroom for API overhead)
_TARGET_DPI = 200
_IMAGE_SHRINK_THRESHOLD = 500 * 1024  # default target for summary-only images
_PRE_SUMMARY_IMAGE_SHRINK_THRESHOLD = 2 * 1024 * 1024
_IMAGE_MAX_PIXELS = 1200              # longest-side cap when shrinking
_IMAGE_MIN_PIXELS = 400               # shortest-side floor when shrinking
_IMAGE_TARGET_SIZE_TOLERANCE = 1.03


def dir_size(paper_dir: Path) -> int:
    return sum(f.stat().st_size for f in paper_dir.rglob("*") if f.is_file())


def _convert_pdf_to_png(pdf_path: Path, dpi: int = _TARGET_DPI) -> None:
    """Rasterise a PDF to PNG(s) at *dpi* and delete the original."""
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        zoom = dpi / 72
        mat = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=mat)
            longest = max(pix.width, pix.height)
            if longest > _IMAGE_MAX_PIXELS:
                scale = _IMAGE_MAX_PIXELS / longest * zoom
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
            if len(doc) == 1:
                out = pdf_path.with_suffix(".png")
            else:
                out = pdf_path.with_name(f"{pdf_path.stem}_p{i}.png")
            pix.save(str(out))
        doc.close()
        pdf_path.unlink()
    except Exception:
        pass


def _is_formula_image(img_path: Path) -> bool:
    """Return True if the image is likely a mathematical formula rather than a figure."""
    try:
        import fitz
        pix = fitz.Pixmap(str(img_path))
        w, h = pix.width, pix.height

        if w * h < 5000:
            return True

        ratio = w / max(h, 1)

        if h < 60 and ratio > 5:
            return True

        n = pix.n
        total = w * h
        step = max(1, total // 1000)
        samples = pix.samples
        black = white = other = 0
        for i in range(0, total, step):
            off = i * n
            if off + n > len(samples):
                break
            channels = [samples[off + c] for c in range(min(n, 3))]
            brightness = sum(channels) / len(channels)
            if brightness < 30:
                black += 1
            elif brightness > 225:
                white += 1
            else:
                other += 1
        sampled = black + white + other
        if sampled == 0:
            return False

        bw_ratio = (black + white) / sampled
        if bw_ratio > 0.90 and h < 130 and ratio > 4:
            return True
    except Exception:
        pass
    return False


def filter_formula_images(img_dir: Path, md_text: str) -> str:
    """Remove formula images from disk and their references from Markdown."""
    removed = []
    for f in sorted(img_dir.iterdir()):
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        if _is_formula_image(f):
            removed.append(f.name)
            f.unlink()
    for name in removed:
        md_text = md_text.replace(f"![](images/{name})", "")
    md_text = re.sub(r"\n{3,}", "\n\n", md_text)
    return md_text


def _jpeg_quality_for_target_ratio(size_ratio: float) -> int:
    if size_ratio >= 0.95:
        return 90
    if size_ratio >= 0.85:
        return 88
    if size_ratio >= 0.70:
        return 86
    return 84


def _rewrite_scaled_image(
    img_path: Path,
    *,
    target_width: int,
    target_height: int,
    has_alpha: bool,
    current_size: int,
    jpg_quality: int = 95,
) -> int | None:
    import fitz

    doc = fitz.open(str(img_path))
    try:
        page = doc[0]
        if page.rect.width <= 0 or page.rect.height <= 0:
            return None
        scale = min(target_width / page.rect.width, target_height / page.rect.height)
        scaled = doc[0].get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            alpha=has_alpha,
        )
    finally:
        doc.close()

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{img_path.name}.",
            suffix=img_path.suffix,
            dir=img_path.parent,
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
        scaled.save(str(tmp_path), jpg_quality=jpg_quality)
        new_size = tmp_path.stat().st_size
        if new_size >= current_size:
            tmp_path.unlink(missing_ok=True)
            return None
        tmp_path.replace(img_path)
        return new_size
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def shrink_image(
    img_path: Path,
    *,
    target_bytes: int = _IMAGE_SHRINK_THRESHOLD,
    max_pixels: int = _IMAGE_MAX_PIXELS,
    min_pixels: int = _IMAGE_MIN_PIXELS,
) -> bool:
    """Down-sample an image toward *target_bytes* without shrinking below *min_pixels*."""
    try:
        before_size = img_path.stat().st_size
        target_limit = int(target_bytes * _IMAGE_TARGET_SIZE_TOLERANCE)
        if target_bytes <= 0 or before_size <= target_limit:
            return False

        import fitz

        current_size = before_size
        changed = False
        for _ in range(6):
            pix = fitz.Pixmap(str(img_path))
            width, height = pix.width, pix.height
            shortest = min(width, height)
            if shortest <= min_pixels:
                break

            suffix = img_path.suffix.lower()
            size_ratio = target_limit / current_size
            has_alpha = bool(pix.alpha) and suffix not in _JPEG_SUFFIXES
            jpg_quality = (
                _jpeg_quality_for_target_ratio(size_ratio)
                if suffix in _JPEG_SUFFIXES
                else 95
            )

            if suffix in _JPEG_SUFFIXES:
                new_size = _rewrite_scaled_image(
                    img_path,
                    target_width=width,
                    target_height=height,
                    has_alpha=False,
                    current_size=current_size,
                    jpg_quality=jpg_quality,
                )
                if new_size is not None:
                    changed = True
                    current_size = new_size
                    if current_size <= target_limit:
                        break
                    continue

            size_scale = math.sqrt(size_ratio)
            scale = min(0.995, size_scale if size_ratio >= 0.85 else size_scale * 0.98)

            if max_pixels > 0:
                longest = max(width, height)
                if longest > max_pixels:
                    over_target = 1 - (target_limit / current_size)
                    cap_scale = max_pixels / longest
                    soft_cap_scale = 1 - ((1 - cap_scale) * over_target)
                    scale = min(scale, soft_cap_scale)

            min_scale = min_pixels / shortest
            if min_scale >= 1:
                break
            scale = max(scale, min_scale)

            new_size = None
            seen_sizes: set[tuple[int, int]] = set()
            for candidate_scale in (scale, scale * 0.95, scale * 0.90, scale * 0.85, scale * 0.80):
                candidate_scale = max(candidate_scale, min_scale)
                new_width = max(1, int(width * candidate_scale))
                new_height = max(1, int(height * candidate_scale))
                if new_width >= width and new_height >= height:
                    continue
                if (new_width, new_height) in seen_sizes:
                    continue
                seen_sizes.add((new_width, new_height))

                new_size = _rewrite_scaled_image(
                    img_path,
                    target_width=new_width,
                    target_height=new_height,
                    has_alpha=has_alpha,
                    current_size=current_size,
                    jpg_quality=jpg_quality,
                )
                if new_size is not None:
                    break

            if new_size is None:
                break

            changed = True
            current_size = new_size
            if current_size <= target_limit:
                break

        return changed and img_path.stat().st_size < before_size
    except Exception:
        return False


def _shrink_image(
    img_path: Path,
    max_pixels: int = _IMAGE_MAX_PIXELS,
    target_bytes: int = _IMAGE_SHRINK_THRESHOLD,
) -> bool:
    """Backward-compatible wrapper for image shrinking."""
    return shrink_image(img_path, target_bytes=target_bytes, max_pixels=max_pixels)


def convert_figure_pdfs(paper_dir: Path) -> None:
    """Convert single-page PDFs (likely figures) to PNG and remove originals."""
    for pdf in list(paper_dir.rglob("*.pdf")):
        if not pdf.is_file() or pdf.name == "paper.pdf":
            continue
        try:
            import fitz
            doc = fitz.open(str(pdf))
            page_count = len(doc)
            doc.close()
            if page_count == 1:
                _convert_pdf_to_png(pdf)
        except Exception:
            pass


def _collect_referenced_stems(paper_dir: Path) -> set[str]:
    """Scan all .tex files and return stems of images referenced via includegraphics."""
    stems: set[str] = set()
    for tex in paper_dir.rglob("*.tex"):
        try:
            text = tex.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", text):
                ref = m.group(1).strip()
                stems.add(Path(ref).stem)
        except Exception:
            pass
    return stems


def _find_batch_dirs(paper_dir: Path, threshold: int = 10) -> set[Path]:
    """Find directories with many image files (likely supplementary data)."""
    batch: set[Path] = set()
    for d in paper_dir.rglob("*"):
        if not d.is_dir():
            continue
        img_count = sum(
            1 for f in d.iterdir()
            if f.is_file() and f.suffix.lower() in _IMAGE_SUFFIXES
        )
        if img_count > threshold:
            batch.add(d)
    return batch


_TIER_LABELS = ["unreferenced+batch", "unreferenced", "referenced+batch", "referenced"]


def _deletion_tier(f: Path, referenced_stems: set[str], batch_dirs: set[Path]) -> int:
    """Lower tier = delete first. 0 = unreferenced batch, 3 = referenced standalone."""
    is_referenced = f.stem in referenced_stems
    in_batch = any(f.is_relative_to(bd) for bd in batch_dirs)
    if not is_referenced and in_batch:
        return 0
    if not is_referenced:
        return 1
    if in_batch:
        return 2
    return 3


def trim_paper_dir(paper_dir: Path, max_bytes: int = _MAX_DIR_BYTES) -> None:
    """Reduce a paper directory to fit within *max_bytes*."""
    log = logging.getLogger("MaxRead")

    for f in paper_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in _JUNK_SUFFIXES:
            f.unlink()

    converted_pdfs = []
    for f in list(paper_dir.rglob("*.pdf")):
        if f.is_file():
            old_name = f.name
            _convert_pdf_to_png(f)
            if not f.exists():
                converted_pdfs.append(old_name)

    if converted_pdfs:
        for tex in paper_dir.rglob("*.tex"):
            try:
                text = tex.read_text(encoding="utf-8", errors="replace")
                updated = text
                for old_name in converted_pdfs:
                    new_name = old_name.rsplit(".", 1)[0] + ".png"
                    updated = updated.replace(old_name, new_name)
                if updated != text:
                    tex.write_text(updated, encoding="utf-8")
            except Exception:
                pass

    for f in paper_dir.rglob("*"):
        if (
            f.is_file()
            and f.suffix.lower() in _IMAGE_SUFFIXES
            and f.stat().st_size > _PRE_SUMMARY_IMAGE_SHRINK_THRESHOLD
        ):
            shrink_image(f, target_bytes=_PRE_SUMMARY_IMAGE_SHRINK_THRESHOLD)

    total = dir_size(paper_dir)
    if total <= max_bytes:
        return

    referenced_stems = _collect_referenced_stems(paper_dir)
    batch_dirs = _find_batch_dirs(paper_dir)

    all_images = [
        f for f in paper_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in _IMAGE_SUFFIXES
    ]
    all_images.sort(key=lambda f: (
        _deletion_tier(f, referenced_stems, batch_dirs),
        -f.stat().st_size,
    ))

    for img in all_images:
        tier = _deletion_tier(img, referenced_stems, batch_dirs)
        log.warning("[trim] Removing image (%s): %s (%d bytes)",
                    _TIER_LABELS[tier], img.relative_to(paper_dir), img.stat().st_size)
        img.unlink()
        if dir_size(paper_dir) <= max_bytes:
            break
