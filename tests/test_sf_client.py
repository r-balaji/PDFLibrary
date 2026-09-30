import asyncio
import json

import app.sf_client as sf_client_module
from app.sf_client import SalesforceContext, SalesforceFilesClient, UploadOptions


class FakeResponse:
    status_code = 201
    text = ''

    def json(self):
        return {'id': '068-test'}


class FakeAsyncClient:
    last_post = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def post(self, url, **kwargs):
        FakeAsyncClient.last_post = (url, kwargs)
        return FakeResponse()


def test_upload_content_version_uses_httpx_multipart(monkeypatch):
    client = SalesforceFilesClient(SalesforceContext('https://example.my.salesforce.com', 'token', '60.0'))

    async def get_content_document_id(content_version_id):
        assert content_version_id == '068-test'
        return '069-test'

    monkeypatch.setattr(sf_client_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(client, '_get_content_document_id', get_content_document_id)

    result = asyncio.run(client.upload_content_version(
        b'%PDF-test-bytes',
        UploadOptions(title='Output', file_name='Output.pdf', first_publish_location_id='058-test'),
    ))

    url, request = FakeAsyncClient.last_post
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
