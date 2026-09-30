import json
import logging
from dataclasses import dataclass

import httpx


QUERY_TIMEOUT_SECONDS = 30.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
UPLOAD_TIMEOUT_SECONDS = 120.0


@dataclass
class SalesforceContext:
    instance_url: str
    access_token: str
    api_version: str  # e.g. '60.0'


@dataclass
class UploadOptions:
    title: str
    file_name: str
    first_publish_location_id: str | None = None


@dataclass
class UploadResult:
    content_version_id: str
    content_document_id: str


class SalesforceFilesClient:
    """Download, upload, and place Salesforce Files through the REST API."""

    def __init__(self, ctx: SalesforceContext, log: logging.Logger | None = None):
        self.ctx = ctx
        self.log = log or logging.getLogger(__name__)
        self._base_url = f'{ctx.instance_url}/services/data/v{ctx.api_version}'
        self._auth_header = {'Authorization': f'Bearer {ctx.access_token}'}

    async def download_version_data(self, content_version_id: str) -> bytes:
        url = f'{self._base_url}/sobjects/ContentVersion/{content_version_id}/VersionData'
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self._auth_header, timeout=DOWNLOAD_TIMEOUT_SECONDS)

        if response.status_code != 200:
            raise RuntimeError(f'Salesforce download failed ({response.status_code}): {response.text[:200]}')

        return response.content

    async def resolve_latest_version(self, content_document_id: str) -> str:
        soql = f"SELECT Id FROM ContentVersion WHERE ContentDocumentId='{content_document_id}' AND IsLatest=true LIMIT 1"
        records = await self._query_records(soql)

        if not records:
            raise RuntimeError(f'No ContentVersion found for ContentDocument {content_document_id}')

        return records[0]['Id']

    async def upload_content_version(self, data: bytes, opts: UploadOptions) -> UploadResult:
        metadata = {'Title': opts.title, 'PathOnClient': opts.file_name}
        if opts.first_publish_location_id:
            metadata['FirstPublishLocationId'] = opts.first_publish_location_id

        multipart_files = {
            'entity_content': (None, json.dumps(metadata), 'application/json'),
            'VersionData': (opts.file_name, data, 'application/pdf'),
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f'{self._base_url}/sobjects/ContentVersion',
                headers=self._auth_header,
                files=multipart_files,
                timeout=UPLOAD_TIMEOUT_SECONDS,
            )

        if response.status_code != 201:
            raise RuntimeError(f'ContentVersion upload failed ({response.status_code}): {response.text[:300]}')

        content_version_id = response.json()['id']
        content_document_id = await self._get_content_document_id(content_version_id)
        return UploadResult(content_version_id=content_version_id, content_document_id=content_document_id)

    async def _get_content_document_id(self, content_version_id: str) -> str:
        soql = f"SELECT ContentDocumentId FROM ContentVersion WHERE Id='{content_version_id}'"
        records = await self._query_records(soql)

        if not records:
            raise RuntimeError(f'Could not resolve ContentDocumentId for {content_version_id}')

        return records[0]['ContentDocumentId']

    async def move_to_folder(self, content_document_id: str, target_folder_id: str) -> None:
        soql = f"SELECT Id, ParentContentFolderId FROM ContentFolderMember WHERE ChildRecordId='{content_document_id}' LIMIT 1"
        records = await self._query_records(soql)

        if not records:
            self.log.warning('No ContentFolderMember found; file stays at library root')
            return

        folder_member = records[0]
        if folder_member['ParentContentFolderId'] == target_folder_id:
            return

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f'{self._base_url}/sobjects/ContentFolderMember/{folder_member["Id"]}',
                headers=self._auth_header,
                json={'ParentContentFolderId': target_folder_id},
                timeout=QUERY_TIMEOUT_SECONDS,
            )

        if not 200 <= response.status_code < 300:
            self.log.warning('ContentFolderMember move failed; file stays at library root', extra={'status': response.status_code})

    async def link_to_record(self, content_document_id: str, linked_entity_id: str, share_type: str = 'V') -> None:
        link = {
            'ContentDocumentId': content_document_id,
            'LinkedEntityId': linked_entity_id,
            'ShareType': share_type,
            'Visibility': 'AllUsers',
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f'{self._base_url}/sobjects/ContentDocumentLink',
                headers=self._auth_header,
                json=link,
                timeout=QUERY_TIMEOUT_SECONDS,
            )

        if response.status_code == 201 or 'DUPLICATE_VALUE' in response.text:
            return

        raise RuntimeError(f'ContentDocumentLink failed ({response.status_code}): {response.text[:200]}')

    async def _query_records(self, soql: str) -> list[dict]:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f'{self._base_url}/query',
                headers=self._auth_header,
                params={'q': soql},
                timeout=QUERY_TIMEOUT_SECONDS,
            )

        if response.status_code != 200:
            raise RuntimeError(f'Salesforce query failed ({response.status_code}): {response.text[:200]}')

        return response.json().get('records', [])
