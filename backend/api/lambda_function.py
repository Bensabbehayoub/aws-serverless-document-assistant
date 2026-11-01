"""
Serverless AI Document Assistant - Lambda backend (Phase 4: CRUD + PDF uploads + text + AI Q&A)

Routes (all on /serverless-ai-assistant, protected by the Cognito JWT authorizer):
  POST    body {"title", "category"}                               -> create a metadata-only document (201)
  POST    body {"title", "category", "file_name", "content_type", "file_size"}
                                                                   -> create a document + presigned S3 upload URL (201)
  POST    ?document_id=<id>&action=confirm_upload                  -> check the file arrived in S3, mark "uploaded" (200)
  GET     ?document_id=<id>                                        -> get one of YOUR documents (200 / 404)
  GET     ?document_id=<id>&action=download                        -> short-lived link to open YOUR PDF (200)
  GET     ?document_id=<id>&action=text                            -> extracted text preview of YOUR PDF (200)
  POST    ?document_id=<id>&action=ask   body {"question": "..."}  -> AI answer grounded in YOUR PDF, with page sources (200)
  GET     (no query parameter)                                     -> list YOUR documents (200)
  DELETE  ?document_id=<id>                                        -> delete YOUR document, its PDF and its text (200 / 404)

Text extraction is done by a separate Lambda (ai-assistant-pdf-processor) triggered by S3.
Statuses: created -> pending_upload -> uploaded -> processing -> ready | failed

The owner is always taken from the verified Cognito token ("sub" claim), never from the request.

Environment variables:
  TABLE_NAME   (required)  DynamoDB table name
  BUCKET_NAME  (required for uploads)  private S3 bucket for PDFs
  OWNER_INDEX  (optional)  GSI name, default "owner_id-index"
  AI_PROVIDER  (optional)  "gemini" (default), "bedrock" or "extractive" (no AI, returns best passages)
  GEMINI_MODEL (optional)  Gemini model ID, default "gemini-flash-latest"
  GEMINI_KEY_PARAM (optional)  SSM Parameter Store name of the Gemini API key (SecureString),
                               default "/ai-assistant/gemini-api-key"
  MODEL_ID     (optional)  Bedrock model / inference profile, default "eu.amazon.nova-micro-v1:0"
"""

import base64
import json
import math
import os
import re
import urllib.error
import urllib.request
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["TABLE_NAME"]
BUCKET_NAME = os.environ.get("BUCKET_NAME")
OWNER_INDEX = os.environ.get("OWNER_INDEX", "owner_id-index")
REGION = os.environ.get("AWS_REGION", "eu-north-1")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_KEY_PARAM = os.environ.get("GEMINI_KEY_PARAM", "/ai-assistant/gemini-api-key")
MODEL_ID = os.environ.get("MODEL_ID", "eu.amazon.nova-micro-v1:0")

table = boto3.resource("dynamodb").Table(TABLE_NAME)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
_gemini_key_cache = None

# Regional endpoint + SigV4 so presigned URLs work directly from the browser (no redirects).
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

MAX_TITLE_LENGTH = 200
MAX_CATEGORY_LENGTH = 100
MAX_FILE_NAME_LENGTH = 255
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
PDF_CONTENT_TYPE = "application/pdf"
UPLOAD_URL_EXPIRES = 300    # seconds
PENDING_UPLOAD_TTL = 24 * 3600  # unfinished uploads are deleted by DynamoDB TTL after a day
DOWNLOAD_URL_EXPIRES = 300  # seconds
TEXT_PREVIEW_CHARS = 20000
FILE_STATUSES = {"uploaded", "processing", "ready", "failed"}  # documents that have a PDF in S3

# --- AI question answering (keeps every call small and cheap) ---
MAX_QUESTION_LENGTH = 1000
CHUNK_CHARS = 1500           # size of one text piece sent to the model
MAX_CONTEXT_CHARS = 12000    # at most ~3,000 tokens of document text per question
MAX_ANSWER_TOKENS = 600
SNIPPET_CHARS = 200
STOPWORDS = set("""
a an and are as at be by can could did do does for from had has have how i if in into is it its
me my of on or our so than that the their them then there these they this to was we were what
when where which who why will with would you your about please tell explain document pdf
summarize summarise summary overview bullet bullets point points few short brief main key give
""".split())

