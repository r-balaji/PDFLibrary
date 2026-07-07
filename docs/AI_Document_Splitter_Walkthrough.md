# AI Document Splitter PDF Service Walkthrough

This document explains the headless PDF worker in this repo: what Apex sends to it, what it does with Salesforce Files, and how it fits into the larger AI Document Splitter pipeline.

## 1. What this service does

The Salesforce side owns classification, orchestration, job records, Prompt Builder calls, and user experience. This service owns only the binary PDF work that Apex cannot do safely:

1. Download a source PDF from Salesforce Files using `ContentVersion.VersionData`.
2. Count pages and optionally create overlapping chunk PDFs for AI classification.
3. Split the original source PDF into final output PDFs from page lists.
4. Upload generated PDFs back to Salesforce as `ContentVersion` records.
5. Optionally move outputs into a `ContentFolder` and link outputs to a Salesforce record.

The service never needs a browser session and never receives large PDF bytes from Apex. Apex sends IDs, metadata, and a short-lived Salesforce access token.

## 2. Big picture

```text
Salesforce Apex
  |
  | POST /v1/chunks or /v1/splits
  | Authorization: Bearer <PDF_SERVICE_API_KEY>
  | Body: job id, ContentVersion id, Salesforce instance URL, access token, metadata
  v
FastAPI service
  |
  | GET /services/data/vXX.X/sobjects/ContentVersion/<id>/VersionData
  v
Salesforce Files
  |
  | raw PDF bytes
  v
pikepdf in memory
  |
  | generated chunk or split PDFs
  v
Salesforce Files
  |
  | POST /services/data/vXX.X/sobjects/ContentVersion
  v
FastAPI service returns new ContentVersion and ContentDocument IDs to Apex
```

## 3. Authentication model

There are two auth layers:

| Layer | Purpose |
|---|---|
| `Authorization: Bearer <PDF_SERVICE_API_KEY>` | Lets the service reject unknown callers. Configure this shared secret in Render and Salesforce. |
| `sfAccessToken` in the JSON body | Lets the service call back into the caller's Salesforce org for Files REST operations. The token should be short-lived and scoped to the running Salesforce context. |

`/` and `/healthz` are public. `/v1/chunks` and `/v1/splits` require the service API key.

## 4. `/v1/chunks`

Use this before AI classification when the source file is too large for a single Prompt Builder file input.

### Request shape

```json
{
  "jobId": "a01...",
  "sourceContentVersionId": "068...",
  "sfInstanceUrl": "https://example.my.salesforce.com",
  "sfAccessToken": "00D...",
  "sfApiVersion": "60.0",
  "libraryId": "058...",
  "maxChunkBytes": 10485760,
  "overlap": 0
}
```

### Processing

1. Download source bytes from `ContentVersion.VersionData`.
2. Reject the file if it exceeds `MAX_SOURCE_BYTES`.
3. Count PDF pages with pikepdf.
4. Compute chunks. When `maxChunkBytes` is present, chunks are packed by saved PDF byte size. Legacy `chunkSize` page-count chunking is used only when `maxChunkBytes` is absent.
5. Create each chunk PDF in memory.
6. Upload each chunk as `bundle_chunk_<index>.pdf`.

### Response shape

```json
{
  "totalPages": 14,
  "chunks": [
    {
      "chunkIndex": 0,
      "contentDocumentId": "069...",
      "contentVersionId": "068...",
      "pageOffset": 1,
      "pageCount": 8
    },
    {
      "chunkIndex": 1,
      "contentDocumentId": "069...",
      "contentVersionId": "068...",
      "pageOffset": 7,
      "pageCount": 8
    }
  ]
}
```

Apex uses `pageOffset` to translate chunk-local AI page numbers back into absolute source page numbers.

## 5. `/v1/splits`

Use this after classification and segment merging, when Apex knows which absolute source pages belong to each output document.

### Request shape

```json
{
  "jobId": "a01...",
  "sourceContentVersionId": "068...",
  "sfInstanceUrl": "https://example.my.salesforce.com",
  "sfAccessToken": "00D...",
  "sfApiVersion": "60.0",
  "libraryId": "058...",
  "targetFolderId": "05H...",
  "linkToRecordId": "a02...",
  "segments": [
    {
      "documentType": "BANK_STATEMENT",
      "sourceInstitution": "Chase",
      "namedParty": "Jane Smith",
      "instanceLabel": "January",
      "pages": [1, 3, 4],
      "fileName": "BankStatement_Chase_Jane_Smith.pdf"
    }
  ]
}
```

### Processing

1. Download source bytes from `ContentVersion.VersionData`.
2. Reject the file if it exceeds `MAX_SOURCE_BYTES`.
3. Count pages and validate that every requested page is within the source PDF.
4. Create one output PDF per segment using the segment's page list.
5. Upload each output as a `ContentVersion`.
6. If `targetFolderId` is present, update the auto-created `ContentFolderMember` to move the file into that folder.
7. If `linkToRecordId` is present, create a `ContentDocumentLink`.

Page lists are 1-based absolute source page numbers. They may be non-contiguous, so a driver's license front and back can be represented as `[1, 10]`.

### Response shape

```json
{
  "outputs": [
    {
      "fileName": "BankStatement_Chase_Jane_Smith.pdf",
      "contentDocumentId": "069...",
      "contentVersionId": "068...",
      "documentType": "BANK_STATEMENT",
      "pages": [1, 3, 4]
    }
  ]
}
```

## 6. Runtime configuration

| Env var | Default | Description |
|---|---:|---|
| `PDF_SERVICE_API_KEY` | none | Required shared secret for service callers. |
| `PORT` | `8080` | HTTP port. |
| `LOG_LEVEL` | `info` | Python logging level. |
| `WORKER_CONCURRENCY` | `2` | Max concurrent pikepdf work in the FastAPI process. |
| `MAX_SOURCE_BYTES` | `104857600` | Defensive cap for source PDFs. |

## 7. Important implementation notes

- PDF work happens in memory through pikepdf and is guarded by an asyncio semaphore.
- `ContentVersion` uploads use multipart/form-data with a JSON `entity_content` part and binary `VersionData` part.
- Moving a file into a Salesforce folder updates the existing `ContentFolderMember`; inserting another membership for the same file can hit Salesforce uniqueness constraints.
- Duplicate `ContentDocumentLink` errors are treated as success because the desired link already exists.
- Route tests use fake Salesforce clients, so the local test suite never reaches the network.

## 8. Verification

Run local tests:

```bash
python3 -m pytest tests/ -v
```

For a Salesforce sandbox smoke test:

1. Deploy the service with a known `PDF_SERVICE_API_KEY`.
2. Configure the Salesforce Named Credential or callout code with the service URL and key.
3. Send a known multi-page PDF through `/v1/chunks` and verify generated `bundle_chunk_*.pdf` files.
4. Send known segments through `/v1/splits` and verify output files, folder placement, record links, and page counts.
5. Confirm large source files are rejected above `MAX_SOURCE_BYTES` with HTTP 413.
