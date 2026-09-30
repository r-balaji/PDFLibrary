import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class SalesforceContext:
    instance_url: str
    access_token: str
    api_version: str  # e.g. '60.0'


@dataclass
class UploadOptions:
    title: str
    file_name: str
    first_publish_location_id: Optional[str] = None


@dataclass
class UploadResult:
    content_version_id: str
    content_document_id: str


class SalesforceFilesClient:
    """Salesforce Files REST round-trips.

    The Apex caller sends a short-lived access token + instance URL in every
    request body. We use them to talk directly to Salesforce Files REST for
    byte transfer, bypassing the Apex callout payload cap (~12 MB).
    """

    def __init__(self, ctx: SalesforceContext, log: logging.Logger = None):
        self.ctx = ctx
        self.log = log or logging.getLogger(__name__)
        self._base = f'{ctx.instance_url}/services/data/v{ctx.api_version}'
        self._auth_header = {'Authorization': f'Bearer {ctx.access_token}'}

    async def download_version_data(self, content_version_id: str) -> bytes:
        url = f'{self._base}/sobjects/ContentVersion/{content_version_id}/VersionData'
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=self._auth_header, timeout=60.0)
        if res.status_code != 200:
            raise RuntimeError(f'Salesforce download failed ({res.status_code}): {res.text[:200]}')
        return res.content

    async def resolve_latest_version(self, content_document_id: str) -> str:
        soql = f"SELECT Id FROM ContentVersion WHERE ContentDocumentId='{content_document_id}' AND IsLatest=true LIMIT 1"
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f'{self._base}/query',
                headers=self._auth_header,
                params={'q': soql},
                timeout=30.0,
            )
        body = res.json()
        if res.status_code != 200 or not body.get('records'):
            raise RuntimeError(f'No ContentVersion found for ContentDocument {content_document_id}')
        return body['records'][0]['Id']

    async def upload_content_version(self, data: bytes, opts: UploadOptions) -> UploadResult:
        boundary = f'boundary_{random.randint(0, 0xFFFFFF):06x}_{int(time.time() * 1000)}'
        meta = {'Title': opts.title, 'PathOnClient': opts.file_name}
        if opts.first_publish_location_id:
            meta['FirstPublishLocationId'] = opts.first_publish_location_id

        body = self._build_multipart(boundary, meta, data, opts.file_name)
        headers = {
            **self._auth_header,
            'Content-Type': f'multipart/form-data; boundary="{boundary}"',
        }
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f'{self._base}/sobjects/ContentVersion',
                headers=headers,
                content=body,
                timeout=120.0,
            )
        if res.status_code != 201:
            raise RuntimeError(f'ContentVersion upload failed ({res.status_code}): {res.text[:300]}')
        cv_id = res.json()['id']
        cd_id = await self._get_content_document_id(cv_id)
        return UploadResult(content_version_id=cv_id, content_document_id=cd_id)

    async def _get_content_document_id(self, content_version_id: str) -> str:
        soql = f"SELECT ContentDocumentId FROM ContentVersion WHERE Id='{content_version_id}'"
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f'{self._base}/query',
                headers=self._auth_header,
                params={'q': soql},
                timeout=30.0,
            )
        body = res.json()
        if res.status_code != 200 or not body.get('records'):
            raise RuntimeError(f'Could not resolve ContentDocumentId for {content_version_id}')
        return body['records'][0]['ContentDocumentId']

    async def move_to_folder(self, content_document_id: str, target_folder_id: str) -> None:
        """Move a ContentDocument into a target folder by updating the
        auto-created ContentFolderMember (inserting a duplicate would fail
        on the uniqueness constraint, so we PATCH instead)."""
        soql = f"SELECT Id, ParentContentFolderId FROM ContentFolderMember WHERE ChildRecordId='{content_document_id}' LIMIT 1"
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f'{self._base}/query',
                headers=self._auth_header,
                params={'q': soql},
                timeout=30.0,
            )
        body = res.json()
        if res.status_code != 200 or not body.get('records'):
            self.log.warning('No ContentFolderMember found; file stays at library root')
            return
        member = body['records'][0]
        if member['ParentContentFolderId'] == target_folder_id:
            return
        async with httpx.AsyncClient() as client:
            patch_res = await client.patch(
                f'{self._base}/sobjects/ContentFolderMember/{member["Id"]}',
                headers={**self._auth_header, 'Content-Type': 'application/json'},
                content=json.dumps({'ParentContentFolderId': target_folder_id}),
                timeout=30.0,
            )
        if not (200 <= patch_res.status_code < 300):
            self.log.warning(
                'ContentFolderMember move failed; file stays at library root',
                extra={'status': patch_res.status_code},
            )

    async def link_to_record(
        self, content_document_id: str, linked_entity_id: str, share_type: str = 'V'
    ) -> None:
        """Create a ContentDocumentLink between a ContentDocument and any record."""
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f'{self._base}/sobjects/ContentDocumentLink',
                headers={**self._auth_header, 'Content-Type': 'application/json'},
                content=json.dumps({
                    'ContentDocumentId': content_document_id,
                    'LinkedEntityId': linked_entity_id,
                    'ShareType': share_type,
                    'Visibility': 'AllUsers',
                }),
                timeout=30.0,
            )
        if res.status_code != 201:
            if 'DUPLICATE_VALUE' in res.text:
                return  # link already exists; not an error
            raise RuntimeError(f'ContentDocumentLink failed ({res.status_code}): {res.text[:200]}')

    def _build_multipart(self, boundary: str, meta: dict, data: bytes, file_name: str) -> bytes:
        parts = []
        parts.append((
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="entity_content"\r\n'
            f'Content-Type: application/json\r\n\r\n'
            + json.dumps(meta) + '\r\n'
        ).encode())
        parts.append((
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="VersionData"; filename="{file_name}"\r\n'
            f'Content-Type: application/pdf\r\n\r\n'
        ).encode())
        parts.append(data)
        parts.append(f'\r\n--{boundary}--\r\n'.encode())
        return b''.join(parts)
