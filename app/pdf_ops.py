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
