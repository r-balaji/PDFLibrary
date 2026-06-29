import { PDFDocument } from 'pdf-lib';

/**
 * Pure pdf-lib operations. No I/O, no Salesforce, no logging.
 *
 * These are the operations that today run in the LWC browser. Same semantics,
 * same defaults — the only difference is they run server-side in a Node
 * worker thread instead of in the user's Chrome process.
 */

export interface ChunkSpec {
  chunkIndex: number;
  startPage: number; // 1-based
  endPage: number;   // 1-based, inclusive
}

/**
 * Mirror of pdfUtil.computeChunks (LWC). Overlapping 8-page chunks by default.
 */
export function computeChunks(totalPages: number, chunkSize = 8, overlap = 2): ChunkSpec[] {
  if (!Number.isInteger(totalPages) || totalPages < 1) return [];
  if (chunkSize < 1) chunkSize = 1;
  if (overlap < 0) overlap = 0;
  if (overlap >= chunkSize) overlap = chunkSize - 1;

  const stride = chunkSize - overlap;
  const chunks: ChunkSpec[] = [];
  let chunkIndex = 0;
  let startPage = 1;
  while (startPage <= totalPages) {
    const endPage = Math.min(startPage + chunkSize - 1, totalPages);
    chunks.push({ chunkIndex, startPage, endPage });
    if (endPage >= totalPages) break;
    startPage += stride;
    chunkIndex += 1;
  }
  return chunks;
}

/**
 * Build one sub-PDF per chunk. Returns the raw bytes per chunk in chunk order.
 */
export async function chunkPdf(sourceBytes: Uint8Array, chunks: ChunkSpec[]): Promise<Uint8Array[]> {
  const sourceDoc = await PDFDocument.load(sourceBytes);
  const out: Uint8Array[] = [];
  for (const chunk of chunks) {
    const subDoc = await PDFDocument.create();
    const indices: number[] = [];
    for (let p = chunk.startPage - 1; p <= chunk.endPage - 1; p++) {
      indices.push(p);
    }
    const copied = await subDoc.copyPages(sourceDoc, indices);
    copied.forEach((page) => subDoc.addPage(page));
    out.push(await subDoc.save());
  }
  return out;
}

/**
 * Slice the source PDF into one sub-PDF per segment, using each segment's
 * absolute page list. pdf-lib's copyPages accepts arbitrary indices, so
 * non-contiguous pages (license front + back on scattered pages) just work.
 */
export async function splitByPages(
  sourceBytes: Uint8Array,
  segments: Array<{ pages: number[] }>,
): Promise<Uint8Array[]> {
  const sourceDoc = await PDFDocument.load(sourceBytes);
  const out: Uint8Array[] = [];
  for (const seg of segments) {
    const subDoc = await PDFDocument.create();
    const indices = seg.pages.map((p) => p - 1);
    const copied = await subDoc.copyPages(sourceDoc, indices);
    copied.forEach((page) => subDoc.addPage(page));
    out.push(await subDoc.save());
  }
  return out;
}

/**
 * Page count of a PDF without copying it.
 */
export async function getPageCount(sourceBytes: Uint8Array): Promise<number> {
  const doc = await PDFDocument.load(sourceBytes);
  return doc.getPageCount();
}