SYSTEM_PROMPT = (
    "You are a document assistant. Answer the user's question using ONLY the document excerpts "
    "provided. Each excerpt starts with its page number like [Page 3]. After every statement, "
    "cite the page(s) it comes from in the form [p. 3]. If the excerpts do not contain the answer, "
    "say clearly that the document does not contain this information and do not guess. "
    "Answer in the same language as the question. Be concise."
)


# ---------- helpers ----------

def build_response(status_code, body):
    # CORS headers are added by API Gateway's CORS configuration.
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_method(event):
    return (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or ""
    ).upper()


def get_user_id(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {}) or {}
    claims = authorizer.get("jwt", {}).get("claims") or authorizer.get("claims") or {}
    return claims.get("sub")


def get_query_params(event):
    return event.get("queryStringParameters") or {}


def parse_json_body(event):
    raw = event.get("body")
    if not raw:
        return None
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def clean_text(value, max_length):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    return value


def load_own_document(document_id, user_id):
    """Return the document if it exists AND belongs to this user, else None."""
    item = table.get_item(Key={"document_id": document_id}).get("Item")
    if not item or item.get("owner_id") != user_id:
        return None
    return item


def not_found():
    # Same answer whether the document doesn't exist or belongs to someone else.
    return build_response(404, {"error": "Document not found."})


# ---------- handlers ----------

def create_document(event, user_id):
    data = parse_json_body(event)
    if data is None:
        return build_response(400, {"error": "Request body must be valid JSON."})

    title = clean_text(data.get("title"), MAX_TITLE_LENGTH)
    category = clean_text(data.get("category"), MAX_CATEGORY_LENGTH)
    if not title:
        return build_response(400, {"error": f"'title' is required (max {MAX_TITLE_LENGTH} characters)."})
    if not category:
        return build_response(400, {"error": f"'category' is required (max {MAX_CATEGORY_LENGTH} characters)."})

    document_id = f"doc-{uuid.uuid4().hex[:12]}"
    item = {
        "document_id": document_id,
        "owner_id": user_id,
        "title": title,
        "category": category,
        "status": "created",
        "created_at": now_iso(),
    }

    upload_url = None
    if data.get("file_name") is not None:
        if not BUCKET_NAME:
            return build_response(500, {"error": "Uploads are not configured (BUCKET_NAME missing)."})

        file_name = clean_text(data.get("file_name"), MAX_FILE_NAME_LENGTH)
        if not file_name or not file_name.lower().endswith(".pdf"):
            return build_response(400, {"error": "'file_name' must be a .pdf file name."})
        if data.get("content_type") != PDF_CONTENT_TYPE:
            return build_response(400, {"error": "Only PDF files are allowed."})

        file_size = data.get("file_size")
        if isinstance(file_size, bool) or not isinstance(file_size, int) or not 0 < file_size <= MAX_FILE_SIZE:
            return build_response(400, {"error": f"'file_size' must be between 1 byte and {MAX_FILE_SIZE // (1024 * 1024)} MB."})

        s3_key = f"uploads/{user_id}/{document_id}.pdf"
        item.update({
            "status": "pending_upload",
            # DynamoDB TTL deletes this entry automatically if the upload never completes.
            # confirm_upload and the PDF processor remove it once the file has arrived.
            "expires_at": int(time.time()) + PENDING_UPLOAD_TTL,
            "file_name": file_name,
            "s3_key": s3_key,
        })
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": BUCKET_NAME, "Key": s3_key, "ContentType": PDF_CONTENT_TYPE},
            ExpiresIn=UPLOAD_URL_EXPIRES,
        )

    table.put_item(Item=item, ConditionExpression=Attr("document_id").not_exists())

    body = {"message": "Document created successfully", "document": item}
    if upload_url:
        body["upload_url"] = upload_url
        body["upload_expires_in"] = UPLOAD_URL_EXPIRES
    return build_response(201, body)


