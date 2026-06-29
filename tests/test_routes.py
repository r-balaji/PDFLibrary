import io
import os

import pikepdf
from fastapi.testclient import TestClient

os.environ.setdefault('PDF_SERVICE_API_KEY', 'dev-secret')

from app.main import app  # noqa: E402
from app.sf_client import UploadResult  # noqa: E402
import app.routes.chunks as chunks_module  # noqa: E402
import app.routes.splits as splits_module  # noqa: E402


AUTH_HEADERS = {'Authorization': 'Bearer dev-secret'}


def make_pdf(page_count: int) -> bytes:
    pdf = pikepdf.Pdf.new()
    for _ in range(page_count):
        page = pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name('/Page'),
            MediaBox=[0, 0, 612, 792],
            Resources=pikepdf.Dictionary(),
        ))
        pdf.pages.append(page)
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


class FakeChunksSalesforceClient:
    uploads = []

    def __init__(self, ctx, log):
        self.ctx = ctx
        self.log = log

    async def download_version_data(self, content_version_id):
        assert content_version_id == 'source-cv'
        return make_pdf(14)

    async def upload_content_version(self, data, opts):
        self.__class__.uploads.append((data, opts))
        index = len(self.__class__.uploads)
        return UploadResult(
            content_version_id=f'chunk-cv-{index}',
            content_document_id=f'chunk-cd-{index}',
        )


class FakeSplitsSalesforceClient:
    uploads = []
    moves = []
    links = []

    def __init__(self, ctx, log):
        self.ctx = ctx
        self.log = log

    async def download_version_data(self, content_version_id):
        assert content_version_id == 'source-cv'
        return make_pdf(5)

    async def upload_content_version(self, data, opts):
        self.__class__.uploads.append((data, opts))
        index = len(self.__class__.uploads)
        return UploadResult(
            content_version_id=f'split-cv-{index}',
            content_document_id=f'split-cd-{index}',
        )

    async def move_to_folder(self, content_document_id, target_folder_id):
        self.__class__.moves.append((content_document_id, target_folder_id))

    async def link_to_record(self, content_document_id, linked_entity_id):
        self.__class__.links.append((content_document_id, linked_entity_id))


def test_healthz_is_public():
    with TestClient(app) as client:
        response = client.get('/healthz')

    assert response.status_code == 200
    assert response.json()['ok'] is True


def test_auth_required_for_work_routes():
    with TestClient(app) as client:
        response = client.post('/v1/chunks', json={})

    assert response.status_code == 401
    assert response.json() == {'error': 'Unauthorized'}


def test_chunks_route_uploads_overlapping_chunks(monkeypatch):
    FakeChunksSalesforceClient.uploads = []
    monkeypatch.setattr(chunks_module, 'SalesforceFilesClient', FakeChunksSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': 'job-1',
            'sourceContentVersionId': 'source-cv',
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': 'token',
            'libraryId': 'library-id',
        })

    assert response.status_code == 200
    assert response.json() == {
        'totalPages': 14,
        'chunks': [
            {
                'chunkIndex': 0,
                'contentDocumentId': 'chunk-cd-1',
                'contentVersionId': 'chunk-cv-1',
                'pageOffset': 1,
                'pageCount': 8,
            },
            {
                'chunkIndex': 1,
                'contentDocumentId': 'chunk-cd-2',
                'contentVersionId': 'chunk-cv-2',
                'pageOffset': 7,
                'pageCount': 8,
            },
        ],
    }
    assert [upload[1].file_name for upload in FakeChunksSalesforceClient.uploads] == [
        'bundle_chunk_0.pdf',
        'bundle_chunk_1.pdf',
    ]
    assert all(upload[1].first_publish_location_id == 'library-id' for upload in FakeChunksSalesforceClient.uploads)


def test_chunks_route_preserves_explicit_zero_overlap(monkeypatch):
    FakeChunksSalesforceClient.uploads = []
    monkeypatch.setattr(chunks_module, 'SalesforceFilesClient', FakeChunksSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': 'job-1',
            'sourceContentVersionId': 'source-cv',
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': 'token',
            'chunkSize': 8,
            'overlap': 0,
        })

    assert response.status_code == 200
    chunks = response.json()['chunks']
    assert [(chunk['pageOffset'], chunk['pageCount']) for chunk in chunks] == [(1, 8), (9, 6)]


def test_splits_route_uploads_moves_and_links_outputs(monkeypatch):
    FakeSplitsSalesforceClient.uploads = []
    FakeSplitsSalesforceClient.moves = []
    FakeSplitsSalesforceClient.links = []
    monkeypatch.setattr(splits_module, 'SalesforceFilesClient', FakeSplitsSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/splits', headers=AUTH_HEADERS, json={
            'jobId': 'job-2',
            'sourceContentVersionId': 'source-cv',
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': 'token',
            'libraryId': 'library-id',
            'targetFolderId': 'folder-id',
            'linkToRecordId': 'loan-id',
            'segments': [
                {
                    'documentType': 'BANK_STATEMENT',
                    'pages': [1, 3],
                    'fileName': 'BankStatement_Chase.pdf',
                },
                {
                    'documentType': 'DRIVERS_LICENSE',
                    'pages': [2],
                    'fileName': 'DriversLicense.PDF',
                },
            ],
        })

    assert response.status_code == 200
    assert response.json()['outputs'] == [
        {
            'fileName': 'BankStatement_Chase.pdf',
            'contentDocumentId': 'split-cd-1',
            'contentVersionId': 'split-cv-1',
            'documentType': 'BANK_STATEMENT',
            'pages': [1, 3],
        },
        {
            'fileName': 'DriversLicense.PDF',
            'contentDocumentId': 'split-cd-2',
            'contentVersionId': 'split-cv-2',
            'documentType': 'DRIVERS_LICENSE',
            'pages': [2],
        },
    ]
    assert [upload[1].title for upload in FakeSplitsSalesforceClient.uploads] == [
        'BankStatement_Chase',
        'DriversLicense',
    ]
    assert FakeSplitsSalesforceClient.moves == [('split-cd-1', 'folder-id'), ('split-cd-2', 'folder-id')]
    assert FakeSplitsSalesforceClient.links == [('split-cd-1', 'loan-id'), ('split-cd-2', 'loan-id')]


def test_splits_route_rejects_pages_outside_source(monkeypatch):
    monkeypatch.setattr(splits_module, 'SalesforceFilesClient', FakeSplitsSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/splits', headers=AUTH_HEADERS, json={
            'jobId': 'job-2',
            'sourceContentVersionId': 'source-cv',
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': 'token',
            'segments': [
                {
                    'documentType': 'BANK_STATEMENT',
                    'pages': [1, 6],
                    'fileName': 'BankStatement_Chase.pdf',
                },
            ],
        })

    assert response.status_code == 422
    assert response.json()['detail'] == 'BankStatement_Chase.pdf has page(s) outside 1..5: [6]'
