import asyncio

import pytest

from app.sf_client import RequestAuthorizationError, SalesforceContext, SalesforceFilesClient
import app.sf_client as sf_client_module


class StubSalesforceClient(SalesforceFilesClient):
    def __init__(self, responses):
        super().__init__(SalesforceContext(
            instance_url='https://example.my.salesforce.com',
            access_token='token',
            api_version='60.0',
        ))
        self.responses = list(responses)
        self.urls = []

    async def _get_json(self, url, params=None):
        self.urls.append((url, params))
        return self.responses.pop(0)


class FakeAsyncClient:
    responses = []
    request_count = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def request(self, method, url, **kwargs):
        self.__class__.request_count += 1
        return self.__class__.responses.pop(0)


def test_build_multipart_contains_metadata_and_pdf_bytes():
    client = SalesforceFilesClient(
        SalesforceContext(
            instance_url='https://example.my.salesforce.com',
            access_token='token',
            api_version='60.0',
        )
    )

    body = client._build_multipart(
        boundary='test-boundary',
        meta={'Title': 'Output', 'PathOnClient': 'Output.pdf'},
        data=b'%PDF-test-bytes',
        file_name='Output.pdf',
    )

    assert b'--test-boundary' in body
    assert b'name="entity_content"' in body
    assert b'"Title": "Output"' in body
    assert b'name="VersionData"; filename="Output.pdf"' in body
    assert b'Content-Type: application/pdf' in body
    assert b'%PDF-test-bytes' in body
    assert body.endswith(b'\r\n--test-boundary--\r\n')


def test_validate_job_context_supports_managed_namespace():
    client = StubSalesforceClient([
        {'organization_id': '00D000000000001AAA'},
        {'ContentDocumentId': '069000000000001AAA'},
        {
            'rdyai__Source_ContentDocument_Id__c': '069000000000001AAA',
            'rdyai__Library_Id__c': '058000000000001AAA',
            'rdyai__Target_Folder_Id__c': '07H000000000001AAA',
            'rdyai__Target_Record_Id__c': 'a97000000000001AAA',
        },
    ])

    asyncio.run(client.validate_job_context(
        '00D000000000001AAA',
        'rdyai__Split_Job__c',
        'a01000000000001AAA',
        '068000000000001AAA',
        '058000000000001AAA',
        '07H000000000001AAA',
        'a97000000000001AAA',
        True,
    ))

    assert '/sobjects/rdyai__Split_Job__c/a01000000000001AAA' in client.urls[2][0]


def test_validate_job_context_rejects_source_file_mismatch():
    client = StubSalesforceClient([
        {'organization_id': '00D000000000001AAA'},
        {'ContentDocumentId': '069000000000001AAA'},
        {
            'Source_ContentDocument_Id__c': '069000000000099AAA',
            'Library_Id__c': None,
            'Target_Folder_Id__c': None,
            'Target_Record_Id__c': None,
        },
    ])

    with pytest.raises(RequestAuthorizationError):
        asyncio.run(client.validate_job_context(
            '00D000000000001AAA',
            'Split_Job__c',
            'a01000000000001AAA',
            '068000000000001AAA',
            None,
        ))


def test_salesforce_get_retries_transient_statuses(monkeypatch):
    async def no_delay(seconds):
        return None

    FakeAsyncClient.request_count = 0
    FakeAsyncClient.responses = [
        sf_client_module.httpx.Response(503),
        sf_client_module.httpx.Response(429),
        sf_client_module.httpx.Response(200, json={'ok': True}),
    ]
    monkeypatch.setattr(sf_client_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(sf_client_module.asyncio, 'sleep', no_delay)
    client = SalesforceFilesClient(SalesforceContext(
        instance_url='https://example.my.salesforce.com',
        access_token='token',
        api_version='60.0',
    ))

    response = asyncio.run(client._request('GET', 'https://example.my.salesforce.com/test'))

    assert response.status_code == 200
    assert FakeAsyncClient.request_count == 3