def confirm_upload(document_id, user_id):
    item = load_own_document(document_id, user_id)
    if not item:
        return not_found()
    s3_key = item.get("s3_key")
    if not s3_key:
        return build_response(400, {"error": "This document has no file attached."})

    try:
        head = s3.head_object(Bucket=BUCKET_NAME, Key=s3_key)
    except ClientError as err:
        if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return build_response(400, {"error": "The file has not been uploaded yet."})
        raise

    size = head.get("ContentLength", 0)
    content_type = head.get("ContentType", "")

    # The real file is checked here, not just what the browser claimed.
    if size > MAX_FILE_SIZE or content_type != PDF_CONTENT_TYPE:
        s3.delete_object(Bucket=BUCKET_NAME, Key=s3_key)
        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "rejected"},
        )
        return build_response(400, {"error": "Uploaded file rejected (must be a PDF up to 10 MB)."})

    # The PDF processor may already have started (S3 triggers it as soon as the file lands).
    # Only move to "uploaded" if we are still at "pending_upload", so we never overwrite
    # "processing" or "ready".
    try:
        result = table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET #s = :s, file_size = :size, uploaded_at = :t REMOVE expires_at",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "uploaded", ":size": size, ":t": now_iso(), ":pending": "pending_upload",
            },
            ReturnValues="ALL_NEW",
        )
        document = result["Attributes"]
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET file_size = :size, uploaded_at = :t REMOVE expires_at",
            ExpressionAttributeValues={":size": size, ":t": now_iso()},
        )
        document = table.get_item(Key={"document_id": document_id})["Item"]

    return build_response(200, {"message": "Upload confirmed", "document": document})


def get_document(document_id, user_id):
    item = load_own_document(document_id, user_id)
    if not item:
        return not_found()
    return build_response(200, {"message": "Document retrieved successfully", "document": item})


def get_download_url(document_id, user_id):
    item = load_own_document(document_id, user_id)
    if not item:
        return not_found()
    if item.get("status") not in FILE_STATUSES or not item.get("s3_key"):
        return build_response(400, {"error": "This document has no uploaded file."})

    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": item["s3_key"],
            "ResponseContentType": PDF_CONTENT_TYPE,
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=DOWNLOAD_URL_EXPIRES,
    )
    return build_response(200, {"download_url": url, "expires_in": DOWNLOAD_URL_EXPIRES})


def get_text_preview(document_id, user_id):
    item = load_own_document(document_id, user_id)
    if not item:
        return not_found()

    status = item.get("status")
    if status == "failed":
        return build_response(422, {"error": item.get("error_message", "Text extraction failed.")})
    if status != "ready" or not item.get("text_key"):
        return build_response(409, {"error": f"Text is not ready yet (status: {status})."})

    raw = s3.get_object(Bucket=BUCKET_NAME, Key=item["text_key"])["Body"].read()
    pages = json.loads(raw).get("pages", [])
    full_text = "\n\n".join(f"[Page {p['page']}]\n{p['text']}" for p in pages if p.get("text"))

    return build_response(200, {
        "document_id": document_id,
        "page_count": item.get("page_count", len(pages)),
        "text_length": item.get("text_length", len(full_text)),
        "preview": full_text[:TEXT_PREVIEW_CHARS],
        "truncated": len(full_text) > TEXT_PREVIEW_CHARS,
    })


def tokenize(text):
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1 and w not in STOPWORDS]


def make_chunks(pages):
    """Split each page into pieces of about CHUNK_CHARS, keeping the page number."""
    chunks = []
    for page in pages:
        text = page.get("text") or ""
        start = 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))
            if end < len(text):  # try to cut at a sentence or line break
                cut = max(text.rfind(". ", start, end), text.rfind("\n", start, end))
                if cut > start + CHUNK_CHARS // 2:
                    end = cut + 1
            piece = text[start:end].strip()
            if piece:
                chunks.append({"page": page["page"], "text": piece, "order": len(chunks)})
            start = end
    return chunks


def rank_chunks(chunks, question):
    """Order pieces by keyword relevance (BM25-style), best first.
    Only matching pieces are returned; if nothing matches (e.g. "summarize"), document order is used."""
    if not chunks:
        return []
    query_terms = set(tokenize(question))
    tokenized = [Counter(tokenize(c["text"])) for c in chunks]
    n = len(chunks)
    avg_len = sum(sum(t.values()) for t in tokenized) / n or 1
    doc_freq = Counter(term for t in tokenized for term in t if term in query_terms)

    def score(i):
        t, length = tokenized[i], sum(tokenized[i].values()) or 1
        total = 0.0
        for term in query_terms:
            tf = t.get(term, 0)
            if tf:
                idf = math.log(1 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                total += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg_len))
        return total

    scores = [score(i) for i in range(n)]
    ranked = sorted((i for i in range(n) if scores[i] > 0), key=lambda i: (-scores[i], i))
    if not ranked:
        ranked = list(range(n))
    return [chunks[i] for i in ranked]


