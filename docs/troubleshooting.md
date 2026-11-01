# Troubleshooting guide

Where to look when something goes wrong, from the symptom in the app to the cause in AWS.

## Where the signals are

| What | Where |
|---|---|
| Overview of requests, errors, latency | CloudWatch → Dashboards → `document-assistant` |
| Alarm emails | Sent to the SNS topic `document-assistant-alerts` |
| API Lambda logs | CloudWatch → Log groups → `/aws/lambda/serverless-ai-assistant` |
| PDF processor logs | CloudWatch → Log groups → `/aws/lambda/ai-assistant-pdf-processor` |
| Document state | DynamoDB → `ai-assistant-documents` → Explore table items |
| Files | S3 bucket → `uploads/<user>/<document>.pdf` and `extracted/<user>/<document>.json` |

All application logs are single-line JSON with a `level` field (`INFO`, `WARN`, `ERROR`), so they can be filtered in CloudWatch Logs Insights:

```
fields @timestamp, level, message, code, reason, detail, document_id
| filter level = "ERROR" or level = "WARN"
| sort @timestamp desc
| limit 50
```

Logs never contain access tokens or document text.

## Symptoms

### "Failed to fetch" in the browser

The browser blocked the request before it got an answer. Almost always CORS or a missing route.

1. DevTools → Network: find the red request and note its method and status.
2. API Gateway → CORS: origin `http://localhost:3000`, headers `authorization`, `content-type`, methods `GET, POST, DELETE, OPTIONS`.
3. API Gateway → Routes: the method must be covered by the `ANY` route (or its own route).
4. If the stage has no automatic deployment, deploy to `default`.

A 404 from API Gateway has no CORS headers, so a wrong URL or missing route also shows up as a CORS error.

### HTTP 401

The login token is missing, expired or not accepted. Sign out and sign in again. If it persists, check API Gateway → Authorization → `CognitoJWT`: the issuer must be the user pool URL and the audience the app client ID.

### HTTP 429

The API throttling limit was reached (10 requests/second, burst 20). Wait a moment. If it happens during normal use, raise the limit in API Gateway → Throttling.

### HTTP 500 with a code in brackets

The API Lambda caught an AWS error and returned its code.

| Code | Meaning | Fix |
|---|---|---|
| `AccessDeniedException` / `AccessDenied` | The Lambda role lacks a permission | Add the missing action to the role's inline policy |
| `ResourceNotFoundException` | Table or index name wrong | Check `TABLE_NAME` and the `owner_id-index` |
| `ValidationException` | Bad request to DynamoDB or the AI model | Read the full error in the API Lambda logs |
| `NoSuchBucket` | Bucket name or region wrong | Check `BUCKET_NAME` |

### HTTP 504 or very slow answers

The Lambda timed out. Check the dashboard's "Lambda duration p95". The API Lambda timeout must be 30 seconds (API Gateway's maximum).

### Document stays "Upload not finished"

The browser created the document but the file never reached S3 (closed tab, lost connection, expired upload link). These entries delete themselves after about a day through DynamoDB TTL on `expires_at`. They can also be deleted in the app.

### Document stays "Queued" or "Reading text"

The PDF processor didn't run or didn't finish.

1. S3 → bucket → Properties → Event notifications: the trigger for `uploads/` + `.pdf` must exist.
2. Processor logs: look for `ERROR` lines or `Task timed out`.
3. A timeout on a large PDF: raise the processor timeout (currently 1 minute) or memory (512 MB).

### Document shows "Couldn't read"

The processor ran but found no usable text. The reason is shown in the app and stored in `error_message`:

- *No readable text found*: the PDF is a scan (an image). Extracting text from scans would need OCR (Amazon Textract, paid).
- *password-protected*: remove the password and upload again.
- *more than 300 pages*: split the PDF.
- *Text extraction failed*: a damaged or unusual PDF. The processor log has the exception type and details.

### Upload fails with "The upload to storage failed"

The PUT to S3 was rejected.

- 403: the upload link expired (valid 5 minutes) or the Lambda role lacks `s3:PutObject` on `uploads/*`.
- A CORS error on the S3 URL: the bucket's CORS rule must allow `PUT` from `http://localhost:3000` with header `content-type`.

## Alarms

| Alarm | Fires when | First thing to check |
|---|---|---|
| `api-5xx` | 3 or more server errors in 5 minutes | API Lambda logs, `ERROR` lines |
| `api-lambda-errors` | The API Lambda crashed or timed out | API Lambda logs, `Task timed out` or a stack trace |
| `processor-lambda-errors` | The processor crashed or timed out | Processor logs |
| `processor-extraction-errors` | The processor hit an unexpected error on a PDF | Processor logs, the `detail` field |
| `api-lambda-throttles` | Lambda refused to run because of concurrency limits | Account concurrency quota in Service Quotas |
