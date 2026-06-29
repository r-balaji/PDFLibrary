import { request } from 'undici';

/**
 * Minimal logger interface both pino.Logger and FastifyBaseLogger satisfy.
 * Keeps the client decoupled from either library's type tree.
 */
export interface Loggerish {
  info(obj: object, msg?: string): void;
  warn(obj: object, msg?: string): void;
  error(obj: object, msg?: string): void;
  debug?(obj: object, msg?: string): void;
}

/**
 * Salesforce Files REST round-trips.
 *
 * The Apex caller sends us a short-lived access token + the org's instance URL
 * in every request body. We use them to talk DIRECTLY to Salesforce Files REST
 * for the byte transfer, so PDF bytes never go through the Apex callout payload
 * (which would otherwise be capped at ~12 MB).
 */

export interface SalesforceContext {
  instanceUrl: string;
  accessToken: string;
  apiVersion: string; // e.g. '60.0'
}

export interface UploadOptions {
  /** Title shown in Salesforce UI. */
  title: string;
  /** File name with extension. */
  fileName: string;
  /** Library (ContentWorkspace) Id, makes the file visible in Files. */
  firstPublishLocationId?: string;
}

export interface UploadResult {
  contentVersionId: string;
  contentDocumentId: string;
}

export class SalesforceFilesClient {
  constructor(private readonly ctx: SalesforceContext, private readonly log: Loggerish) {}

  /**
   * Download the binary body of a ContentVersion.
   */
  async downloadVersionData(contentVersionId: string): Promise<Buffer> {
    const url = `${this.ctx.instanceUrl}/services/data/v${this.ctx.apiVersion}/sobjects/ContentVersion/${contentVersionId}/VersionData`;
    const res = await request(url, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${this.ctx.accessToken}`,
      },
    });
    if (res.statusCode !== 200) {
      const body = await res.body.text();
      throw new Error(`Salesforce download failed (${res.statusCode}): ${body.slice(0, 200)}`);
    }
    const arrayBuffer = await res.body.arrayBuffer();
    return Buffer.from(arrayBuffer);
  }

  /**
   * Resolve the latest ContentVersion Id for a given ContentDocumentId.
   * Apex usually has the ContentVersionId already, but if it passes a
   * ContentDocumentId we need to fetch the latest version.
   */
  async resolveLatestVersion(contentDocumentId: string): Promise<string> {
    const soql = `SELECT Id FROM ContentVersion WHERE ContentDocumentId='${contentDocumentId}' AND IsLatest=true LIMIT 1`;
    const url = `${this.ctx.instanceUrl}/services/data/v${this.ctx.apiVersion}/query?q=${encodeURIComponent(soql)}`;
    const res = await request(url, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${this.ctx.accessToken}`,
      },
    });
    const body = await res.body.json() as { records?: Array<{ Id: string }> };
    if (res.statusCode !== 200 || !body.records || body.records.length === 0) {
      throw new Error(`No ContentVersion found for ContentDocument ${contentDocumentId}`);
    }
    return body.records[0].Id;
  }

  /**
   * Upload a new ContentVersion. Returns the new ContentVersionId and
   * ContentDocumentId. Uses multipart/form-data; the binary part is `VersionData`.
   *
   * If firstPublishLocationId is provided the file lands in that library; otherwise
   * it's owned by the running user and visible in their personal Files.
   */
  async uploadContentVersion(bytes: Buffer, opts: UploadOptions): Promise<UploadResult> {
    const boundary = `boundary_${Math.random().toString(16).slice(2)}_${Date.now()}`;
    const url = `${this.ctx.instanceUrl}/services/data/v${this.ctx.apiVersion}/sobjects/ContentVersion`;

    const meta: Record<string, string> = {
      Title: opts.title,
      PathOnClient: opts.fileName,
    };
    if (opts.firstPublishLocationId) {
      meta.FirstPublishLocationId = opts.firstPublishLocationId;
    }

    const multipart = this.buildMultipart(boundary, meta, bytes, opts.fileName);

    const res = await request(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.ctx.accessToken}`,
        'Content-Type': `multipart/form-data; boundary="${boundary}"`,
      },
      body: multipart,
    });

    const text = await res.body.text();
    if (res.statusCode !== 201) {
      throw new Error(`ContentVersion upload failed (${res.statusCode}): ${text.slice(0, 300)}`);
    }
    const created = JSON.parse(text) as { id: string };
    const cvId = created.id;
    const cdId = await this.getContentDocumentId(cvId);
    return { contentVersionId: cvId, contentDocumentId: cdId };
  }

  private async getContentDocumentId(contentVersionId: string): Promise<string> {
    const url = `${this.ctx.instanceUrl}/services/data/v${this.ctx.apiVersion}/query?q=` +
      encodeURIComponent(`SELECT ContentDocumentId FROM ContentVersion WHERE Id='${contentVersionId}'`);
    const res = await request(url, {
      method: 'GET',
      headers: { Authorization: `Bearer ${this.ctx.accessToken}` },
    });
    const body = await res.body.json() as { records?: Array<{ ContentDocumentId: string }> };
    if (res.statusCode !== 200 || !body.records || body.records.length === 0) {
      throw new Error(`Could not resolve ContentDocumentId for ${contentVersionId}`);
    }
    return body.records[0].ContentDocumentId;
  }

  /**
   * Create a ContentDocumentLink between a ContentDocument and any record.
   * Used to attach split outputs to the Loan_Application__c.
   */
  async linkToRecord(contentDocumentId: string, linkedEntityId: string, shareType: string = 'V'): Promise<void> {
    const url = `${this.ctx.instanceUrl}/services/data/v${this.ctx.apiVersion}/sobjects/ContentDocumentLink`;
    const res = await request(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.ctx.accessToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        ContentDocumentId: contentDocumentId,
        LinkedEntityId: linkedEntityId,
        ShareType: shareType,
        Visibility: 'AllUsers',
      }),
    });
    if (res.statusCode !== 201) {
      const body = await res.body.text();
      // Duplicate link is fine (link already exists); don't fail the whole job.
      if (body.includes('DUPLICATE_VALUE')) return;
      throw new Error(`ContentDocumentLink failed (${res.statusCode}): ${body.slice(0, 200)}`);
    }
  }

  private buildMultipart(
    boundary: string,
    meta: Record<string, string>,
    bytes: Buffer,
    fileName: string,
  ): Buffer {
    const parts: Buffer[] = [];

    // JSON metadata part
    parts.push(
      Buffer.from(
        `--${boundary}\r\n` +
        `Content-Disposition: form-data; name="entity_content"\r\n` +
        `Content-Type: application/json\r\n\r\n` +
        JSON.stringify(meta) + '\r\n',
      ),
    );

    // Binary VersionData part
    parts.push(
      Buffer.from(
        `--${boundary}\r\n` +
        `Content-Disposition: form-data; name="VersionData"; filename="${fileName}"\r\n` +
        `Content-Type: application/pdf\r\n\r\n`,
      ),
    );
    parts.push(bytes);
    parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));

    return Buffer.concat(parts);
  }
}
