import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..pdf_ops import get_page_count, split_by_pages
from ..sf_client import SalesforceContext, SalesforceFilesClient, UploadOptions

router = APIRouter()
log = logging.getLogger(__name__)


class SegmentInput(BaseModel):
    documentType: str
    sourceInstitution: Optional[str] = None
    namedParty: Optional[str] = None
    instanceLabel: Optional[str] = None
    pages: List[int]   # 1-based absolute page numbers
    fileName: str      # already built by Apex


class SplitBody(BaseModel):
    jobId: str
    sourceContentVersionId: str
    sfInstanceUrl: str
    sfAccessToken: str
    sfApiVersion: Optional[str] = '60.0'
    libraryId: Optional[str] = None
    targetFolderId: Optional[str] = None
    linkToRecordId: Optional[str] = None
    segments: List[SegmentInput]


class SplitOutput(BaseModel):
    fileName: str
    contentDocumentId: str
    contentVersionId: str
    documentType: str
    pages: List[int]


@router.post('/v1/splits')
async def splits_route(body: SplitBody, request: Request):
    if not body.segments:
        raise HTTPException(status_code=400, detail='segments must be a non-empty array')

    config = request.app.state.config
    job_log = log.getChild(body.jobId)
    ctx = SalesforceContext(
        instance_url=body.sfInstanceUrl,
        access_token=body.sfAccessToken,
        api_version=body.sfApiVersion or '60.0',
    )
    sf = SalesforceFilesClient(ctx, job_log)

    try:
        source_bytes = await sf.download_version_data(body.sourceContentVersionId)

        if len(source_bytes) > config.max_source_bytes:
            raise HTTPException(
                status_code=413,
                detail=f'Source file too large ({len(source_bytes)} bytes; max {config.max_source_bytes})',
            )

        async with request.app.state.pdf_semaphore:
            total_pages = await asyncio.to_thread(get_page_count, source_bytes)
        if total_pages < 1:
            raise HTTPException(status_code=422, detail='Source PDF has no pages')

        for seg in body.segments:
            if not seg.pages:
                raise HTTPException(status_code=422, detail=f'{seg.fileName} must include at least one page')
            invalid_pages = [p for p in seg.pages if p < 1 or p > total_pages]
            if invalid_pages:
                raise HTTPException(
                    status_code=422,
                    detail=f'{seg.fileName} has page(s) outside 1..{total_pages}: {invalid_pages}',
                )

        async with request.app.state.pdf_semaphore:
            sliced = await asyncio.to_thread(
                split_by_pages,
                source_bytes,
                [{'pages': s.pages} for s in body.segments],
            )

        outputs = []
        for seg, seg_bytes in zip(body.segments, sliced):
            title = seg.fileName[:-4] if seg.fileName.lower().endswith('.pdf') else seg.fileName
            up = await sf.upload_content_version(seg_bytes, UploadOptions(
                title=title,
                file_name=seg.fileName,
                first_publish_location_id=body.libraryId,
            ))
            if body.targetFolderId:
                await sf.move_to_folder(up.content_document_id, body.targetFolderId)
            if body.linkToRecordId:
                await sf.link_to_record(up.content_document_id, body.linkToRecordId)
            outputs.append(SplitOutput(
                fileName=seg.fileName,
                contentDocumentId=up.content_document_id,
                contentVersionId=up.content_version_id,
                documentType=seg.documentType,
                pages=seg.pages,
            ))

        job_log.info(f'{len(outputs)} splits uploaded')
        return {'outputs': [o.model_dump() for o in outputs]}

    except HTTPException:
        raise
    except Exception as exc:
        job_log.error(f'splits failed: {exc}')
        raise HTTPException(status_code=500, detail=str(exc))
