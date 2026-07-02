data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}

locals {
  rule_safe_prefix     = replace(var.project_prefix, "-", "_")
  topic_namespace      = "imperial-demo/sites"
  summary_topic_filter = "${local.topic_namespace}/+/nodes/+/summary"
  rule_name            = "${local.rule_safe_prefix}_summary_ingest"
  hosted_home_ids      = [for i in range(var.hosted_home_count) : format("home-%04d", i)]
  cold_bucket_name     = "${var.project_prefix}-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}"
  cloud_simulator_name = "${var.project_prefix}-cloud-simulator"

  tags = {
    Project     = "ImperialRadarDemo"
    Owner       = "Demo"
    ManagedBy   = "Terraform"
    Environment = "demo"
    CostMode    = "ephemeral"
  }
}

# --- IoT registry: small marker set for the direct-node publisher contract ----
resource "aws_iot_thing_group" "hosted_homes" {
  name = "${var.project_prefix}-hosted-homes"

  properties {
    description = "Hosted marker group for the radar fleet demo"
    attribute_payload {
      attributes = {
        hosted_home_count = tostring(var.hosted_home_count)
        nodes_per_home    = tostring(var.nodes_per_home)
        topology          = "raspberry_pi_nodes_to_cloud_ingest"
      }
    }
  }
}

resource "aws_iot_thing" "home_supervisor" {
  for_each = toset(local.hosted_home_ids)

  name = "${var.project_prefix}-${each.key}"
  attributes = {
    site_id        = each.key
    role           = "pi-node-publisher-marker"
    nodes_per_home = tostring(var.nodes_per_home)
  }
}

resource "aws_iot_thing_group_membership" "hosted_home" {
  for_each = aws_iot_thing.home_supervisor

  thing_name       = each.value.name
  thing_group_name = aws_iot_thing_group.hosted_homes.name
}

resource "aws_iot_policy" "hosted_supervisor_publish" {
  name = "${var.project_prefix}-hosted-supervisor-publish"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["iot:Connect"]
        Resource = [
          "arn:aws:iot:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:client/${var.project_prefix}-*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["iot:Publish"]
        Resource = [
          "arn:aws:iot:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:topic/$aws/rules/${local.rule_name}/${local.topic_namespace}/*/nodes/*/summary"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "iot:GetThingShadow",
          "iot:UpdateThingShadow"
        ]
        Resource = [
          "arn:aws:iot:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:thing/${var.project_prefix}-home-*"
        ]
      }
    ]
  })
}

# --- Cold audit copy ----------------------------------------------------------
resource "aws_s3_bucket" "summary_cold_copy" {
  bucket        = local.cold_bucket_name
  force_destroy = var.force_destroy_bucket
}

resource "aws_s3_bucket_public_access_block" "summary_cold_copy" {
  bucket = aws_s3_bucket.summary_cold_copy.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "summary_cold_copy" {
  bucket = aws_s3_bucket.summary_cold_copy.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "summary_cold_copy" {
  bucket = aws_s3_bucket.summary_cold_copy.id

  rule {
    id     = "expire-demo-summaries"
    status = "Enabled"

    filter {
      prefix = "summaries/"
    }

    expiration {
      days = var.s3_retention_days
    }
  }
}

# --- Hot latest-state store ---------------------------------------------------
resource "aws_dynamodb_table" "latest_home_state" {
  name         = "${var.project_prefix}-latest-home-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "site_id"
  range_key    = "node_id"

  attribute {
    name = "site_id"
    type = "S"
  }

  attribute {
    name = "node_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }
}

# --- Lambda hot ingest --------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/ingest_summary.py"
  output_path = "${path.module}/.terraform-build/ingest_summary.zip"
}

data "archive_file" "cloud_simulator_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/cloud_simulator.py"
  output_path = "${path.module}/.terraform-build/cloud_simulator.zip"
}

data "archive_file" "dashboard_snapshot_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/dashboard_snapshot.py"
  output_path = "${path.module}/.terraform-build/dashboard_snapshot.zip"
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "lambda_ingest" {
  name               = "${var.project_prefix}-lambda-ingest"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda_ingest" {
  statement {
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem"
    ]
    resources = [aws_dynamodb_table.latest_home_state.arn]
  }

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "lambda_ingest" {
  name   = "${var.project_prefix}-lambda-ingest"
  role   = aws_iam_role.lambda_ingest.id
  policy = data.aws_iam_policy_document.lambda_ingest.json
}

resource "aws_cloudwatch_log_group" "lambda_ingest" {
  name              = "/aws/lambda/${var.project_prefix}-summary-ingest"
  retention_in_days = 3
}

resource "aws_lambda_function" "summary_ingest" {
  function_name    = "${var.project_prefix}-summary-ingest"
  role             = aws_iam_role.lambda_ingest.arn
  handler          = "ingest_summary.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      LATEST_TABLE_NAME = aws_dynamodb_table.latest_home_state.name
      ITEM_TTL_SECONDS  = tostring(var.latest_state_ttl_seconds)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_ingest,
    aws_iam_role_policy.lambda_ingest
  ]
}

