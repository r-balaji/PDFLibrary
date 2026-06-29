# pdf-lib-service

Headless PDF chunking and slicing service for the AI Document Splitter. Called from Apex via Named Credential; talks directly to Salesforce Files REST for byte transfer.

Companion to the Salesforce repo in `Originate Agents/`. See the splitter walkthrough at `Originate Agents/docs/AI_Document_Splitter_Walkthrough.md` for the end-to-end picture.

## Why this exists

The browser pdf-lib path (LWC) covers loan-officer UI flows but not the headless case where the loan application arrives via REST from a borrower portal — no browser, no pdf-lib. This service runs the same pdf-lib operations server-side so Apex can drive them from a Queueable.

## What it does

Two endpoints, both authed via a shared secret in the `Authorization: Bearer ...` header.

### POST /v1/chunks

Splits a source PDF (> 12 MB) into overlapping 8-page chunks, uploads each chunk to Salesforce as a new ContentVersion, returns the new IDs. Used as the first step of the headless flow for large bundles; Apex then enqueues one classify call per chunk against Salesforce Prompt Builder.

### POST /v1/splits

Given a source PDF and a list of segments (each with a `pages: [int]` array of absolute page numbers, contiguous or not), slices the source into one output PDF per segment and uploads each as a new ContentVersion in Salesforce. Optionally links each output to a record (e.g. `Loan_Application__c`).

## How the bytes move

Apex sends only IDs + a short-lived access token in the request body (a few KB). The service uses that token to talk directly to Salesforce's Files REST API for the actual byte transfer:

```
   Apex ───── tiny JSON ─────► Service
                                │
                                ├─► GET ContentVersion VersionData  (download source bytes)
                                ├─► pdf-lib chunk / slice           (in-memory, never on disk)
                                └─► POST ContentVersion             (upload result bytes)

   Apex ◄──── tiny JSON ────── Service
```

This sidesteps the Apex callout payload limit (~12 MB async). Source file size is bounded by Salesforce Files (2 GB), not Apex.

## Local dev

```bash
npm install
export PDF_SERVICE_API_KEY=dev-secret
npm run dev
```

The service listens on `http://localhost:8080`. Health check: `GET /healthz`.

## Deploy

Render-ready via `render.yaml`. Push the repo, point Render at it, the Starter plan auto-provisions for $7/mo.

```bash
# After Render deploys:
curl https://your-service.onrender.com/healthz
# → {"ok":true,"name":"pdf-lib-service","version":"0.1.0"}
```

Note the generated `PDF_SERVICE_API_KEY` from Render's env vars panel and configure it on the Salesforce Named Credential.

## Hosting model

Default: single shared instance for all customers (tenant isolation via Org Id in request bodies; bytes never persist).

Per-customer dedicated: same Docker image, separate Render service. Each customer's Salesforce Named Credential points to its dedicated URL. Use this for customers with hard data-isolation requirements; same code, isolated infrastructure.

## Tests

```bash
npm test
```

Vitest exercises the pure pdf-lib operations (`computeChunks`, `chunkPdf`, `splitByPages`). The Salesforce REST round-trips and route auth are best exercised against a sandbox; see the end-to-end smoke described in the splitter walkthrough.
