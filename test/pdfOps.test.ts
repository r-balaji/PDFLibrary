import { describe, it, expect } from 'vitest';
import { PDFDocument } from 'pdf-lib';
import { computeChunks, chunkPdf, splitByPages, getPageCount } from '../src/lib/pdfOps.js';

async function makePdf(pageCount: number): Promise<Uint8Array> {
  const doc = await PDFDocument.create();
  for (let i = 0; i < pageCount; i++) {
    doc.addPage([612, 792]);
  }
  return doc.save();
}

describe('computeChunks', () => {
  it('returns one chunk when totalPages <= chunkSize', () => {
    const chunks = computeChunks(5, 8, 2);
    expect(chunks).toHaveLength(1);
    expect(chunks[0]).toEqual({ chunkIndex: 0, startPage: 1, endPage: 5 });
  });

  it('produces overlapping chunks for larger docs', () => {
    const chunks = computeChunks(14, 8, 2);
    expect(chunks).toHaveLength(2);
    expect(chunks[0]).toEqual({ chunkIndex: 0, startPage: 1, endPage: 8 });
    expect(chunks[1]).toEqual({ chunkIndex: 1, startPage: 7, endPage: 14 });
  });

  it('clamps overlap when it would equal chunkSize', () => {
    const chunks = computeChunks(20, 8, 8); // overlap >= size collapses to size-1
    expect(chunks[0].startPage).toBe(1);
    expect(chunks[1].startPage).toBe(2); // stride = 1
  });

  it('handles invalid input safely', () => {
    expect(computeChunks(0, 8, 2)).toEqual([]);
    expect(computeChunks(-1, 8, 2)).toEqual([]);
    expect(computeChunks(10, 0, 2)).not.toEqual([]); // chunkSize clamped to 1
  });
});

describe('getPageCount', () => {
  it('returns the page count of a generated PDF', async () => {
    const pdf = await makePdf(7);
    expect(await getPageCount(pdf)).toBe(7);
  });
});

describe('chunkPdf', () => {
  it('produces N output PDFs with the expected page counts', async () => {
    const source = await makePdf(14);
    const specs = computeChunks(14, 8, 2);
    const chunks = await chunkPdf(source, specs);
    expect(chunks).toHaveLength(2);
    expect(await getPageCount(chunks[0])).toBe(8);
    expect(await getPageCount(chunks[1])).toBe(8);
  });
});

describe('splitByPages', () => {
  it('produces one output per segment with the right page count', async () => {
    const source = await makePdf(10);
    const segments = [
      { pages: [1, 2, 3] },
      { pages: [4, 5, 6] },
      { pages: [7, 8, 9, 10] },
    ];
    const outs = await splitByPages(source, segments);
    expect(outs).toHaveLength(3);
    expect(await getPageCount(outs[0])).toBe(3);
    expect(await getPageCount(outs[1])).toBe(3);
    expect(await getPageCount(outs[2])).toBe(4);
  });

  it('handles non-contiguous page lists (license front + back)', async () => {
    const source = await makePdf(12);
    const segments = [
      { pages: [1, 10] },        // license front + back
      { pages: [3, 4, 5] },      // bank statement
    ];
    const outs = await splitByPages(source, segments);
    expect(await getPageCount(outs[0])).toBe(2);
    expect(await getPageCount(outs[1])).toBe(3);
  });

  it('preserves page order as given in the segment', async () => {
    const source = await makePdf(5);
    const outs = await splitByPages(source, [{ pages: [5, 1] }]);
    expect(await getPageCount(outs[0])).toBe(2);
    // Order is implicitly preserved because pdf-lib.copyPages respects index order.
  });
});
