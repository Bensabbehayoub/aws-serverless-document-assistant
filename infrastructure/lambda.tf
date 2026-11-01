# ---------- Packages ----------

# The API Lambda only uses boto3, which the Lambda runtime already includes.
data "archive_file" "api" {
  type        = "zip"
  source_file = "${local.backend_dir}/api/lambda_function.py"
  output_path = "${local.build_dir}/api.zip"
}

# The processor needs pypdf: run `python scripts/build.py` first, which installs it into
# .build/pdf_processor next to the code.
data "archive_file" "processor" {
  type        = "zip"
  source_dir  = "${local.build_dir}/pdf_processor"
  output_path = "${local.build_dir}/pdf_processor.zip"
  excludes    = ["**/__pycache__/**"]
}

# ---------- Log groups (created here so retention is managed) ----------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.api_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "processor" {
  name              = "/aws/lambda/${local.processor_function_name}"
  retention_in_days = var.log_retention_days
}

# ---------- Shared trust policy ----------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ---------- API Lambda ----------

resource "aws_iam_role" "api" {
  name               = "${local.api_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }

  statement {
    sid = "DocumentsTable"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.documents.arn,
      "${aws_dynamodb_table.documents.arn}/index/*",
    ]
  }

  statement {
    sid       = "UploadedPdfs"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.documents.arn}/uploads/*"]
  }

  statement {
    sid       = "ExtractedText"
    actions   = ["s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.documents.arn}/extracted/*"]
  }

  dynamic "statement" {
    for_each = var.ai_provider == "gemini" ? [1] : []
    content {
      sid       = "GeminiApiKey"
      actions   = ["ssm:GetParameter"]
      resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${var.gemini_key_parameter}"]
    }
  }

  dynamic "statement" {
    for_each = var.ai_provider == "bedrock" ? [1] : []
    content {
      sid     = "BedrockModel"
      actions = ["bedrock:InvokeModel"]
      resources = [
        "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_model_id}",
        "arn:${local.partition}:bedrock:*::foundation-model/*",
      ]
    }
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "${local.api_function_name}-access"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_lambda_function" "api" {
  function_name    = local.api_function_name
  description      = "Document Assistant API: documents, uploads, text and questions"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 30 # API Gateway's maximum; AI answers can take a few seconds
  memory_size      = 256

  environment {
    variables = {
      TABLE_NAME       = aws_dynamodb_table.documents.name
      BUCKET_NAME      = aws_s3_bucket.documents.bucket
      OWNER_INDEX      = "owner_id-index"
      AI_PROVIDER      = var.ai_provider
      GEMINI_KEY_PARAM = var.gemini_key_parameter
      MODEL_ID         = var.bedrock_model_id
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.api.name
  }

  depends_on = [aws_iam_role_policy.api]
}

resource "aws_lambda_permission" "api_from_gateway" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

# ---------- PDF processor Lambda ----------

resource "aws_iam_role" "processor" {
  name               = "${local.processor_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "processor" {
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.processor.arn}:*"]
  }

  statement {
    sid       = "ReadUploadedPdfs"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/uploads/*"]
  }

  statement {
    sid       = "WriteExtractedText"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/extracted/*"]
  }

  statement {
    sid       = "UpdateDocumentStatus"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.documents.arn]
  }
}

resource "aws_iam_role_policy" "processor" {
  name   = "${local.processor_function_name}-access"
  role   = aws_iam_role.processor.id
  policy = data.aws_iam_policy_document.processor.json
}

resource "aws_lambda_function" "processor" {
  function_name    = local.processor_function_name
  description      = "Extracts text from uploaded PDFs"
  role             = aws_iam_role.processor.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.processor.output_path
  source_code_hash = data.archive_file.processor.output_base64sha256
  timeout          = 60
  memory_size      = 512

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.documents.name
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.processor.name
  }

  depends_on = [aws_iam_role_policy.processor]
}

resource "aws_lambda_permission" "processor_from_s3" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.processor.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.documents.arn
  source_account = local.account_id
}
