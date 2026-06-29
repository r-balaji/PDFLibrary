import io

import pikepdf
import pytest

from app.pdf_ops import chunk_pdf, compute_chunks, get_page_count, split_by_pages


def make_pdf(page_count: int) -> bytes:
    pdf = pikepdf.Pdf.new()
    for _ in range(page_count):
        page = pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name('/Page'),
            MediaBox=[0, 0, 612, 792],
            Resources=pikepdf.Dictionary(),
        ))
        pdf.pages.append(page)
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


class TestComputeChunks:
    def test_single_chunk_when_pages_lte_chunk_size(self):
        chunks = compute_chunks(5, 8, 2)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].start_page == 1
        assert chunks[0].end_page == 5

    def test_overlapping_chunks_for_larger_docs(self):
        chunks = compute_chunks(14, 8, 2)
        assert len(chunks) == 2
        assert chunks[0].start_page == 1 and chunks[0].end_page == 8
        assert chunks[1].start_page == 7 and chunks[1].end_page == 14

    def test_clamps_overlap_when_equal_to_chunk_size(self):
        chunks = compute_chunks(20, 8, 8)
        assert chunks[0].start_page == 1
        assert chunks[1].start_page == 2  # stride = 1

    def test_invalid_input(self):
        assert compute_chunks(0, 8, 2) == []
        assert compute_chunks(-1, 8, 2) == []
        assert compute_chunks(10, 0, 2) != []  # chunk_size clamped to 1


class TestGetPageCount:
    def test_returns_page_count(self):
        assert get_page_count(make_pdf(7)) == 7


class TestChunkPdf:
    def test_produces_n_pdfs_with_expected_page_counts(self):
        source = make_pdf(14)
        specs = compute_chunks(14, 8, 2)
        chunks = chunk_pdf(source, specs)
        assert len(chunks) == 2
        assert get_page_count(chunks[0]) == 8
        assert get_page_count(chunks[1]) == 8


class TestSplitByPages:
    def test_one_output_per_segment(self):
        source = make_pdf(10)
        segments = [
            {'pages': [1, 2, 3]},
            {'pages': [4, 5, 6]},
            {'pages': [7, 8, 9, 10]},
        ]
        outs = split_by_pages(source, segments)
        assert len(outs) == 3
        assert get_page_count(outs[0]) == 3
        assert get_page_count(outs[1]) == 3
        assert get_page_count(outs[2]) == 4

    def test_non_contiguous_pages(self):
        source = make_pdf(12)
        segments = [
            {'pages': [1, 10]},    # license front + back
            {'pages': [3, 4, 5]},  # bank statement
        ]
        outs = split_by_pages(source, segments)
        assert get_page_count(outs[0]) == 2
        assert get_page_count(outs[1]) == 3

    def test_preserves_page_order_as_given(self):
        source = make_pdf(5)
        outs = split_by_pages(source, [{'pages': [5, 1]}])
        assert get_page_count(outs[0]) == 2
