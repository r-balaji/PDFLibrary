import io
from dataclasses import dataclass
from typing import List

import pikepdf


@dataclass
class ChunkSpec:
    chunk_index: int
    start_page: int  # 1-based
    end_page: int    # 1-based, inclusive


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

    source = pikepdf.open(io.BytesIO(source_bytes))
    total_pages = len(source.pages)
    if total_pages < 1:
        return []

    overlap = max(0, overlap)
    chunks: List[ChunkSpec] = []
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
    new_pdf = pikepdf.Pdf.new()
    for page_idx in range(start_page - 1, end_page):
        new_pdf.pages.append(source.pages[page_idx])
    buf = io.BytesIO()
    new_pdf.save(buf)
    return buf.getvalue()


def chunk_pdf(source_bytes: bytes, chunks: List[ChunkSpec]) -> List[bytes]:
    """Build one sub-PDF per chunk. Returns raw bytes per chunk in chunk order."""
    source = pikepdf.open(io.BytesIO(source_bytes))
    out = []
    for chunk in chunks:
        new_pdf = pikepdf.Pdf.new()
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
    source = pikepdf.open(io.BytesIO(source_bytes))
    out = []
    for seg in segments:
        new_pdf = pikepdf.Pdf.new()
        for page_num in seg['pages']:
            new_pdf.pages.append(source.pages[page_num - 1])
        buf = io.BytesIO()
        new_pdf.save(buf)
        out.append(buf.getvalue())
    return out


def get_page_count(source_bytes: bytes) -> int:
    """Page count of a PDF without copying it."""
    pdf = pikepdf.open(io.BytesIO(source_bytes))
    return len(pdf.pages)
