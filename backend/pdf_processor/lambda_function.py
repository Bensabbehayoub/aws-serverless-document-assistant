"""
Serverless AI Document Assistant - PDF processor Lambda (Phase 3)

Triggered automatically by S3 when a PDF is uploaded to uploads/<owner_id>/<document_id>.pdf

1. Marks the document "processing" in DynamoDB.
2. Downloads the PDF and extracts the text page by page (pypdf).
3. Saves the text as JSON to extracted/<owner_id>/<document_id>.json in the same bucket.
4. Marks the document "ready" (or "failed" with a reason).

Environment variables:
  TABLE_NAME  (required)  DynamoDB table name
"""

import io
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError
from pypdf import PdfReader

TABLE_NAME = os.environ["TABLE_NAME"]

table = boto3.resource("dynamodb").Table(TABLE_NAME)
s3 = boto3.client("s3")

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB, same limit as the API
MAX_PAGES = 300
MIN_TEXT_CHARS = 20               # less than this = probably a scanned PDF

KEY_PATTERN = re.compile(r"^uploads/(?P<owner>[^/]+)/(?P<doc>doc-[a-f0-9]{12})\.pdf$")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(level, **fields):
    print(json.dumps({"level": level, **fields}))


def set_status(document_id, owner_id, status, extra=None):
    """Update the status only if the document still exists and belongs to this owner."""
    names = {"#s": "status"}
    values = {":s": status, ":owner": owner_id, ":t": now_iso()}
    parts = ["#s = :s", "processed_at = :t"]

    for i, (key, value) in enumerate((extra or {}).items()):
        names[f"#k{i}"] = key
        values[f":v{i}"] = value
        parts.append(f"#k{i} = :v{i}")

    try:
        table.update_item(
            Key={"document_id": document_id},
            # The file has arrived, so the "unfinished upload" expiry no longer applies.
            UpdateExpression="SET " + ", ".join(parts) + " REMOVE expires_at",
            ConditionExpression="attribute_exists(document_id) AND owner_id = :owner",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Document was deleted (or never existed) - nothing to update.
            return False
        raise


def extract_pages(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise ValueError("The PDF is password-protected.")

    if len(reader.pages) > MAX_PAGES:
        raise ValueError(f"The PDF has more than {MAX_PAGES} pages.")

    pages = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        pages.append({"page": number, "text": text})
    return pages


def process_object(bucket, key):
    match = KEY_PATTERN.match(key)
    if not match:
        log("WARN", message="Ignoring object with unexpected key", key=key)
        return

    owner_id = match.group("owner")
    document_id = match.group("doc")

    if not set_status(document_id, owner_id, "processing"):
        log("WARN", message="Document not found, skipping", document_id=document_id)
        return

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] > MAX_FILE_SIZE:
            raise ValueError("The file is larger than 10 MB.")

        pdf_bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        pages = extract_pages(pdf_bytes)
        total_chars = sum(len(p["text"]) for p in pages)

        if total_chars < MIN_TEXT_CHARS:
            raise ValueError("No readable text found. The PDF is probably scanned (an image).")

        text_key = f"extracted/{owner_id}/{document_id}.json"
        s3.put_object(
            Bucket=bucket,
            Key=text_key,
            Body=json.dumps({"document_id": document_id, "pages": pages}, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

        set_status(document_id, owner_id, "ready", {
            "text_key": text_key,
            "page_count": len(pages),
            "text_length": total_chars,
        })
        log("INFO", message="Document processed", document_id=document_id,
            pages=len(pages), chars=total_chars)

    except ValueError as err:
        set_status(document_id, owner_id, "failed", {"error_message": str(err)})
        log("WARN", message="Document could not be processed", document_id=document_id, reason=str(err))

    except Exception as err:  # noqa: BLE001
        set_status(document_id, owner_id, "failed", {"error_message": "Text extraction failed."})
        log("ERROR", message="Unexpected processing error", document_id=document_id,
            type=type(err).__name__, detail=str(err))


def lambda_handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        process_object(bucket, key)
    return {"processed": len(event.get("Records", []))}
