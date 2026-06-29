from app.sf_client import SalesforceContext, SalesforceFilesClient


def test_build_multipart_contains_metadata_and_pdf_bytes():
    client = SalesforceFilesClient(
        SalesforceContext(
            instance_url='https://example.my.salesforce.com',
            access_token='token',
            api_version='60.0',
        )
    )

    body = client._build_multipart(
        boundary='test-boundary',
        meta={'Title': 'Output', 'PathOnClient': 'Output.pdf'},
        data=b'%PDF-test-bytes',
        file_name='Output.pdf',
    )

    assert b'--test-boundary' in body
    assert b'name="entity_content"' in body
    assert b'"Title": "Output"' in body
    assert b'name="VersionData"; filename="Output.pdf"' in body
    assert b'Content-Type: application/pdf' in body
    assert b'%PDF-test-bytes' in body
    assert body.endswith(b'\r\n--test-boundary--\r\n')
