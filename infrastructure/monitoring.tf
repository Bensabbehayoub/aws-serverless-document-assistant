locals {
  metric_namespace = "DocumentAssistant/${var.environment}"
  alarm_actions    = [aws_sns_topic.alerts.arn]
}

# ---------- Where alerts go ----------

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

# AWS sends a confirmation email first; alerts only arrive after you click "Confirm subscription".
resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------- Count handled extraction errors ----------

# The processor catches its own errors (the PDF is marked "failed"), so Lambda's Errors
# metric never sees them. This turns its JSON ERROR log lines into a metric.
resource "aws_cloudwatch_log_metric_filter" "processor_errors" {
  name           = "${local.name}-processor-errors"
  log_group_name = aws_cloudwatch_log_group.processor.name
  pattern        = "{ $.level = \"ERROR\" }"

  metric_transformation {
    name          = "ProcessorErrors"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
  }
}

# ---------- Alarms ----------

locals {
  alarms = {
    "api-5xx" = {
      description = "The API returned 3 or more server errors in 5 minutes."
      namespace   = "AWS/ApiGateway"
      metric      = "5xx"
      dimensions  = { ApiId = aws_apigatewayv2_api.http.id, Stage = aws_apigatewayv2_stage.default.name }
      threshold   = 3
    }
    "api-lambda-errors" = {
      description = "The API Lambda crashed or timed out."
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      dimensions  = { FunctionName = aws_lambda_function.api.function_name }
      threshold   = 1
    }
    "api-lambda-throttles" = {
      description = "The API Lambda was throttled (concurrency limit reached)."
      namespace   = "AWS/Lambda"
      metric      = "Throttles"
      dimensions  = { FunctionName = aws_lambda_function.api.function_name }
      threshold   = 1
    }
    "processor-lambda-errors" = {
      description = "The PDF processor crashed or timed out."
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      dimensions  = { FunctionName = aws_lambda_function.processor.function_name }
      threshold   = 1
    }
    "processor-extraction-errors" = {
      description = "The PDF processor hit an unexpected error while reading a PDF."
      namespace   = local.metric_namespace
      metric      = "ProcessorErrors"
      dimensions  = {}
      threshold   = 1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = local.alarms

  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dimensions
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.processor_errors]
}

# ---------- Dashboard ----------

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name
  dashboard_body = templatefile("${path.module}/dashboard.json.tftpl", {
    api_id             = aws_apigatewayv2_api.http.id
    stage              = aws_apigatewayv2_stage.default.name
    api_function       = aws_lambda_function.api.function_name
    processor_function = aws_lambda_function.processor.function_name
    region             = var.region
    metric_namespace   = local.metric_namespace
  })
}
