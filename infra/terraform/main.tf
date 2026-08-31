# SPDX-License-Identifier: GPL-3.0-only

resource "azurerm_resource_group" "geoqa" {
  name     = var.resource_group_name
  location = var.location
}

resource "azurerm_storage_account" "data_lake" {
  name                = var.storage_account_name
  resource_group_name = azurerm_resource_group.geoqa.name
  location            = azurerm_resource_group.geoqa.location

  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"

  is_hns_enabled = true

  min_tls_version = "TLS1_2"
}

resource "azurerm_storage_data_lake_gen2_filesystem" "data" {
  name               = "data"
  storage_account_id = azurerm_storage_account.data_lake.id
}

# Platform-level safety net behind the application's own 7-day retention:
# even if cleanup never runs, uncurated sandbox artifacts expire here.
resource "azurerm_storage_management_policy" "execution_retention" {
  storage_account_id = azurerm_storage_account.data_lake.id

  rule {
    name    = "expire-uncurated-execution-artifacts"
    enabled = true

    filters {
      prefix_match = [
        "data/execution-authorizations/",
        "data/execution-jobs/",
        "data/question-sessions/",
      ]
      blob_types = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = 7
      }
    }
  }
}

resource "azurerm_container_registry" "images" {
  name                = var.container_registry_name
  resource_group_name = azurerm_resource_group.geoqa.name
  location            = azurerm_resource_group.geoqa.location
  sku                 = "Basic"
  admin_enabled       = false
}

resource "azurerm_log_analytics_workspace" "execution" {
  name                = var.log_analytics_workspace_name
  resource_group_name = azurerm_resource_group.geoqa.name
  location            = azurerm_resource_group.geoqa.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_container_app_environment" "execution" {
  name                       = var.container_app_environment_name
  resource_group_name        = azurerm_resource_group.geoqa.name
  location                   = azurerm_resource_group.geoqa.location
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.execution.id

  # Azure auto-populates the Consumption profile; declaring it keeps
  # plans clean instead of proposing its removal on every run.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

resource "azurerm_user_assigned_identity" "dispatch" {
  name                = "id-geoqa-execution-dispatch"
  resource_group_name = azurerm_resource_group.geoqa.name
  location            = azurerm_resource_group.geoqa.location
}

# The gen2 filesystem resource id is a dfs.core.windows.net data-plane URL;
# role assignments require the ARM container scope built from the account id.
locals {
  data_container_scope = "${azurerm_storage_account.data_lake.id}/blobServices/default/containers/${azurerm_storage_data_lake_gen2_filesystem.data.name}"
}

resource "azurerm_role_assignment" "dispatch_data_contributor" {
  scope                = local.data_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.dispatch.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "dispatch_registry_pull" {
  scope                = azurerm_container_registry.images.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.dispatch.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "dispatch_external_secrets_reader" {
  scope                = var.external_secrets_key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.dispatch.principal_id
  principal_type       = "ServicePrincipal"
}

# Always-on single-replica web/API app (revision_mode Single: one active
# revision, no traffic splitting).
resource "azurerm_container_app" "web" {
  name                         = var.web_app_name
  resource_group_name          = azurerm_resource_group.geoqa.name
  container_app_environment_id = azurerm_container_app_environment.execution.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.dispatch.id]
  }

  registry {
    server   = azurerm_container_registry.images.login_server
    identity = azurerm_user_assigned_identity.dispatch.id
  }

  secret {
    name                = "openai-api-key"
    identity            = azurerm_user_assigned_identity.dispatch.id
    key_vault_secret_id = var.openai_api_key_secret_id
  }

  secret {
    name                = "github-client-secret"
    identity            = azurerm_user_assigned_identity.dispatch.id
    key_vault_secret_id = var.github_client_secret_id
  }

  ingress {
    external_enabled           = true
    allow_insecure_connections = false
    target_port                = 8000

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "web-api"
      image  = "${azurerm_container_registry.images.login_server}/${var.web_api_image_name}:${var.web_api_image_tag}"
      cpu    = var.web_app_cpu
      memory = var.web_app_memory

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.dispatch.client_id
      }

      env {
        name  = "AZURE_STORAGE_ACCOUNT_NAME"
        value = azurerm_storage_account.data_lake.name
      }

      env {
        name  = "DATA_FILESYSTEM_NAME"
        value = azurerm_storage_data_lake_gen2_filesystem.data.name
      }

      env {
        name        = "OPENAI_API_KEY"
        secret_name = "openai-api-key"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/api/health"
      }

      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/api/health"
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.dispatch_data_contributor,
    azurerm_role_assignment.dispatch_registry_pull,
    azurerm_role_assignment.dispatch_external_secrets_reader,
  ]
}

# Easy Auth (GitHub login) is configured through an ARM template because the
# azurerm provider does not expose Container Apps authConfigs natively.
# AllowAnonymous exposes Catalog inspection; the API enforces auth per route.
resource "azurerm_resource_group_template_deployment" "web_auth" {
  name                = "${var.web_app_name}-github-auth"
  resource_group_name = azurerm_resource_group.geoqa.name
  deployment_mode     = "Incremental"
  template_content = jsonencode({
    "$schema"      = "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
    contentVersion = "1.0.0.0"
    resources = [
      {
        type       = "Microsoft.App/containerApps/authConfigs"
        apiVersion = "2025-07-01"
        name       = "${azurerm_container_app.web.name}/current"
        properties = {
          platform = {
            enabled = true
          }
          globalValidation = {
            unauthenticatedClientAction = "AllowAnonymous"
          }
          httpSettings = {
            requireHttps = true
          }
          identityProviders = {
            gitHub = {
              enabled = true
              registration = {
                clientId                = var.github_client_id
                clientSecretSettingName = "github-client-secret"
              }
            }
          }
        }
      }
    ]
  })

  depends_on = [azurerm_container_app.web]
}
