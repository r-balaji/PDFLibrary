import type { FastifyInstance, FastifyRequest } from 'fastify';
import { SalesforceFilesClient } from '../sf/filesClient.js';
import type { SalesforceContext } from '../sf/filesClient.js';
import { splitByPages } from '../lib/pdfOps.js';
import type { AppConfig } from '../config.js';

interface SegmentInput {
  documentType: string;
  sourceInstitution: string | null;
  namedParty: string | null;
  instanceLabel: string | null;
  pages: number[];                // 1-based absolute page numbers
  fileName: string;               // already built by Apex
}

interface SplitBody {
  jobId: string;
  sourceContentVersionId: string;
  sfInstanceUrl: string;
  sfAccessToken: string;
  sfApiVersion?: string;
  libraryId?: string;             // optional FirstPublishLocationId
  linkToRecordId?: string;        // optional Loan_Application__c (or any) to attach outputs to
  segments: SegmentInput[];
}

interface SplitOutput {
  fileName: string;
  contentDocumentId: string;
  contentVersionId: string;
  documentType: string;
  pages: number[];
}

export async function registerSplitsRoute(app: FastifyInstance, _config: AppConfig) {
  app.post('/v1/splits', async (req: FastifyRequest<{ Body: SplitBody }>, reply) => {
    const body = req.body;
    if (!body || !body.jobId || !body.sourceContentVersionId || !body.sfInstanceUrl || !body.sfAccessToken) {
      return reply.code(400).send({ error: 'Missing required fields: jobId, sourceContentVersionId, sfInstanceUrl, sfAccessToken' });
    }
    if (!Array.isArray(body.segments) || body.segments.length === 0) {
      return reply.code(400).send({ error: 'segments must be a non-empty array' });
    }

    const log = req.log.child({ jobId: body.jobId, route: 'splits' });
    const ctx: SalesforceContext = {
      instanceUrl: body.sfInstanceUrl,
      accessToken: body.sfAccessToken,
      apiVersion: body.sfApiVersion ?? '60.0',
    };
    const sf = new SalesforceFilesClient(ctx, log);

    try {
      const sourceBytes = await sf.downloadVersionData(body.sourceContentVersionId);
      const sliced = await splitByPages(sourceBytes, body.segments);

      const outputs: SplitOutput[] = [];
      for (let i = 0; i < body.segments.length; i++) {
        const seg = body.segments[i];
        const bytes = Buffer.from(sliced[i]);
        const titleBase = seg.fileName.replace(/\.pdf$/i, '');
        const up = await sf.uploadContentVersion(bytes, {
          title: titleBase,
          fileName: seg.fileName,
          firstPublishLocationId: body.libraryId,
        });
        if (body.linkToRecordId) {
          await sf.linkToRecord(up.contentDocumentId, body.linkToRecordId);
        }
        outputs.push({
          fileName: seg.fileName,
          contentDocumentId: up.contentDocumentId,
          contentVersionId: up.contentVersionId,
          documentType: seg.documentType,
          pages: seg.pages,
        });
      }

      log.info({ outputs: outputs.length }, 'splits uploaded');
      return reply.send({ outputs });
    } catch (err) {
      log.error({ err }, 'splits failed');
      return reply.code(500).send({ error: (err as Error).message });
    }
  });
}
