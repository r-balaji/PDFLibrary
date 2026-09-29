"""Security controls shared by the public PDF service routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from anyio import fail_after, to_process
from fastapi import HTTPException, Request

from .state_store import ClaimResult


SALESFORCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
SALESFORCE_API_VERSION_PATTERN = re.compile(r"^[0-9]{2,3}\.0$")
SPLIT_JOB_OBJECT_PATTERN = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_]*__)?Split_Job__c$")


def validate_salesforce_endpoint(instance_url: str, api_version: str, allowed_suffixes: tuple[str, ...]) -> str:
    """Allow outbound callbacks only to approved Salesforce HTTPS domains."""
    try:
        parsed = urlsplit(instance_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("sfInstanceUrl is invalid") from error

    hostname = (parsed.hostname or "").lower().rstrip(".")
    allowed_host = any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in allowed_suffixes)
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not allowed_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("sfInstanceUrl must be an approved Salesforce HTTPS domain")
    if not SALESFORCE_API_VERSION_PATTERN.fullmatch(api_version):
        raise ValueError("sfApiVersion is invalid")
    return f"https://{hostname}"


def validate_pdf_filename(file_name: str) -> str:
    """Reject control characters and path syntax in Salesforce upload names."""
    if (
        not file_name
        or len(file_name) > 255
        or not file_name.lower().endswith(".pdf")
        or any(character in file_name for character in ("/", "\\", "\r", "\n", "\x00"))
    ):
        raise ValueError("fileName must be a safe PDF filename")
    return file_name


def request_fingerprint(model, excluded_fields: set[str] | None = None) -> str:
    """Hash a normalized request without persisting transient credentials."""
    payload = model.model_dump(exclude=excluded_fields or set(), mode="json")
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


async def run_idempotent_request(request: Request, stage: str, body, operation):
    key = f"{body.sfOrgId}:{stage}:{body.jobId}"
    fingerprint = request_fingerprint(body, {"sfAccessToken"})
    store = request.app.state.idempotency_store
    claim = await store.claim(key, fingerprint) if store else ClaimResult('claimed', key)
    if claim.state == "conflict":
        raise HTTPException(status_code=409, detail="This job stage was already submitted with different inputs")
    if claim.state == "processing":
        raise HTTPException(status_code=409, detail="This job stage is already processing")
    if claim.state == "complete":
        return claim.response

    try:
        response = await operation()
    except Exception:
        if store:
            await store.fail(key)
        raise
    if store:
        await store.complete(key, response)
    return response


async def run_pdf_operation(request: Request, operation, *args):
    """Run native PDF parsing outside the web process and kill it on timeout."""
    try:
        with fail_after(request.app.state.config.pdf_operation_timeout_seconds):
            return await to_process.run_sync(operation, *args, cancellable=True)
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail='PDF processing timed out') from error


@asynccontextmanager
async def processing_slot(request: Request):
    """Bound whole-request memory and CPU usage, not only the PDF operation."""
    semaphore = request.app.state.processing_semaphore
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail="PDF service is busy; retry later") from error
    try:
        yield
    finally:
        semaphore.release()
