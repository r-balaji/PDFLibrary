import io
from dataclasses import dataclass
from typing import List

import pikepdf


UNSAFE_ACTIONS = {
    '/GoToR',
    '/ImportData',
    '/JavaScript',
    '/Launch',
    '/SubmitForm',
}

UNSAFE_ANNOTATIONS = {'/FileAttachment', '/Movie', '/RichMedia', '/Screen', '/Sound'}


class UnsafePdfError(ValueError):
    """Raised when a file is malformed, encrypted, or contains active content."""


class PdfLimitError(ValueError):
    """Raised when a valid PDF exceeds a configured processing limit."""


class PdfPageAssignmentError(ValueError):
    """Raised when final split pages are invalid, missing, or duplicated."""


@dataclass
class ChunkSpec:
    chunk_index: int
    start_page: int  # 1-based
    end_page: int    # 1-based, inclusive


def prepare_chunks(
    source_bytes: bytes,
    max_chunk_bytes: int | None,
    chunk_size: int,
    overlap: int,
    max_pages: int,
    max_segments: int,
) -> tuple[int, List[ChunkSpec], List[bytes]]:
    """Validate and create transport chunks inside one isolated worker."""
    total_pages = get_page_count(source_bytes)
    if total_pages < 1:
        raise PdfLimitError('Source PDF has no pages')
    if total_pages > max_pages:
        raise PdfLimitError('Source PDF exceeds the configured page limit')
    chunk_specs = (
        compute_chunks_by_size(source_bytes, max_chunk_bytes, overlap)
        if max_chunk_bytes is not None
        else compute_chunks(total_pages, chunk_size, overlap)
    )
    if len(chunk_specs) > max_segments:
        raise PdfLimitError('Source PDF creates too many chunks')
    return total_pages, chunk_specs, chunk_pdf(source_bytes, chunk_specs)


def prepare_splits(
    source_bytes: bytes,
    segments: List[dict],
    max_pages: int,
) -> tuple[int, List[bytes]]:
    """Validate page ownership and create final outputs in an isolated worker."""
    total_pages = get_page_count(source_bytes)
    if total_pages < 1:
        raise PdfLimitError('Source PDF has no pages')
    if total_pages > max_pages:
        raise PdfLimitError('Source PDF exceeds the configured page limit')

    page_claims = []
    for segment in segments:
        pages = segment['pages']
        file_name = segment['fileName']
        if any(page < 1 or page > total_pages for page in pages):
            raise PdfPageAssignmentError(f'{file_name} contains pages outside the source PDF')
        if len(pages) != len(set(pages)):
            raise PdfPageAssignmentError(f'{file_name} contains duplicate pages')
        page_claims.extend(pages)

    if len(page_claims) != len(set(page_claims)):
        raise PdfPageAssignmentError('A source page is assigned to multiple output documents')
    if set(page_claims) != set(range(1, total_pages + 1)):
        raise PdfPageAssignmentError('Every source page must be assigned exactly once')
    return total_pages, split_by_pages(source_bytes, segments)


def compute_chunks(total_pages: int, chunk_size: int = 8, overlap: int = 2) -> List[ChunkSpec]:
    """Mirror of pdfUtil.computeChunks (LWC). Overlapping 8-page chunks by default."""
    if not isinstance(total_pages, int) or total_pages < 1:
        return []
    chunk_size = max(1, chunk_size)
    overlap = max(0, overlap)
    if overlap >= chunk_size:
        overlap = chunk_size - 1

    stride = chunk_size - overlap
    chunks: List[ChunkSpec] = []
    chunk_index = 0
    start_page = 1
    while start_page <= total_pages:
        end_page = min(start_page + chunk_size - 1, total_pages)
        chunks.append(ChunkSpec(chunk_index=chunk_index, start_page=start_page, end_page=end_page))
        if end_page >= total_pages:
            break
        start_page += stride
        chunk_index += 1
    return chunks


