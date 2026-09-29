import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

import httpx


TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRY_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 30.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
UPLOAD_TIMEOUT_SECONDS = 120.0
SPLIT_JOB_OBJECT_SUFFIX = 'Split_Job__c'


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


class SourceFileTooLargeError(RuntimeError):
    """Raised when a Salesforce file exceeds the configured streaming limit."""


class RequestAuthorizationError(RuntimeError):
    """Raised when request IDs do not belong to the supplied Salesforce job."""


@dataclass(frozen=True)
class _SplitJobFields:
    source_document: str
    library: str
    target_folder: str
    target_record: str

    @classmethod
    def from_object_name(cls, object_api_name: str) -> '_SplitJobFields':
        if not object_api_name.endswith(SPLIT_JOB_OBJECT_SUFFIX):
            raise RequestAuthorizationError('Split job object name is invalid')

        namespace = object_api_name[:-len(SPLIT_JOB_OBJECT_SUFFIX)]
        return cls(
            source_document=f'{namespace}Source_ContentDocument_Id__c',
            library=f'{namespace}Library_Id__c',
            target_folder=f'{namespace}Target_Folder_Id__c',
            target_record=f'{namespace}Target_Record_Id__c',
        )

    def all(self) -> tuple[str, str, str, str]:
        return self.source_document, self.library, self.target_folder, self.target_record


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

    # Job authorization

    async def validate_job_context(self, sf_org_id: str, split_job_object_api_name: str, job_id: str, source_content_version_id: str, library_id: str | None, target_folder_id: str | None = None, target_record_id: str | None = None, validate_destination: bool = False) -> None:
        """Bind every request to one real org, split job, and source file."""
        await self._verify_organization(sf_org_id)
        source_document_id = await self._load_source_document_id(source_content_version_id)
        fields = _SplitJobFields.from_object_name(split_job_object_api_name)
        job = await self._load_split_job(split_job_object_api_name, job_id, fields)

        expected_ids = {
            fields.source_document: source_document_id,
            fields.library: library_id,
        }
        if validate_destination:
            expected_ids[fields.target_folder] = target_folder_id
            expected_ids[fields.target_record] = target_record_id

        self._verify_job_ids(job, expected_ids)

    async def _verify_organization(self, expected_org_id: str) -> None:
        user_info = await self._get_json(f'{self.ctx.instance_url}/services/oauth2/userinfo')
        if not self._same_id(user_info.get('organization_id'), expected_org_id):
            raise RequestAuthorizationError('Salesforce organization does not match the request')

    async def _load_source_document_id(self, content_version_id: str) -> str | None:
        version = await self._get_json(
            f'{self._base}/sobjects/ContentVersion/{content_version_id}',
            params={'fields': 'ContentDocumentId'},
        )
        return version.get('ContentDocumentId')

    async def _load_split_job(self, object_api_name: str, job_id: str, fields: _SplitJobFields) -> dict:
        return await self._get_json(
            f'{self._base}/sobjects/{quote(object_api_name, safe="")}/{job_id}',
            params={'fields': ','.join(fields.all())},
        )

    def _verify_job_ids(self, job: dict, expected_ids: dict[str, str | None]) -> None:
        for field_name, expected_id in expected_ids.items():
            if not self._same_nullable_id(job.get(field_name), expected_id):
                raise RequestAuthorizationError('Request IDs do not match the Salesforce split job')

    # File download

    async def download_version_data(self, content_version_id: str, max_bytes: int) -> bytes:
        url = f'{self._base}/sobjects/ContentVersion/{content_version_id}/VersionData'
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                content = await self._download_once(url, max_bytes, attempt)
                if content is not None:
                    return content
            except httpx.RequestError:
                if self._is_last_attempt(attempt):
                    raise RuntimeError('Salesforce download failed after retries') from None
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError('Salesforce download failed after retries')

    async def _download_once(self, url: str, max_bytes: int, attempt: int) -> bytes | None:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            async with client.stream('GET', url, headers=self._auth_header, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                if self._should_retry(response, attempt):
                    await self._retry_delay(response, attempt)
                    return None
                if response.status_code != 200:
                    raise RuntimeError(f'Salesforce download failed with status {response.status_code}')

                self._verify_content_length(response, max_bytes)
                return await self._read_limited_content(response, max_bytes)

    @staticmethod
    def _verify_content_length(response: httpx.Response, max_bytes: int) -> None:
        declared_size = response.headers.get('content-length')
        if not declared_size:
            return
        try:
            declared_size_bytes = int(declared_size)
        except ValueError as error:
            raise RuntimeError('Salesforce returned an invalid Content-Length') from error
        if declared_size_bytes > max_bytes:
            raise SourceFileTooLargeError('Source file exceeds the configured limit')

    @staticmethod
    async def _read_limited_content(response: httpx.Response, max_bytes: int) -> bytes:
        content = bytearray()
        async for block in response.aiter_bytes():
            content.extend(block)
            if len(content) > max_bytes:
                raise SourceFileTooLargeError('Source file exceeds the configured limit')
        return bytes(content)

    # File upload and placement

    async def resolve_latest_version(self, content_document_id: str) -> str:
        soql = f"SELECT Id FROM ContentVersion WHERE ContentDocumentId='{content_document_id}' AND IsLatest=true LIMIT 1"
        records = await self._query(soql)
        if not records:
            raise RuntimeError('No latest ContentVersion was found')
        return records[0]['Id']

    async def upload_content_version(self, data: bytes, opts: UploadOptions) -> UploadResult:
        boundary = f'boundary_{secrets.token_hex(16)}'
        metadata = {'Title': opts.title, 'PathOnClient': opts.file_name}
        if opts.first_publish_location_id:
            metadata['FirstPublishLocationId'] = opts.first_publish_location_id

        body = self._build_multipart(boundary, metadata, data, opts.file_name)
        headers = {
            **self._auth_header,
            'Content-Type': f'multipart/form-data; boundary="{boundary}"',
        }
        # Do not retry ContentVersion creation. A timeout can happen after the
        # commit, and blindly retrying would create a duplicate file.
        async with httpx.AsyncClient(follow_redirects=False) as client:
            try:
                res = await client.post(
                    f'{self._base}/sobjects/ContentVersion',
                    headers=headers,
                    content=body,
                    timeout=UPLOAD_TIMEOUT_SECONDS,
                )
            except httpx.RequestError:
                raise RuntimeError('ContentVersion upload failed') from None
        if res.status_code != 201:
            raise RuntimeError(f'ContentVersion upload failed with status {res.status_code}')
        cv_id = self._response_json(res).get('id')
        if not cv_id:
            raise RuntimeError('ContentVersion upload returned no record ID')
        cd_id = await self._get_content_document_id(cv_id)
        return UploadResult(content_version_id=cv_id, content_document_id=cd_id)

    async def _get_content_document_id(self, content_version_id: str) -> str:
        soql = f"SELECT ContentDocumentId FROM ContentVersion WHERE Id='{content_version_id}'"
        records = await self._query(soql)
        if not records:
            raise RuntimeError('Could not resolve ContentDocumentId')
        return records[0]['ContentDocumentId']

    async def move_to_folder(self, content_document_id: str, target_folder_id: str) -> None:
        """Move a ContentDocument into a target folder by updating the
        auto-created ContentFolderMember (inserting a duplicate would fail
        on the uniqueness constraint, so we PATCH instead)."""
        soql = f"SELECT Id, ParentContentFolderId FROM ContentFolderMember WHERE ChildRecordId='{content_document_id}' LIMIT 1"
        records = await self._query(soql)
        if not records:
            raise RuntimeError('No ContentFolderMember was found for the uploaded file')
        member = records[0]
        if self._same_id(member.get('ParentContentFolderId'), target_folder_id):
            return
        patch_res = await self._request(
            'PATCH',
            f'{self._base}/sobjects/ContentFolderMember/{member["Id"]}',
            headers={**self._auth_header, 'Content-Type': 'application/json'},
            content=json.dumps({'ParentContentFolderId': target_folder_id}),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not (200 <= patch_res.status_code < 300):
            raise RuntimeError(f'ContentFolderMember move failed with status {patch_res.status_code}')

    async def link_to_record(self, content_document_id: str, linked_entity_id: str, share_type: str = 'V') -> None:
        """Create a ContentDocumentLink between a ContentDocument and any record."""
        res = await self._request(
            'POST',
            f'{self._base}/sobjects/ContentDocumentLink',
            headers={**self._auth_header, 'Content-Type': 'application/json'},
            content=json.dumps({
                'ContentDocumentId': content_document_id,
                'LinkedEntityId': linked_entity_id,
                'ShareType': share_type,
                'Visibility': 'AllUsers',
            }),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if res.status_code != 201:
            if 'DUPLICATE_VALUE' in res.text:
                return  # link already exists; not an error
            raise RuntimeError(f'ContentDocumentLink failed with status {res.status_code}')

    async def delete_content_documents(self, content_document_ids: list[str]) -> None:
        """Best-effort cleanup for outputs created by a failed request."""
        for content_document_id in reversed(content_document_ids):
            try:
                response = await self._request(
                    'DELETE',
                    f'{self._base}/sobjects/ContentDocument/{content_document_id}',
                    headers=self._auth_header,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                if response.status_code not in (204, 404):
                    self.log.warning('Output cleanup failed status=%s', response.status_code)
            except Exception as error:
                self.log.warning('Output cleanup failed type=%s', type(error).__name__)

    # Salesforce REST

    async def _query(self, soql: str) -> list[dict]:
        response = await self._request(
            'GET',
            f'{self._base}/query',
            headers=self._auth_header,
            params={'q': soql},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        body = self._response_json(response)
        if response.status_code != 200 or not isinstance(body.get('records'), list):
            raise RuntimeError(f'Salesforce query failed with status {response.status_code}')
        return body['records']

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        response = await self._request(
            'GET',
            url,
            headers=self._auth_header,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RequestAuthorizationError('Salesforce request context could not be verified')
        body = self._response_json(response)
        if not isinstance(body, dict):
            raise RequestAuthorizationError('Salesforce request context could not be verified')
        return body

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    response = await client.request(method, url, **kwargs)
            except httpx.RequestError:
                if self._is_last_attempt(attempt):
                    raise RuntimeError('Salesforce request failed after retries') from None
                await asyncio.sleep(2 ** attempt)
                continue
            if not self._should_retry(response, attempt):
                return response
            await self._retry_delay(response, attempt)
        raise RuntimeError('Salesforce request failed after retries')

    async def _retry_delay(self, response: httpx.Response, attempt: int) -> None:
        await asyncio.sleep(self._retry_delay_seconds(response.headers.get('retry-after'), attempt))

    @staticmethod
    def _retry_delay_seconds(retry_after: str | None, attempt: int) -> float:
        default_delay = float(2 ** attempt)
        if not retry_after:
            return default_delay
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                return max(0.0, min(parsed.timestamp() - time.time(), 30.0))
            except (TypeError, ValueError, OverflowError):
                return default_delay

    @staticmethod
    def _is_last_attempt(attempt: int) -> bool:
        return attempt >= MAX_RETRY_ATTEMPTS - 1

    @classmethod
    def _should_retry(cls, response: httpx.Response, attempt: int) -> bool:
        return response.status_code in TRANSIENT_STATUS_CODES and not cls._is_last_attempt(attempt)

    # Value and payload helpers

    @staticmethod
    def _response_json(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _same_id(first: str | None, second: str | None) -> bool:
        return bool(first and second and first[:15].lower() == second[:15].lower())

    @classmethod
    def _same_nullable_id(cls, first: str | None, second: str | None) -> bool:
        if not first and not second:
            return True
        return cls._same_id(first, second)

    @staticmethod
    def _build_multipart(boundary: str, meta: dict, data: bytes, file_name: str) -> bytes:
        metadata_part = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="entity_content"\r\n'
            f'Content-Type: application/json\r\n\r\n'
            + json.dumps(meta) + '\r\n'
        ).encode()
        file_header = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="VersionData"; filename="{file_name}"\r\n'
            f'Content-Type: application/pdf\r\n\r\n'
        ).encode()
        closing_boundary = f'\r\n--{boundary}--\r\n'.encode()
        return b''.join((metadata_part, file_header, data, closing_boundary))
