output "url" {
  value = "https://${var.domain_name}"
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "service" {
  value = aws_ecs_service.app.name
}

output "efs_id" {
  value = aws_efs_file_system.data.id
}

output "waf_web_acl_arn" {
  value = aws_wafv2_web_acl.main.arn
}

output "image_tag" {
  description = "portfolio-api-demo tag the service is running (pass back as -var image_tag=...)."
  value       = var.image_tag
}

output "mcpo_image_tag" {
  description = "mcpo-demo tag the service is running (pass back as -var mcpo_image_tag=...)."
  value       = var.mcpo_image_tag
}

output "waf_blocklist_cidrs" {
  value = aws_wafv2_ip_set.blocklist.addresses
}
