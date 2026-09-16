# SPDX-License-Identifier: GPL-3.0-only

output "resource_group_name" {
  description = "Resource group containing the GeoQA infrastructure."
  value       = azurerm_resource_group.geoqa.name
}

output "storage_account_name" {
  description = "Persistent Azure Storage account name."
  value       = azurerm_storage_account.data_lake.name
}

output "storage_account_id" {
  description = "Persistent Azure Storage account resource ID."
  value       = azurerm_storage_account.data_lake.id
}

output "data_lake_filesystem" {
  description = "Data Lake filesystem name."
  value       = azurerm_storage_data_lake_gen2_filesystem.data.name
}

output "data_lake_filesystem_id" {
  description = "Data Lake filesystem resource ID."
  value       = azurerm_storage_data_lake_gen2_filesystem.data.id
}

output "dfs_endpoint" {
  description = "Data Lake DFS endpoint."
  value       = azurerm_storage_account.data_lake.primary_dfs_endpoint
}

output "container_registry_id" {
  description = "Container Registry resource ID."
  value       = azurerm_container_registry.images.id
}

output "container_registry_login_server" {
  description = "Container Registry login server used for image publishing."
  value       = azurerm_container_registry.images.login_server
}

output "web_api_image_repository" {
  description = "ACR repository for the public web/API image."
  value       = "${azurerm_container_registry.images.login_server}/${var.web_api_image_name}"
}

output "web_app_id" {
  description = "Resource ID of the public web/API Container App."
  value       = azurerm_container_app.web.id
}

output "web_app_fqdn" {
  description = "Public hostname for Catalog inspection and the Live Sandbox."
  value       = azurerm_container_app.web.ingress[0].fqdn
}

output "web_app_url" {
  description = "HTTPS URL for Catalog inspection and the Live Sandbox."
  value       = "https://${azurerm_container_app.web.ingress[0].fqdn}"
}

output "github_oauth_callback_url" {
  description = "Exact callback URL required by the GitHub OAuth application."
  value       = "https://${azurerm_container_app.web.ingress[0].fqdn}/.auth/login/github/callback"
}

output "log_analytics_workspace_id" {
  description = "Execution Log Analytics workspace resource ID."
  value       = azurerm_log_analytics_workspace.execution.id
}

output "container_app_environment_id" {
  description = "Container Apps environment resource ID."
  value       = azurerm_container_app_environment.execution.id
}

output "execution_dispatch_identity_id" {
  description = "Managed identity resource ID for the future API dispatcher workload."
  value       = azurerm_user_assigned_identity.dispatch.id
}

output "execution_dispatch_identity_client_id" {
  description = "Managed identity client ID for the future API dispatcher workload."
  value       = azurerm_user_assigned_identity.dispatch.client_id
}

