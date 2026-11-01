data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  api_function_name       = "${local.name}-api"
  processor_function_name = "${local.name}-pdf-processor"

  backend_dir = "${path.module}/../backend"
  build_dir   = "${path.module}/../.build"
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}
