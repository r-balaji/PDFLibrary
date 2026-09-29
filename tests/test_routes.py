import io
import os

import pikepdf
from fastapi.testclient import TestClient

os.environ.setdefault('PDF_SERVICE_API_KEY', 'dev-secret')

from app.main import app  # noqa: E402
from app.sf_client import RequestAuthorizationError, UploadResult  # noqa: E402
import app.routes.chunks as chunks_module  # noqa: E402
import app.routes.splits as splits_module  # noqa: E402


AUTH_HEADERS = {'Authorization': 'Bearer dev-secret'}
JOB_ID = 'a01000000000001AAA'
SECOND_JOB_ID = 'a01000000000002AAA'
SOURCE_VERSION_ID = '068000000000001AAA'
LIBRARY_ID = '058000000000001AAA'
FOLDER_ID = '07H000000000001AAA'
TARGET_RECORD_ID = 'a97000000000001AAA'
SALESFORCE_TOKEN = 'test-salesforce-access-token'
SALESFORCE_ORG_ID = '00D000000000001AAA'
SPLIT_JOB_OBJECT = 'Split_Job__c'


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
    links = []

    def __init__(self, ctx, log):
        self.ctx = ctx
        self.log = log

    async def validate_job_context(self, *args):
        assert args[0] == SALESFORCE_ORG_ID
        assert args[1] == SPLIT_JOB_OBJECT

    async def download_version_data(self, content_version_id, max_bytes):
        assert content_version_id == SOURCE_VERSION_ID
        assert max_bytes > 0
        return make_pdf(14)

    async def upload_content_version(self, data, opts):
        self.__class__.uploads.append((data, opts))
        index = len(self.__class__.uploads)
        return UploadResult(
            content_version_id=f'chunk-cv-{index}',
            content_document_id=f'chunk-cd-{index}',
        )

    async def link_to_record(self, content_document_id, linked_entity_id):
        self.__class__.links.append((content_document_id, linked_entity_id))


class FakeSplitsSalesforceClient:
    uploads = []
    moves = []
    links = []

    def __init__(self, ctx, log):
        self.ctx = ctx
        self.log = log

    async def validate_job_context(self, *args):
        assert args[0] == SALESFORCE_ORG_ID
        assert args[1] == SPLIT_JOB_OBJECT

    async def download_version_data(self, content_version_id, max_bytes):
        assert content_version_id == SOURCE_VERSION_ID
        assert max_bytes > 0
        return make_pdf(3)

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


class FailingSplitsSalesforceClient(FakeSplitsSalesforceClient):
    upload_attempts = 0
    deleted = []

    async def upload_content_version(self, data, opts):
        self.__class__.upload_attempts += 1
        if self.__class__.upload_attempts == 2:
            raise RuntimeError('temporary upload failure')
        return UploadResult(
            content_version_id='cleanup-cv-1',
            content_document_id='cleanup-cd-1',
        )

    async def delete_content_documents(self, content_document_ids):
        self.__class__.deleted.extend(content_document_ids)


class UnauthorizedSalesforceClient(FakeChunksSalesforceClient):
    downloads = 0

    async def validate_job_context(self, *args):
        raise RequestAuthorizationError('mismatch')

    async def download_version_data(self, content_version_id, max_bytes):
        self.__class__.downloads += 1
        return await super().download_version_data(content_version_id, max_bytes)


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


def test_validation_errors_do_not_reflect_salesforce_token():
    rejected_token = 'tiny'
    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': rejected_token,
        })

    assert response.status_code == 422
    assert response.json() == {'detail': 'Request validation failed'}
    assert rejected_token not in response.text


def test_chunks_route_uploads_overlapping_chunks(monkeypatch):
    FakeChunksSalesforceClient.uploads = []
    FakeChunksSalesforceClient.links = []
    monkeypatch.setattr(chunks_module, 'SalesforceFilesClient', FakeChunksSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': SALESFORCE_TOKEN,
            'libraryId': LIBRARY_ID,
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
    assert all(upload[1].first_publish_location_id == LIBRARY_ID for upload in FakeChunksSalesforceClient.uploads)
    assert FakeChunksSalesforceClient.links == [('chunk-cd-1', JOB_ID), ('chunk-cd-2', JOB_ID)]


def test_chunks_route_preserves_explicit_zero_overlap(monkeypatch):
    FakeChunksSalesforceClient.uploads = []
    monkeypatch.setattr(chunks_module, 'SalesforceFilesClient', FakeChunksSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': SALESFORCE_TOKEN,
            'chunkSize': 8,
            'overlap': 0,
        })

    assert response.status_code == 200
    chunks = response.json()['chunks']
    assert [(chunk['pageOffset'], chunk['pageCount']) for chunk in chunks] == [(1, 8), (9, 6)]


def test_chunks_route_uses_max_chunk_bytes_when_present(monkeypatch):
    FakeChunksSalesforceClient.uploads = []
    monkeypatch.setattr(chunks_module, 'SalesforceFilesClient', FakeChunksSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': SALESFORCE_TOKEN,
            'chunkSize': 8,
            'maxChunkBytes': 10 * 1024 * 1024,
            'overlap': 0,
        })

    assert response.status_code == 200
    chunks = response.json()['chunks']
    assert [(chunk['pageOffset'], chunk['pageCount']) for chunk in chunks] == [(1, 14)]
    assert len(FakeChunksSalesforceClient.uploads) == 1


