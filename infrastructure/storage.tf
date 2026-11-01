# ---------- DynamoDB: document metadata ----------

resource "aws_dynamodb_table" "documents" {
  name         = "${local.name}-documents"
  billing_mode = "PAY_PER_REQUEST" # pay per request, nothing when idle
  hash_key     = "document_id"

  attribute {
    name = "document_id"
    type = "S"
  }

  attribute {
    name = "owner_id"
    type = "S"
  }

  # Lets the API list "my documents" without scanning the whole table.
  global_secondary_index {
    name            = "owner_id-index"
    hash_key        = "owner_id"
    projection_type = "ALL"
  }

  # Unfinished uploads carry expires_at and are deleted automatically.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Encrypted at rest by default with an AWS-owned key (free, no KMS charges).
}

# ---------- S3: PDFs and extracted text ----------

resource "aws_s3_bucket" "documents" {
  bucket = "${local.name}-pdfs-${local.account_id}"

  # Dev environment: allow "terraform destroy" to remove the bucket with its files.
  force_destroy = var.environment != "prod"
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket = aws_s3_bucket.documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The browser uploads PDFs directly to S3 with a presigned URL.
resource "aws_s3_bucket_cors_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  cors_rule {
    allowed_origins = [var.frontend_origin]
    allowed_methods = ["PUT"]
    allowed_headers = ["content-type"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.documents.arn,
      "${aws_s3_bucket.documents.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "documents" {
  bucket = aws_s3_bucket.documents.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.documents]
}

# Start the PDF processor whenever a PDF lands in uploads/.
resource "aws_s3_bucket_notification" "documents" {
  bucket = aws_s3_bucket.documents.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.processor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "uploads/"
    filter_suffix       = ".pdf"
  }

  depends_on = [aws_lambda_permission.processor_from_s3]
}