# --- IoT rule: Basic Ingest -> S3 + Lambda -----------------------------------
data "aws_iam_policy_document" "iot_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "iot_rule" {
  name               = "${var.project_prefix}-iot-rule"
  assume_role_policy = data.aws_iam_policy_document.iot_assume_role.json
}

resource "aws_cloudwatch_log_group" "iot_rule_errors" {
  count = var.enable_rule_cloudwatch_logs ? 1 : 0

  name              = "/aws/iot/${var.project_prefix}/rule-errors"
  retention_in_days = 3
}

data "aws_iam_policy_document" "iot_rule" {
  dynamic "statement" {
    for_each = var.s3_cold_copy_enabled ? [1] : []
    content {
      effect = "Allow"
      actions = [
        "s3:PutObject"
      ]
      resources = ["${aws_s3_bucket.summary_cold_copy.arn}/summaries/*"]
    }
  }

  dynamic "statement" {
    for_each = var.enable_rule_cloudwatch_logs ? [1] : []
    content {
      effect = "Allow"
      actions = [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ]
      resources = ["${aws_cloudwatch_log_group.iot_rule_errors[0].arn}:*"]
    }
  }
}

resource "aws_iam_role_policy" "iot_rule" {
  name   = "${var.project_prefix}-iot-rule"
  role   = aws_iam_role.iot_rule.id
  policy = data.aws_iam_policy_document.iot_rule.json
}

resource "aws_iot_topic_rule" "summary_ingest" {
  name        = local.rule_name
  description = "Radar node summary ingest for the demo AWS mapping"
  enabled     = var.iot_rule_enabled
  sql         = "SELECT *, topic(3) AS site_id_from_topic, topic(5) AS node_id_from_topic, timestamp() AS ingested_at_epoch_ms FROM '${local.summary_topic_filter}'"
  sql_version = "2016-03-23"

  lambda {
    function_arn = aws_lambda_function.summary_ingest.arn
  }

  dynamic "s3" {
    for_each = var.s3_cold_copy_enabled ? [1] : []
    content {
      bucket_name = aws_s3_bucket.summary_cold_copy.bucket
      key         = "summaries/$${topic(3)}/$${topic(5)}/$${timestamp()}.json"
      role_arn    = aws_iam_role.iot_rule.arn
    }
  }

  dynamic "error_action" {
    for_each = var.enable_rule_cloudwatch_logs ? [1] : []
    content {
      cloudwatch_logs {
        log_group_name = aws_cloudwatch_log_group.iot_rule_errors[0].name
        role_arn       = aws_iam_role.iot_rule.arn
      }
    }
  }

  depends_on = [aws_iam_role_policy.iot_rule]
}

resource "aws_lambda_permission" "allow_iot_rule" {
  statement_id  = "AllowExecutionFromIoTRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.summary_ingest.function_name
  principal     = "iot.amazonaws.com"
  source_arn    = aws_iot_topic_rule.summary_ingest.arn
}

# --- Cloud-side hosted simulator ---------------------------------------------
data "aws_iam_policy_document" "cloud_simulator_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "cloud_simulator" {
  name               = "${var.project_prefix}-cloud-simulator"
  assume_role_policy = data.aws_iam_policy_document.cloud_simulator_assume_role.json
}