def select_chunks(chunks, question):
    """Pieces to send to the AI: everything for small documents, otherwise the most
    relevant pieces within the size budget. Returned in document order."""
    if sum(len(c["text"]) for c in chunks) <= MAX_CONTEXT_CHARS:
        return chunks  # small document: send everything

    selected, used = [], 0
    for chunk in rank_chunks(chunks, question):
        size = len(chunk["text"])
        if used + size > MAX_CONTEXT_CHARS:
            continue
        selected.append(chunk)
        used += size
    return sorted(selected, key=lambda c: c["order"])  # keep document order


class AIProviderError(Exception):
    """An error from the AI provider that should be shown to the user."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def get_gemini_key():
    """Read the API key from SSM Parameter Store once per Lambda container."""
    global _gemini_key_cache
    if _gemini_key_cache is None:
        try:
            result = ssm.get_parameter(Name=GEMINI_KEY_PARAM, WithDecryption=True)
        except ClientError as err:
            code = err.response["Error"]["Code"]
            if code == "ParameterNotFound":
                raise AIProviderError(500, f"Gemini API key not found in Parameter Store ({GEMINI_KEY_PARAM}).")
            raise
        _gemini_key_cache = result["Parameter"]["Value"].strip()
    return _gemini_key_cache


def call_gemini(system_prompt, user_message):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        # Newer Flash models "think" before answering and that counts toward the output
        # limit, so the limit is higher than the answer itself needs.
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.2},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": get_gemini_key()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        print(json.dumps({"level": "ERROR", "source": "gemini", "status": err.code, "detail": detail}))
        if err.code == 429:
            raise AIProviderError(429, "The free AI quota is used up for now. Try again in a minute.")
        if err.code in (400, 401, 403):
            raise AIProviderError(502, f"Gemini rejected the request (HTTP {err.code}). Check the API key and model name.")
        if err.code == 404:
            raise AIProviderError(502, f"Gemini model '{GEMINI_MODEL}' was not found. Check GEMINI_MODEL.")
        raise AIProviderError(502, f"Gemini error (HTTP {err.code}).")
    except (urllib.error.URLError, TimeoutError) as err:
        print(json.dumps({"level": "ERROR", "source": "gemini", "detail": str(err)}))
        raise AIProviderError(504, "The AI service did not answer in time. Please try again.")

    candidates = data.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
    answer = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not answer:
        reason = candidates[0].get("finishReason") if candidates else (data.get("promptFeedback") or {}).get("blockReason")
        raise AIProviderError(502, f"The AI returned no answer (reason: {reason or 'unknown'}).")

    usage = data.get("usageMetadata", {})
    return answer, usage.get("promptTokenCount"), usage.get("candidatesTokenCount")


def call_bedrock(system_prompt, user_message):
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": MAX_ANSWER_TOKENS, "temperature": 0.2},
    )
    answer = "".join(part.get("text", "") for part in response["output"]["message"]["content"]).strip()
    usage = response.get("usage", {})
    return answer, usage.get("inputTokens"), usage.get("outputTokens")


EXTRACTIVE_PASSAGES = 3
EXTRACTIVE_PASSAGE_CHARS = 700


def extractive_answer(passages):
    """No-AI mode: the answer is the most relevant passages with their page numbers."""
    lines = ["AI answers are turned off. These are the most relevant passages:"]
    for p in passages:
        lines.append(f"\n[p. {p['page']}] {p['text']}")
    return "\n".join(lines), None, None


def model_name():
    return {"gemini": GEMINI_MODEL, "bedrock": MODEL_ID}.get(AI_PROVIDER, "none")


def load_pages(item):
    raw = s3.get_object(Bucket=BUCKET_NAME, Key=item["text_key"])["Body"].read()
    return json.loads(raw).get("pages", [])


def ask_document(event, document_id, user_id):
    data = parse_json_body(event)
    if data is None:
        return build_response(400, {"error": "Request body must be valid JSON."})
    question = clean_text(data.get("question"), MAX_QUESTION_LENGTH)
    if not question:
        return build_response(400, {"error": f"'question' is required (max {MAX_QUESTION_LENGTH} characters)."})

    item = load_own_document(document_id, user_id)
    if not item:
        return not_found()
    if item.get("status") != "ready" or not item.get("text_key"):
        return build_response(409, {"error": f"The document is not ready yet (status: {item.get('status')})."})

    all_chunks = make_chunks(load_pages(item))
    if not all_chunks:
        return build_response(422, {"error": "This document has no readable text."})

    passages = None
    if AI_PROVIDER not in ("gemini", "bedrock"):
        chunks = rank_chunks(all_chunks, question)[:EXTRACTIVE_PASSAGES]  # best first
        passages = [{"page": c["page"], "text": c["text"][:EXTRACTIVE_PASSAGE_CHARS]} for c in chunks]
    else:
        chunks = select_chunks(all_chunks, question)

    context = "\n\n".join(f"[Page {c['page']}]\n{c['text']}" for c in chunks)
    user_message = (
        f"Document title: {item.get('title', '')}\n\n"
        f"<document_excerpts>\n{context}\n</document_excerpts>\n\n"
        f"Question: {question}"
    )

    try:
        if AI_PROVIDER == "gemini":
            answer, input_tokens, output_tokens = call_gemini(SYSTEM_PROMPT, user_message)
        elif AI_PROVIDER == "bedrock":
            answer, input_tokens, output_tokens = call_bedrock(SYSTEM_PROMPT, user_message)
        else:
            answer, input_tokens, output_tokens = extractive_answer(passages)
    except AIProviderError as err:
        return build_response(err.status_code, {"error": err.message})

    sources, seen_pages = [], set()
    for c in chunks:
        if c["page"] not in seen_pages:
            seen_pages.add(c["page"])
            sources.append({"page": c["page"], "snippet": c["text"][:SNIPPET_CHARS]})

    print(json.dumps({
        "level": "INFO", "event": "ask", "document_id": document_id,
        "provider": AI_PROVIDER, "model": model_name(), "chunks": len(chunks),
        "input_tokens": input_tokens, "output_tokens": output_tokens,
    }))

    body = {
        "document_id": document_id,
        "question": question,
        "answer": answer,
        "sources": sources,
        "provider": AI_PROVIDER,
        "model": model_name(),
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    if passages is not None:
        body["passages"] = passages  # best first, for the no-AI mode
    return build_response(200, body)


def list_documents(user_id):
    items = []
    query_args = {
        "IndexName": OWNER_INDEX,
        "KeyConditionExpression": Key("owner_id").eq(user_id),
    }
    while True:
        result = table.query(**query_args)
        items.extend(result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            break
        query_args["ExclusiveStartKey"] = result["LastEvaluatedKey"]

    items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return build_response(200, {
        "count": len(items),
        "documents": items,
        "ai_enabled": AI_PROVIDER in ("gemini", "bedrock"),
    })


def delete_document(document_id, user_id):
    try:
        result = table.delete_item(
            Key={"document_id": document_id},
            ConditionExpression=Attr("owner_id").eq(user_id),
            ReturnValues="ALL_OLD",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return not_found()
        raise

    old = result.get("Attributes", {})
    if BUCKET_NAME:
        for key in (old.get("s3_key"), old.get("text_key")):
            if key:
                s3.delete_object(Bucket=BUCKET_NAME, Key=key)

    return build_response(200, {"message": "Document deleted successfully", "document_id": document_id})


# ---------- entry point ----------

def lambda_handler(event, context):
    method = get_method(event)

    if method == "OPTIONS":
        return build_response(204, {})

    user_id = get_user_id(event)
    if not user_id:
        return build_response(401, {"error": "Unauthorized."})

    params = get_query_params(event)
    document_id = (params.get("document_id") or "").strip()
    action = (params.get("action") or "").strip()

    try:
        if method == "POST":
            if action == "confirm_upload":
                if not document_id:
                    return build_response(400, {"error": "'document_id' query parameter is required."})
                return confirm_upload(document_id, user_id)
            if action == "ask":
                if not document_id:
                    return build_response(400, {"error": "'document_id' query parameter is required."})
                return ask_document(event, document_id, user_id)
            return create_document(event, user_id)

        if method == "GET":
            if document_id and action == "download":
                return get_download_url(document_id, user_id)
            if document_id and action == "text":
                return get_text_preview(document_id, user_id)
            if document_id:
                return get_document(document_id, user_id)
            return list_documents(user_id)

        if method == "DELETE":
            if not document_id:
                return build_response(400, {"error": "'document_id' query parameter is required."})
            return delete_document(document_id, user_id)

        return build_response(405, {"error": f"Method {method} not allowed."})

    except ClientError as err:
        code = err.response["Error"]["Code"]
        print(json.dumps({"level": "ERROR", "source": "aws", "code": code, "method": method, "action": action}))
        return build_response(500, {"error": "AWS service error.", "code": code})
    except Exception as err:  # noqa: BLE001
        print(json.dumps({"level": "ERROR", "source": "lambda", "type": type(err).__name__, "detail": str(err)}))
        return build_response(500, {"error": "Internal server error."})
