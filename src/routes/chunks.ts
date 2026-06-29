import type { FastifyInstance, FastifyRequest } from 'fastify';
import { SalesforceFilesClient } from '../sf/filesClient.js';
import type { SalesforceContext } from '../sf/filesClient.js';
import { chunkPdf, computeChunks, getPageCount } from '../lib/pdfOps.js';
import type { AppConfig } from '../config.js';

interface ChunkBody {
  jobId: string;
  sourceContentVersionId: string;
  sfInstanceUrl: string;
  sfAccessToken: string;
  sfApiVersion?: string;          // default 60.0
  libraryId?: string;             // optional: FirstPublishLocationId for chunk uploads
  chunkSize?: number;             // default 8
  overlap?: number;               // default 2
}

interface ChunkOutput {
  chunkIndex: number;
  contentDocumentId: string;
  contentVersionId: string;
  pageOffset: number;
  pageCount: number;
}

export async function registerChunksRoute(app: FastifyInstance, config: AppConfig) {
  app.post('/v1/chunks', async (req: FastifyRequest<{ Body: ChunkBody }>, reply) => {
    const body = req.body;
    if (!body || !body.jobId || !body.sourceContentVersionId || !body.sfInstanceUrl || !body.sfAccessToken) {
      return reply.code(400).send({ error: 'Missing required fields: jobId, sourceContentVersionId, sfInstanceUrl, sfAccessToken' });
    }

    const log = req.log.child({ jobId: body.jobId, route: 'chunks' });
    const ctx: SalesforceContext = {
      instanceUrl: body.sfInstanceUrl,
      accessToken: body.sfAccessToken,
      apiVersion: body.sfApiVersion ?? '60.0',
    };
    const sf = new SalesforceFilesClient(ctx, log);

    try {
      const sourceBytes = await sf.downloadVersionData(body.sourceContentVersionId);
      if (sourceBytes.length > config.maxSourceBytes) {
        return reply.code(413).send({ error: `Source file too large (${sourceBytes.length} bytes; max ${config.maxSourceBytes})` });
      }

      const totalPages = await getPageCount(sourceBytes);
      if (totalPages < 1) {
        return reply.code(422).send({ error: 'Source PDF has no pages' });
      }

      const chunkSize = body.chunkSize ?? 8;
      const overlap = body.overlap ?? 2;
      const chunkSpecs = computeChunks(totalPages, chunkSize, overlap);
      log.info({ totalPages, chunkCount: chunkSpecs.length }, 'computed chunks');

      const chunkBytes = await chunkPdf(sourceBytes, chunkSpecs);

      const outputs: ChunkOutput[] = [];
      for (let i = 0; i < chunkSpecs.length; i++) {
        const spec = chunkSpecs[i];
        const bytes = Buffer.from(chunkBytes[i]);
        const fileName = `bundle_chunk_${spec.chunkIndex}.pdf`;
        const up = await sf.uploadContentVersion(bytes, {
          title: `bundle_chunk_${spec.chunkIndex}`,
          fileName,
          firstPublishLocationId: body.libraryId,
        });
        outputs.push({
          chunkIndex: spec.chunkIndex,
          contentDocumentId: up.contentDocumentId,
          contentVersionId: up.contentVersionId,
          pageOffset: spec.startPage,
          pageCount: spec.endPage - spec.startPage + 1,
        });
      }

      log.info({ outputs: outputs.length }, 'chunks uploaded');
      return reply.send({ totalPages, chunks: outputs });
    } catch (err) {
      log.error({ err }, 'chunks failed');
      return reply.code(500).send({ error: (err as Error).message });
    }
  });
}