data "aws_iam_policy_document" "cloud_simulator" {
  statement {
    effect = "Allow"
    actions = [
      "iot:Publish"
    ]
    resources = [
      "arn:aws:iot:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:topic/$aws/rules/${local.rule_name}/${local.topic_namespace}/*/nodes/*/summary"
    ]
  }

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "cloud_simulator" {
  name   = "${var.project_prefix}-cloud-simulator"
  role   = aws_iam_role.cloud_simulator.id
  policy = data.aws_iam_policy_document.cloud_simulator.json
}

resource "aws_cloudwatch_log_group" "cloud_simulator" {
  name              = "/aws/lambda/${local.cloud_simulator_name}"
  retention_in_days = 3
}

resource "aws_lambda_function" "cloud_simulator" {
  function_name    = local.cloud_simulator_name
  role             = aws_iam_role.cloud_simulator.arn
  handler          = "cloud_simulator.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.cloud_simulator_zip.output_path
  source_code_hash = data.archive_file.cloud_simulator_zip.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = {
      IOT_ENDPOINT        = data.aws_iot_endpoint.data.endpoint_address
      IOT_RULE_NAME       = aws_iot_topic_rule.summary_ingest.name
      HOME_COUNT          = tostring(var.cloud_simulator_home_count)
      NODES_PER_HOME      = tostring(var.nodes_per_home)
      POST_PERIOD_SECONDS = tostring(var.cloud_simulator_period_seconds)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.cloud_simulator,
    aws_iam_role_policy.cloud_simulator
  ]
}

resource "aws_cloudwatch_event_rule" "cloud_simulator" {
  count = var.cloud_simulator_enabled ? 1 : 0

  name                = "${var.project_prefix}-cloud-simulator-every-minute"
  description         = "Runs the optional direct-node Imperial radar demo load generator in AWS Lambda"
  schedule_expression = "rate(1 minute)"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "cloud_simulator" {
  count = var.cloud_simulator_enabled ? 1 : 0

  rule      = aws_cloudwatch_event_rule.cloud_simulator[0].name
  target_id = "cloud-simulator"
  arn       = aws_lambda_function.cloud_simulator.arn
  input = jsonencode({
    home_count          = var.cloud_simulator_home_count
    nodes_per_home      = var.nodes_per_home
    post_period_seconds = var.cloud_simulator_period_seconds
  })
}

resource "aws_lambda_permission" "allow_eventbridge_cloud_simulator" {
  count = var.cloud_simulator_enabled ? 1 : 0

  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cloud_simulator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cloud_simulator[0].arn
}

# --- Hosted dashboard read API ----------------------------------------------
resource "aws_iam_role" "dashboard_snapshot" {
  name               = "${var.project_prefix}-dashboard-snapshot"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "dashboard_snapshot" {
  statement {
    effect = "Allow"
    actions = [
      "dynamodb:Scan"
    ]
    resources = [aws_dynamodb_table.latest_home_state.arn]
  }

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "dashboard_snapshot" {
  name   = "${var.project_prefix}-dashboard-snapshot"
  role   = aws_iam_role.dashboard_snapshot.id
  policy = data.aws_iam_policy_document.dashboard_snapshot.json
}

resource "aws_cloudwatch_log_group" "dashboard_snapshot" {
  name              = "/aws/lambda/${var.project_prefix}-dashboard-snapshot"
  retention_in_days = 3
}

resource "aws_lambda_function" "dashboard_snapshot" {
  function_name    = "${var.project_prefix}-dashboard-snapshot"
  role             = aws_iam_role.dashboard_snapshot.arn
  handler          = "dashboard_snapshot.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.dashboard_snapshot_zip.output_path
  source_code_hash = data.archive_file.dashboard_snapshot_zip.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      LATEST_TABLE_NAME   = aws_dynamodb_table.latest_home_state.name
      DASHBOARD_MAX_ITEMS = tostring(var.dashboard_snapshot_max_items)
      CORS_ALLOW_ORIGIN   = var.dashboard_cors_allow_origin
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.dashboard_snapshot,
    aws_iam_role_policy.dashboard_snapshot
  ]
}

resource "aws_lambda_function_url" "dashboard_snapshot" {
  function_name      = aws_lambda_function.dashboard_snapshot.function_name
  authorization_type = "NONE"

  cors {
    allow_credentials = false
    allow_headers     = ["content-type"]
    allow_methods     = ["GET"]
    allow_origins     = [var.dashboard_cors_allow_origin]
    max_age           = 300
  }
}

resource "aws_lambda_permission" "allow_public_dashboard_snapshot_url" {
  statement_id           = "AllowPublicDashboardSnapshotFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.dashboard_snapshot.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "allow_public_dashboard_snapshot_invoke" {
  statement_id  = "AllowPublicDashboardSnapshotInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dashboard_snapshot.function_name
  principal     = "*"
}
