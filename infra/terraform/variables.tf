# SPDX-License-Identifier: GPL-3.0-only

variable "location" {
  description = "Azure region for GeoQA infrastructure."
  type        = string
  default     = "West Europe"
}

variable "resource_group_name" {
  description = "Azure resource group name."
  type        = string
}

variable "storage_account_name" {
  description = "Unique Azure Storage Account name."
  type        = string
}

variable "container_registry_name" {
  description = "Globally unique Azure Container Registry name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9]{5,50}$", var.container_registry_name))
    error_message = "The container registry name must contain 5-50 alphanumeric characters."
  }
}

variable "container_app_environment_name" {
  description = "Container Apps environment name for isolated execution jobs."
  type        = string
  default     = "cae-geoqa-execution"
}

variable "log_analytics_workspace_name" {
  description = "Log Analytics workspace name for execution observability."
  type        = string
  default     = "log-geoqa-execution"
}

variable "web_app_name" {
  description = "Public Container App serving Catalog inspection and the FastAPI Live Sandbox."
  type        = string
  default     = "ca-geoqa-web"
}

variable "web_api_image_name" {
  description = "ACR repository name for the combined web/API image."
  type        = string
  default     = "geoqa-web-api"
}

variable "web_api_image_tag" {
  description = "Immutable deployment tag for the combined web/API image."
  type        = string

  validation {
    condition     = length(trimspace(var.web_api_image_tag)) > 0 && lower(var.web_api_image_tag) != "latest"
    error_message = "The web/API image tag must be immutable and must not be latest."
  }
}

variable "web_app_cpu" {
  description = "Benchmark-derived vCPU allocation for the web/API container."
  type        = number
  default     = 0.5
}

variable "web_app_memory" {
  description = "Benchmark-derived memory allocation for the web/API container."
  type        = string
  default     = "1Gi"
}

variable "github_client_id" {
  description = "Public client id of the GitHub OAuth application used by Container Apps authentication."
  type        = string
}

variable "external_secrets_key_vault_id" {
  description = "Azure resource ID of the RBAC-enabled Key Vault holding external credentials."
  type        = string
}

variable "openai_api_key_secret_id" {
  description = "Versionless or versioned Key Vault secret URI for the OpenAI API key."
  type        = string
  sensitive   = true
}

variable "github_client_secret_id" {
  description = "Versionless or versioned Key Vault secret URI for the GitHub OAuth client secret."
  type        = string
  sensitive   = true
}