def test_splits_route_uploads_moves_and_links_outputs(monkeypatch):
    FakeSplitsSalesforceClient.uploads = []
    FakeSplitsSalesforceClient.moves = []
    FakeSplitsSalesforceClient.links = []
    monkeypatch.setattr(splits_module, 'SalesforceFilesClient', FakeSplitsSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/splits', headers=AUTH_HEADERS, json={
            'jobId': SECOND_JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': SALESFORCE_TOKEN,
            'libraryId': LIBRARY_ID,
            'targetFolderId': FOLDER_ID,
            'linkToRecordId': TARGET_RECORD_ID,
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
    assert FakeSplitsSalesforceClient.moves == [('split-cd-1', FOLDER_ID), ('split-cd-2', FOLDER_ID)]
    assert FakeSplitsSalesforceClient.links == [
        ('split-cd-1', TARGET_RECORD_ID),
        ('split-cd-1', SECOND_JOB_ID),
        ('split-cd-2', TARGET_RECORD_ID),
        ('split-cd-2', SECOND_JOB_ID),
    ]


def test_split_failure_cleans_up_created_outputs(monkeypatch):
    FailingSplitsSalesforceClient.upload_attempts = 0
    FailingSplitsSalesforceClient.deleted = []
    FailingSplitsSalesforceClient.moves = []
    FailingSplitsSalesforceClient.links = []
    monkeypatch.setattr(splits_module, 'SalesforceFilesClient', FailingSplitsSalesforceClient)
    payload = {
        'jobId': SECOND_JOB_ID,
        'sfOrgId': SALESFORCE_ORG_ID,
        'splitJobObjectApiName': SPLIT_JOB_OBJECT,
        'sourceContentVersionId': SOURCE_VERSION_ID,
        'sfInstanceUrl': 'https://example.my.salesforce.com',
        'sfAccessToken': SALESFORCE_TOKEN,
        'libraryId': LIBRARY_ID,
        'segments': [
            {'documentType': 'BANK_STATEMENT', 'pages': [1, 3], 'fileName': 'Bank.pdf'},
            {'documentType': 'DRIVERS_LICENSE', 'pages': [2], 'fileName': 'License.pdf'},
        ],
    }

    with TestClient(app) as client:
        response = client.post('/v1/splits', headers=AUTH_HEADERS, json=payload)

    assert response.status_code == 500
    assert FailingSplitsSalesforceClient.upload_attempts == 2
    assert FailingSplitsSalesforceClient.deleted == ['cleanup-cd-1']


def test_splits_route_rejects_pages_outside_source(monkeypatch):
    monkeypatch.setattr(splits_module, 'SalesforceFilesClient', FakeSplitsSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/splits', headers=AUTH_HEADERS, json={
            'jobId': SECOND_JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': SALESFORCE_TOKEN,
            'segments': [
                {
                    'documentType': 'BANK_STATEMENT',
                    'pages': [1, 6],
                    'fileName': 'BankStatement_Chase.pdf',
                },
            ],
        })

    assert response.status_code == 422
    assert response.json()['detail'] == 'BankStatement_Chase.pdf contains pages outside the source PDF'


def test_rejects_non_salesforce_callback_host():
    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://attacker.example.com',
            'sfAccessToken': SALESFORCE_TOKEN,
        })

    assert response.status_code == 422
    assert response.json()['detail'] == 'sfInstanceUrl must be an approved Salesforce HTTPS domain'


def test_rejects_job_context_mismatch_before_download(monkeypatch):
    UnauthorizedSalesforceClient.downloads = 0
    monkeypatch.setattr(chunks_module, 'SalesforceFilesClient', UnauthorizedSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/chunks', headers=AUTH_HEADERS, json={
            'jobId': JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': SALESFORCE_TOKEN,
        })

    assert response.status_code == 403
    assert UnauthorizedSalesforceClient.downloads == 0


def test_rejects_duplicate_page_claims(monkeypatch):
    monkeypatch.setattr(splits_module, 'SalesforceFilesClient', FakeSplitsSalesforceClient)

    with TestClient(app) as client:
        response = client.post('/v1/splits', headers=AUTH_HEADERS, json={
            'jobId': SECOND_JOB_ID,
            'sfOrgId': SALESFORCE_ORG_ID,
            'splitJobObjectApiName': SPLIT_JOB_OBJECT,
            'sourceContentVersionId': SOURCE_VERSION_ID,
            'sfInstanceUrl': 'https://example.my.salesforce.com',
            'sfAccessToken': SALESFORCE_TOKEN,
            'segments': [
                {'documentType': 'BANK_STATEMENT', 'pages': [1, 2], 'fileName': 'Bank.pdf'},
                {'documentType': 'OTHER', 'pages': [2, 3], 'fileName': 'Other.pdf'},
            ],
        })

    assert response.status_code == 422
    assert response.json()['detail'] == 'A source page is assigned to multiple output documents'
