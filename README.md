# pdf-lib-service

Headless PDF chunking and slicing service for the AI Document Splitter. Apex calls this service through a Named Credential; the service then talks directly to Salesforce Files REST for the actual byte transfer.

This repo is the external PDF worker for the Salesforce AI Document Splitter flow. It uses FastAPI for HTTP, pikepdf for PDF page operations, and Salesforce REST APIs for `ContentVersion` download/upload.

## Why this exists

Apex cannot copy arbitrary pages out of an existing PDF, and pushing PDF bytes through Apex callouts runs into practical payload limits. This service keeps Apex requests small: Apex sends IDs, a short-lived Salesforce access token, and split metadata; the service downloads and uploads the bytes directly from Salesforce Files.

## Endpoints

All endpoints except `/` and `/healthz` require:

```http
Authorization: Bearer <PDF_SERVICE_API_KEY>
```

### `POST /v1/chunks`

Downloads a source `ContentVersion`, splits it into chunks, uploads each chunk as a new `ContentVersion`, and returns the new file IDs plus the page offset for each chunk.

Send `maxChunkBytes` to pack each chunk up to a byte-size target. `chunkSize` remains supported only as a legacy fallback when `maxChunkBytes` is absent.

### `POST /v1/splits`

Downloads a source `ContentVersion`, slices it into one output PDF per segment, uploads each output as a new `ContentVersion`, and optionally moves each output into a target folder and links it to a record.

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
```

## Configuration

| Env var | Required | Default | Notes |
|---|---:|---:|---|
| `PDF_SERVICE_API_KEY` | yes | none | Shared secret expected in the `Authorization` header. |
| `PORT` | no | `8080` | Used by Render/Docker. |
| `LOG_LEVEL` | no | `info` | Python logging level. |
| `WORKER_CONCURRENCY` | no | `2` | Max concurrent PDF CPU work inside this process. |
| `MAX_SOURCE_BYTES` | no | `104857600` | Defensive source PDF size cap. |

## Deploy

The service is Render-ready through `render.yaml` and Docker:

```bash
docker build -t pdf-lib-service .
docker run --rm -p 8080:8080 -e PDF_SERVICE_API_KEY=dev-secret pdf-lib-service
```

After Render deploys:

```bash
curl https://your-service.onrender.com/healthz
```

Copy the generated `PDF_SERVICE_API_KEY` from Render into the Salesforce Named Credential or callout configuration.

## Tests

```bash
python3 -m pytest tests/ -v
```

The test suite covers the pure PDF page operations plus the FastAPI auth, chunk, split, and validation paths with a fake Salesforce client. Live Salesforce REST round-trips should be smoke-tested against a sandbox.
