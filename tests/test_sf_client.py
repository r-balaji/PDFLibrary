import asyncio
import json

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


class FakeUploadResponse:
    status_code = 201

    def json(self):
        return {'id': '068-test'}


class FakeUploadClient:
    last_post = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, **kwargs):
        self.__class__.last_post = (url, kwargs)
        return FakeUploadResponse()


def test_upload_content_version_uses_httpx_multipart(monkeypatch):
    client = SalesforceFilesClient(SalesforceContext('https://example.my.salesforce.com', 'token', '60.0'))

    async def get_content_document_id(content_version_id):
        assert content_version_id == '068-test'
        return '069-test'

    monkeypatch.setattr(sf_client_module.httpx, 'AsyncClient', FakeUploadClient)
    monkeypatch.setattr(client, '_get_content_document_id', get_content_document_id)

    result = asyncio.run(client.upload_content_version(
        b'%PDF-test-bytes',
        sf_client_module.UploadOptions('Output', 'Output.pdf', '058-test'),
    ))

    url, request = FakeUploadClient.last_post
    metadata_part = request['files']['entity_content']
    pdf_part = request['files']['VersionData']

    assert url.endswith('/sobjects/ContentVersion')
    assert json.loads(metadata_part[1]) == {
        'Title': 'Output',
        'PathOnClient': 'Output.pdf',
        'FirstPublishLocationId': '058-test',
    }
    assert pdf_part == ('Output.pdf', b'%PDF-test-bytes', 'application/pdf')
    assert request['headers'] == {'Authorization': 'Bearer token'}
    assert result.content_version_id == '068-test'
    assert result.content_document_id == '069-test'


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
