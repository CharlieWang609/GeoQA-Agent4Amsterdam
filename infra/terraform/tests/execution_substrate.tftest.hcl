# SPDX-License-Identifier: GPL-3.0-only

mock_provider "azurerm" {}

variables {
  resource_group_name           = "rg-geoqa-test"
  storage_account_name          = "stgeoqatest"
  container_registry_name      = "acrgeoqatest"
  web_api_image_tag             = "mvp-test"
  github_client_id              = "github-client-id"
  external_secrets_key_vault_id = "/subscriptions/test/resourceGroups/rg-secrets/providers/Microsoft.KeyVault/vaults/kv-geoqa-test"
  openai_api_key_secret_id      = "https://kv-geoqa-test.vault.azure.net/secrets/openai-api-key"
  github_client_secret_id       = "https://kv-geoqa-test.vault.azure.net/secrets/github-client-secret"
}

override_resource {
  target          = azurerm_storage_account.data_lake
  override_during = plan
  values = {
    id = "/subscriptions/test/resourceGroups/rg-geoqa-test/providers/Microsoft.Storage/storageAccounts/stgeoqatest"
  }
}

override_resource {
  target          = azurerm_container_registry.images
  override_during = plan
  values = {
    id           = "/subscriptions/test/resourceGroups/rg-geoqa-test/providers/Microsoft.ContainerRegistry/registries/acrgeoqatest"
    login_server = "acrgeoqatest.azurecr.io"
  }
}

override_resource {
  target          = azurerm_log_analytics_workspace.execution
  override_during = plan
  values = {
    id = "/subscriptions/test/resourceGroups/rg-geoqa-test/providers/Microsoft.OperationalInsights/workspaces/log-geoqa-execution"
  }
}

override_resource {
  target          = azurerm_user_assigned_identity.dispatch
  override_during = plan
  values = {
    id        = "/subscriptions/test/resourceGroups/rg-geoqa-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-geoqa-execution-dispatch"
    client_id = "11111111-1111-1111-1111-111111111111"
  }
}

run "provisions_observable_container_platform" {
  command = plan

  assert {
    condition     = azurerm_container_registry.images.name == "acrgeoqatest" && !azurerm_container_registry.images.admin_enabled
    error_message = "The registry must use the configured name without admin credentials."
  }

  assert {
    condition     = azurerm_log_analytics_workspace.execution.retention_in_days == 30
    error_message = "Execution logs must be retained for the configured baseline period."
  }

  assert {
    condition     = azurerm_container_app_environment.execution.log_analytics_workspace_id == azurerm_log_analytics_workspace.execution.id
    error_message = "The Container Apps environment must send logs to Log Analytics."
  }
}

run "grants_web_identity_least_privilege_access" {
  command = plan

  assert {
    condition     = azurerm_user_assigned_identity.dispatch.name == "id-geoqa-execution-dispatch"
    error_message = "The web/API workload must use its managed identity."
  }

  assert {
    condition     = azurerm_role_assignment.dispatch_data_contributor.scope == "${azurerm_storage_account.data_lake.id}/blobServices/default/containers/data" && azurerm_role_assignment.dispatch_data_contributor.role_definition_name == "Storage Blob Data Contributor"
    error_message = "The web/API identity must receive blob contributor access at the data container's ARM scope."
  }

  assert {
    condition     = azurerm_role_assignment.dispatch_registry_pull.role_definition_name == "AcrPull" && azurerm_role_assignment.dispatch_registry_pull.scope == azurerm_container_registry.images.id
    error_message = "The web/API identity must receive pull-only access at registry scope."
  }

  assert {
    condition     = azurerm_role_assignment.dispatch_external_secrets_reader.role_definition_name == "Key Vault Secrets User" && azurerm_role_assignment.dispatch_external_secrets_reader.scope == var.external_secrets_key_vault_id
    error_message = "The web/API identity must read external credentials through Key Vault RBAC."
  }
}

run "expires_all_temporary_sandbox_artifacts" {
  command = plan

  assert {
    condition = toset(azurerm_storage_management_policy.execution_retention.rule[0].filters[0].prefix_match) == toset([
      "data/execution-authorizations/",
      "data/execution-jobs/",
      "data/question-sessions/",
    ])
    error_message = "All temporary session and execution artifacts must share the retention policy."
  }

  assert {
    condition     = azurerm_storage_management_policy.execution_retention.rule[0].actions[0].base_blob[0].delete_after_days_since_creation_greater_than == 7
    error_message = "Temporary artifacts must expire seven days after creation, regardless of later pointer updates."
  }

  assert {
    condition     = length(azurerm_storage_management_policy.execution_retention.rule) == 1
    error_message = "Only the sandbox-artifact retention rule remains after queue removal."
  }
}

run "wires_the_complete_public_application_boundary" {
  command = plan

  assert {
    condition     = azurerm_container_app.web.ingress[0].external_enabled && !azurerm_container_app.web.ingress[0].allow_insecure_connections && azurerm_container_app.web.ingress[0].target_port == 8000
    error_message = "The web/API Container App must expose one HTTPS-only public ingress."
  }

  assert {
    condition     = azurerm_container_app.web.registry[0].identity == azurerm_user_assigned_identity.dispatch.id && azurerm_container_app.web.template[0].container[0].image == "acrgeoqatest.azurecr.io/geoqa-web-api:mvp-test"
    error_message = "The web/API app must pull its versioned image from ACR using its managed identity."
  }

  assert {
    condition     = length([for item in azurerm_container_app.web.template[0].container[0].env : item if item.name == "EXECUTION_QUEUE_NAME"]) == 0
    error_message = "The in-process execution app no longer receives queue wiring."
  }

  assert {
    condition     = one([for item in azurerm_container_app.web.secret : item.key_vault_secret_id if item.name == "openai-api-key"]) == "https://kv-geoqa-test.vault.azure.net/secrets/openai-api-key" && one([for item in azurerm_container_app.web.secret : item.key_vault_secret_id if item.name == "github-client-secret"]) == "https://kv-geoqa-test.vault.azure.net/secrets/github-client-secret"
    error_message = "External credentials must remain Key Vault-backed Container App secrets."
  }

  assert {
    condition     = jsondecode(azurerm_resource_group_template_deployment.web_auth.template_content).resources[0].properties.platform.enabled && jsondecode(azurerm_resource_group_template_deployment.web_auth.template_content).resources[0].properties.globalValidation.unauthenticatedClientAction == "AllowAnonymous" && jsondecode(azurerm_resource_group_template_deployment.web_auth.template_content).resources[0].properties.identityProviders.gitHub.registration.clientId == "github-client-id"
    error_message = "Platform auth must allow anonymous Catalog inspection while configuring GitHub identity claims for Live Sandbox authorization."
  }
}
