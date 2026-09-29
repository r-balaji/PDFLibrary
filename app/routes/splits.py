import logging
import secrets
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..pdf_ops import PdfLimitError, PdfPageAssignmentError, UnsafePdfError, prepare_splits
from ..security import (
    SALESFORCE_ID_PATTERN,
    SPLIT_JOB_OBJECT_PATTERN,
    processing_slot,
    run_idempotent_request,
    run_pdf_operation,
    validate_pdf_filename,
    validate_salesforce_endpoint,
)
from ..sf_client import (
    RequestAuthorizationError,
    SourceFileTooLargeError,
    SalesforceContext,
    SalesforceFilesClient,
    UploadOptions,
)

router = APIRouter()
log = logging.getLogger(__name__)


class SegmentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    documentType: str = Field(min_length=1, max_length=100)
    sourceInstitution: Optional[str] = Field(default=None, max_length=255)
    namedParty: Optional[str] = Field(default=None, max_length=255)
    instanceLabel: Optional[str] = Field(default=None, max_length=255)
    pages: List[int] = Field(min_length=1, max_length=2000)
    fileName: str = Field(min_length=5, max_length=255)

    @field_validator('fileName')
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        return validate_pdf_filename(value)


class SplitBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    jobId: str = Field(pattern=SALESFORCE_ID_PATTERN.pattern)
    sfOrgId: str = Field(pattern=SALESFORCE_ID_PATTERN.pattern)
    splitJobObjectApiName: str = Field(pattern=SPLIT_JOB_OBJECT_PATTERN.pattern)
    sourceContentVersionId: str = Field(pattern=SALESFORCE_ID_PATTERN.pattern)
    sfInstanceUrl: str
    sfAccessToken: str = Field(min_length=10, max_length=4096, repr=False)
    sfApiVersion: str = '60.0'
    libraryId: Optional[str] = Field(default=None, pattern=SALESFORCE_ID_PATTERN.pattern)
    targetFolderId: Optional[str] = Field(default=None, pattern=SALESFORCE_ID_PATTERN.pattern)
    linkToRecordId: Optional[str] = Field(default=None, pattern=SALESFORCE_ID_PATTERN.pattern)
    segments: List[SegmentInput] = Field(min_length=1, max_length=500)


class SplitOutput(BaseModel):
    fileName: str
    contentDocumentId: str
    contentVersionId: str
    documentType: str
    pages: List[int]


@router.post('/v1/splits')
async def splits_route(body: SplitBody, request: Request):
    config = request.app.state.config
    job_log = log.getChild(body.jobId)
    try:
        instance_url = validate_salesforce_endpoint(
            body.sfInstanceUrl,
            body.sfApiVersion,
            config.allowed_salesforce_domain_suffixes,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if len(body.segments) > config.max_segments:
        raise HTTPException(status_code=413, detail='Split request contains too many segments')

    sf = SalesforceFilesClient(SalesforceContext(
        instance_url=instance_url,
        access_token=body.sfAccessToken,
        api_version=body.sfApiVersion,
    ), job_log)

    async def process_request():
        created_document_ids = []
        try:
            source_bytes = await sf.download_version_data(body.sourceContentVersionId, config.max_source_bytes)
            _total_pages, sliced = await run_pdf_operation(
                request,
                prepare_splits,
                source_bytes,
                [{'pages': segment.pages, 'fileName': segment.fileName} for segment in body.segments],
                config.max_pages,
            )

            outputs = []
            for segment, segment_bytes in zip(body.segments, sliced):
                title = segment.fileName[:-4] if segment.fileName.lower().endswith('.pdf') else segment.fileName
                uploaded = await sf.upload_content_version(segment_bytes, UploadOptions(
                    title=title,
                    file_name=segment.fileName,
                    first_publish_location_id=body.libraryId,
                ))
                created_document_ids.append(uploaded.content_document_id)
                if body.targetFolderId:
                    await sf.move_to_folder(uploaded.content_document_id, body.targetFolderId)
                if body.linkToRecordId:
                    await sf.link_to_record(uploaded.content_document_id, body.linkToRecordId)
                await sf.link_to_record(uploaded.content_document_id, body.jobId)
                outputs.append(SplitOutput(
                    fileName=segment.fileName,
                    contentDocumentId=uploaded.content_document_id,
                    contentVersionId=uploaded.content_version_id,
                    documentType=segment.documentType,
                    pages=segment.pages,
                ))

            response = {'outputs': [output.model_dump() for output in outputs]}
            job_log.info(
                'stage=splits requestId=%s status=complete outputs=%s',
                request.state.request_id,
                len(outputs),
            )
            return response
        except Exception:
            if created_document_ids:
                await sf.delete_content_documents(created_document_ids)
            raise

    try:
        async with processing_slot(request):
            await sf.validate_job_context(
                body.sfOrgId,
                body.splitJobObjectApiName,
                body.jobId,
                body.sourceContentVersionId,
                body.libraryId,
                body.targetFolderId,
                body.linkToRecordId,
                True,
            )
            return await run_idempotent_request(request, 'splits', body, process_request)
    except RequestAuthorizationError:
        raise HTTPException(status_code=403, detail='Request does not match its Salesforce job') from None
    except SourceFileTooLargeError:
        raise HTTPException(status_code=413, detail='Source PDF exceeds the configured size limit')
    except UnsafePdfError:
        raise HTTPException(status_code=422, detail='Source file is not a safe, readable PDF') from None
    except PdfPageAssignmentError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except PdfLimitError as error:
        status_code = 422 if str(error) == 'Source PDF has no pages' else 413
        raise HTTPException(status_code=status_code, detail=str(error)) from None
    except HTTPException:
        raise
    except Exception as error:
        incident_id = secrets.token_hex(8)
        job_log.exception(
            'stage=splits requestId=%s status=failed incident=%s type=%s',
            request.state.request_id,
            incident_id,
            type(error).__name__,
        )
        raise HTTPException(status_code=500, detail=f'PDF processing failed; incident={incident_id}') from None
