output "aws_region" {
  value = data.aws_region.current.name
}

output "hosted_home_count" {
  value = var.hosted_home_count
}

output "iot_data_endpoint" {
  value = data.aws_iot_endpoint.data.endpoint_address
}

output "iot_rule_name" {
  value = aws_iot_topic_rule.summary_ingest.name
}

output "basic_ingest_topic_example" {
  value = "$aws/rules/${aws_iot_topic_rule.summary_ingest.name}/imperial-demo/sites/home-0000/nodes/home-0000-n1/summary"
}

output "hosted_thing_group" {
  value = aws_iot_thing_group.hosted_homes.name
}

output "latest_state_table_name" {
  value = aws_dynamodb_table.latest_home_state.name
}

output "cold_copy_bucket_name" {
  value = aws_s3_bucket.summary_cold_copy.bucket
}

output "lambda_function_name" {
  value = aws_lambda_function.summary_ingest.function_name
}

output "cloud_simulator_function_name" {
  value = aws_lambda_function.cloud_simulator.function_name
}

output "dashboard_snapshot_url" {
  value = aws_lambda_function_url.dashboard_snapshot.function_url
}

output "cloud_simulator_enabled" {
  value = var.cloud_simulator_enabled
}

output "cloud_simulator_home_count" {
  value = var.cloud_simulator_home_count
}

output "pause_command" {
  value = "terraform apply -var='iot_rule_enabled=false' -var='cloud_simulator_enabled=false'"
}

output "resume_command" {
  value = "terraform apply -var='iot_rule_enabled=true' -var='cloud_simulator_enabled=true'"
}
