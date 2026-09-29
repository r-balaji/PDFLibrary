# pdf-lib-service

Headless PDF chunking and slicing service for the AI Document Splitter. Apex calls this service through a Named Credential; the service then talks directly to Salesforce Files REST for the actual byte transfer.

This repo is the external PDF worker for the Salesforce AI Document Splitter flow. It uses FastAPI for HTTP, pikepdf for PDF page operations, and Salesforce REST APIs for `ContentVersion` download/upload.

## Why this exists

Apex cannot copy arbitrary pages out of an existing PDF, and pushing PDF bytes through Apex callouts runs into practical payload limits. This service keeps Apex requests small: Apex sends IDs, a short-lived Salesforce access token, and split metadata; the service downloads and uploads the bytes directly from Salesforce Files.

## Endpoints

All endpoints except `/`, `/healthz`, and `/readyz` require:

```http
Authorization: Bearer <PDF_SERVICE_API_KEY>
```

### `POST /v1/chunks`

Downloads a source `ContentVersion`, splits it into chunks, uploads each chunk as a new `ContentVersion`, and returns the new file IDs plus the page offset for each chunk.

Before downloading the file, the service verifies that the supplied Salesforce org, split job, source file, and library belong to the same request context.

Send `maxChunkBytes` to pack each chunk up to a byte-size target. `chunkSize` remains supported only as a legacy fallback when `maxChunkBytes` is absent.

### `POST /v1/splits`

Downloads a source `ContentVersion`, slices it into one output PDF per segment, uploads each output as a new `ContentVersion`, and optionally moves each output into a target folder and links it to a record.

The service verifies the destination folder and linked record against the split job before processing.

Segments use 1-based absolute page numbers:

```json
{
  "documentType": "BANK_STATEMENT",
  "pages": [1, 3, 4],
  "fileName": "BankStatement_Chase_Jane_Smith.pdf"
}
```

Non-contiguous page lists are supported.

## How the bytes move

```text
Apex -> tiny JSON -> service
                    |
                    |-> GET ContentVersion VersionData
                    |-> pikepdf chunk/split in memory
                    |-> POST new ContentVersion records

Apex <- tiny JSON <- service
```

PDF bytes are never written to disk by the application code.

## Local Dev

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
export PDF_SERVICE_API_KEY=dev-secret
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Health check:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
```

## Configuration

| Env var | Required | Default | Notes |
|---|---:|---:|---|
| `PDF_SERVICE_API_KEY` | yes* | none | Existing single shared secret expected in the `Authorization` header. |
| `PDF_SERVICE_API_KEYS` | yes* | none | Comma-separated current and next keys for zero-downtime rotation. Takes precedence over `PDF_SERVICE_API_KEY`. |
| `DATABASE_URL` | production | none | Render Postgres connection used only to prevent duplicate job operations and return completed responses. |
| `PORT` | no | `8080` | Used by Render/Docker. |
| `LOG_LEVEL` | no | `info` | Python logging level. |
| `WORKER_CONCURRENCY` | no | `2` | Max concurrent PDF CPU work inside this process. |
| `PDF_OPERATION_TIMEOUT_SECONDS` | no | `110` | Hard timeout for isolated PDF parsing/splitting work. |
| `MAX_SOURCE_BYTES` | no | `104857600` | Defensive source PDF size cap. |
| `MAX_REQUEST_BYTES` | no | `1048576` | Maximum JSON request body size. |
| `MAX_PAGES` | no | `2000` | Maximum pages accepted from one source PDF. |
| `MAX_SEGMENTS` | no | `500` | Maximum chunks or final output segments per request. |

`*` Configure either `PDF_SERVICE_API_KEY` or `PDF_SERVICE_API_KEYS`.

## Deploy

The Render Blueprint creates the web service and a small private Render Postgres database for 30-minute request idempotency. `/readyz` reports unhealthy when that database is unavailable.

```bash
docker build -t pdf-lib-service .
docker run --rm -p 8080:8080 -e PDF_SERVICE_API_KEY=dev-secret pdf-lib-service
```

After Render deploys:

```bash
curl https://your-service.onrender.com/healthz
```

Copy the generated `PDF_SERVICE_API_KEY` from Render into the Salesforce Named Credential or callout configuration. To rotate it, set `PDF_SERVICE_API_KEYS` to `old-key,new-key`, update Salesforce to the new key, then remove the old key.

## Tests

```bash
python3 -m pytest tests/ -v
```

The test suite covers PDF validation and page operations, request authorization, failure cleanup, retry behavior, and the chunk/split routes. Live Salesforce REST round-trips and Render Postgres connectivity should also be smoke-tested before production release.
