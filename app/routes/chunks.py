import logging
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..pdf_ops import PdfLimitError, UnsafePdfError, prepare_chunks
from ..security import (
    SALESFORCE_ID_PATTERN,
    SPLIT_JOB_OBJECT_PATTERN,
    processing_slot,
    run_idempotent_request,
    run_pdf_operation,
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


class ChunkBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    jobId: str = Field(pattern=SALESFORCE_ID_PATTERN.pattern)
    sfOrgId: str = Field(pattern=SALESFORCE_ID_PATTERN.pattern)
    splitJobObjectApiName: str = Field(pattern=SPLIT_JOB_OBJECT_PATTERN.pattern)
    sourceContentVersionId: str = Field(pattern=SALESFORCE_ID_PATTERN.pattern)
    sfInstanceUrl: str
    sfAccessToken: str = Field(min_length=10, max_length=4096, repr=False)
    sfApiVersion: str = '60.0'
    libraryId: Optional[str] = Field(default=None, pattern=SALESFORCE_ID_PATTERN.pattern)
    maxChunkBytes: Optional[int] = Field(default=None, gt=0)
    chunkSize: Optional[int] = Field(default=8, ge=1, le=200)
    overlap: Optional[int] = Field(default=None, ge=0, le=50)


class ChunkOutput(BaseModel):
    chunkIndex: int
    contentDocumentId: str
    contentVersionId: str
    pageOffset: int
    pageCount: int


@router.post('/v1/chunks')
async def chunks_route(body: ChunkBody, request: Request):
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
    if body.maxChunkBytes is not None and not config.min_chunk_bytes <= body.maxChunkBytes <= config.max_chunk_bytes:
        raise HTTPException(
            status_code=422,
            detail=f'maxChunkBytes must be between {config.min_chunk_bytes} and {config.max_chunk_bytes}',
        )

    sf = SalesforceFilesClient(SalesforceContext(
        instance_url=instance_url,
        access_token=body.sfAccessToken,
        api_version=body.sfApiVersion,
    ), job_log)

    async def process_request():
        created_document_ids = []
        try:
            source_bytes = await sf.download_version_data(body.sourceContentVersionId, config.max_source_bytes)
            overlap = body.overlap if body.overlap is not None else (0 if body.maxChunkBytes is not None else 2)
            chunk_size = body.chunkSize if body.chunkSize is not None else 8
            total_pages, chunk_specs, chunk_bytes_list = await run_pdf_operation(
                request,
                prepare_chunks,
                source_bytes,
                body.maxChunkBytes,
                chunk_size,
                overlap,
                config.max_pages,
                config.max_segments,
            )

            outputs = []
            for spec, chunk_bytes in zip(chunk_specs, chunk_bytes_list):
                uploaded = await sf.upload_content_version(chunk_bytes, UploadOptions(
                    title=f'bundle_chunk_{spec.chunk_index}',
                    file_name=f'bundle_chunk_{spec.chunk_index}.pdf',
                    first_publish_location_id=body.libraryId,
                ))
                created_document_ids.append(uploaded.content_document_id)
                await sf.link_to_record(uploaded.content_document_id, body.jobId)
                outputs.append(ChunkOutput(
                    chunkIndex=spec.chunk_index,
                    contentDocumentId=uploaded.content_document_id,
                    contentVersionId=uploaded.content_version_id,
                    pageOffset=spec.start_page,
                    pageCount=spec.end_page - spec.start_page + 1,
                ))

            response = {'totalPages': total_pages, 'chunks': [output.model_dump() for output in outputs]}
            job_log.info(
                'stage=chunks requestId=%s status=complete outputs=%s',
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
            )
            return await run_idempotent_request(request, 'chunks', body, process_request)
    except RequestAuthorizationError:
        raise HTTPException(status_code=403, detail='Request does not match its Salesforce job') from None
    except SourceFileTooLargeError:
        raise HTTPException(status_code=413, detail='Source PDF exceeds the configured size limit')
    except UnsafePdfError:
        raise HTTPException(status_code=422, detail='Source file is not a safe, readable PDF') from None
    except PdfLimitError as error:
        status_code = 422 if str(error) == 'Source PDF has no pages' else 413
        raise HTTPException(status_code=status_code, detail=str(error)) from None
    except HTTPException:
        raise
    except Exception as error:
        incident_id = secrets.token_hex(8)
        job_log.exception(
            'stage=chunks requestId=%s status=failed incident=%s type=%s',
            request.state.request_id,
            incident_id,
            type(error).__name__,
        )
        raise HTTPException(status_code=500, detail=f'PDF processing failed; incident={incident_id}') from None