def compute_chunks_by_size(source_bytes: bytes, max_chunk_bytes: int, overlap: int = 0) -> List[ChunkSpec]:
    """Pack contiguous page ranges by saved PDF byte size.

    max_chunk_bytes is treated as the primary boundary. If one page alone exceeds
    the limit, emit that single-page chunk so callers get a deterministic service
    response and can surface/provider-handle the oversized file.
    """
    if not isinstance(max_chunk_bytes, int) or max_chunk_bytes < 1:
        return []

    chunks: List[ChunkSpec] = []
    with pikepdf.open(io.BytesIO(source_bytes)) as source:
        total_pages = len(source.pages)
        if total_pages < 1:
            return []

        overlap = max(0, overlap)
        chunk_index = 0
        start_page = 1
        while start_page <= total_pages:
            end_page = _find_largest_chunk_end(source, start_page, total_pages, max_chunk_bytes)
            chunks.append(ChunkSpec(chunk_index=chunk_index, start_page=start_page, end_page=end_page))
            if end_page >= total_pages:
                break
            pages_in_chunk = end_page - start_page + 1
            safe_overlap = min(overlap, max(0, pages_in_chunk - 1))
            start_page = end_page - safe_overlap + 1
            chunk_index += 1
    return chunks


def _find_largest_chunk_end(source: pikepdf.Pdf, start_page: int, total_pages: int, max_chunk_bytes: int) -> int:
    best_end_page = start_page
    for end_page in range(start_page, total_pages + 1):
        candidate_bytes = _copy_page_range(source, start_page, end_page)
        if len(candidate_bytes) > max_chunk_bytes and end_page > start_page:
            return best_end_page
        best_end_page = end_page
        if len(candidate_bytes) > max_chunk_bytes:
            return end_page
    return best_end_page


def _copy_page_range(source: pikepdf.Pdf, start_page: int, end_page: int) -> bytes:
    with pikepdf.Pdf.new() as new_pdf:
        for page_idx in range(start_page - 1, end_page):
            new_pdf.pages.append(source.pages[page_idx])
        buf = io.BytesIO()
        new_pdf.save(buf)
        return buf.getvalue()


def chunk_pdf(source_bytes: bytes, chunks: List[ChunkSpec]) -> List[bytes]:
    """Build one sub-PDF per chunk. Returns raw bytes per chunk in chunk order."""
    out = []
    with pikepdf.open(io.BytesIO(source_bytes)) as source:
        for chunk in chunks:
            with pikepdf.Pdf.new() as new_pdf:
                for page_idx in range(chunk.start_page - 1, chunk.end_page):
                    new_pdf.pages.append(source.pages[page_idx])
                buf = io.BytesIO()
                new_pdf.save(buf)
                out.append(buf.getvalue())
    return out


def split_by_pages(source_bytes: bytes, segments: List[dict]) -> List[bytes]:
    """Slice the source PDF into one sub-PDF per segment using each segment's
    absolute page list. Non-contiguous pages (e.g. license front + back on
    scattered pages) work natively since pikepdf accepts arbitrary index lists."""
    out = []
    with pikepdf.open(io.BytesIO(source_bytes)) as source:
        for seg in segments:
            with pikepdf.Pdf.new() as new_pdf:
                for page_num in seg['pages']:
                    new_pdf.pages.append(source.pages[page_num - 1])
                buf = io.BytesIO()
                new_pdf.save(buf)
                out.append(buf.getvalue())
    return out


def get_page_count(source_bytes: bytes) -> int:
    """Page count of a PDF without copying it."""
    if b'%PDF-' not in source_bytes[:1024]:
        raise UnsafePdfError('Missing PDF header')
    try:
        with pikepdf.open(io.BytesIO(source_bytes)) as pdf:
            if pdf.is_encrypted:
                raise UnsafePdfError('Encrypted PDFs are not supported')
            _reject_active_content(pdf)
            return len(pdf.pages)
    except UnsafePdfError:
        raise
    except (pikepdf.PasswordError, pikepdf.PdfError) as error:
        raise UnsafePdfError('The PDF is malformed or password protected') from error


def _reject_active_content(pdf: pikepdf.Pdf) -> None:
    root = pdf.Root
    if '/OpenAction' in root or '/AA' in root:
        raise UnsafePdfError('PDF document actions are not supported')

    names = root.get('/Names')
    if names is not None and ('/JavaScript' in names or '/EmbeddedFiles' in names):
        raise UnsafePdfError('PDF scripts and embedded files are not supported')

    acro_form = root.get('/AcroForm')
    if acro_form is not None and '/XFA' in acro_form:
        raise UnsafePdfError('XFA forms are not supported')

    for obj in pdf.objects:
        try:
            action_type = obj.get('/S')
            annotation_type = obj.get('/Subtype')
        except (AttributeError, TypeError, ValueError):
            continue
        if action_type is not None and str(action_type) in UNSAFE_ACTIONS:
            raise UnsafePdfError('PDF active content is not supported')
        if annotation_type is not None and str(annotation_type) in UNSAFE_ANNOTATIONS:
            raise UnsafePdfError('PDF active content is not supported')
