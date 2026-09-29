import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.security import run_idempotent_request, run_pdf_operation, validate_pdf_filename, validate_salesforce_endpoint
from app.state_store import ClaimResult


ALLOWED_DOMAINS = ('my.salesforce.com',)


class RequestBody:
    sfOrgId = '00D000000000001AAA'
    jobId = 'a01000000000001AAA'

    def model_dump(self, exclude=None, mode=None):
        values = {'sfOrgId': self.sfOrgId, 'jobId': self.jobId, 'sfAccessToken': 'secret'}
        return {key: value for key, value in values.items() if key not in (exclude or set())}


class FakeStore:
    def __init__(self, claim):
        self.claim_result = claim
        self.completed = None
        self.failed = None

    async def claim(self, key, payload_hash):
        return self.claim_result

    async def complete(self, key, response):
        self.completed = (key, response)

    async def fail(self, key):
        self.failed = key


def test_accepts_salesforce_my_domain():
    assert validate_salesforce_endpoint(
        'https://example.my.salesforce.com',
        '60.0',
        ALLOWED_DOMAINS,
    ) == 'https://example.my.salesforce.com'


@pytest.mark.parametrize('url', [
    'http://example.my.salesforce.com',
    'https://attacker.example.com',
    'https://example.my.salesforce.com.attacker.example',
    'https://example.my.salesforce.com/path',
    'https://user@example.my.salesforce.com',
])
def test_rejects_unsafe_salesforce_endpoint(url):
    with pytest.raises(ValueError):
        validate_salesforce_endpoint(url, '60.0', ALLOWED_DOMAINS)


def test_rejects_invalid_salesforce_api_version():
    with pytest.raises(ValueError):
        validate_salesforce_endpoint('https://example.my.salesforce.com', '../latest', ALLOWED_DOMAINS)


@pytest.mark.parametrize('file_name', ['../secret.pdf', 'bad\r\nname.pdf', 'not-a-pdf.txt'])
def test_rejects_unsafe_pdf_filename(file_name):
    with pytest.raises(ValueError):
        validate_pdf_filename(file_name)


def test_pdf_operation_timeout_terminates_slow_worker():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(pdf_operation_timeout_seconds=0.01),
            ),
        ),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(run_pdf_operation(request, time.sleep, 1))

    assert error.value.status_code == 503


def test_idempotency_returns_completed_response_without_running_operation():
    key = '00D000000000001AAA:chunks:a01000000000001AAA'
    store = FakeStore(ClaimResult('complete', key, {'chunks': ['existing']}))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(idempotency_store=store)))
    operation_calls = []

    async def operation():
        operation_calls.append(True)
        return {'chunks': ['new']}

    response = asyncio.run(run_idempotent_request(request, 'chunks', RequestBody(), operation))

    assert response == {'chunks': ['existing']}
    assert operation_calls == []


def test_idempotency_records_completed_operation():
    key = '00D000000000001AAA:splits:a01000000000001AAA'
    store = FakeStore(ClaimResult('claimed', key))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(idempotency_store=store)))

    async def operation():
        return {'outputs': ['created']}

    response = asyncio.run(run_idempotent_request(request, 'splits', RequestBody(), operation))

    assert response == {'outputs': ['created']}
    assert store.completed == (key, response)
