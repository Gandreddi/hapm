# Phase 0 skeleton: validates but is not applied. Network isolation, private endpoints,
# diagnostics and role assignments are listed in docs/backlog.md.
data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "hapm" {
  name     = "${var.prefix}-rg"
  location = var.location
}

resource "azurerm_key_vault" "hapm" {
  name                       = replace("${var.prefix}-kv", "-", "")
  location                   = azurerm_resource_group.hapm.location
  resource_group_name        = azurerm_resource_group.hapm.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
}

resource "azurerm_kubernetes_cluster" "hapm" {
  name                = "${var.prefix}-aks"
  location            = azurerm_resource_group.hapm.location
  resource_group_name = azurerm_resource_group.hapm.name
  dns_prefix          = var.prefix

  default_node_pool {
    name       = "system"
    node_count = 2
    vm_size    = "Standard_D4s_v5"
  }

  identity {
    type = "SystemAssigned"
  }

  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }
}

resource "azurerm_postgresql_flexible_server" "hapm" {
  name                          = "${var.prefix}-pg"
  location                      = azurerm_resource_group.hapm.location
  resource_group_name           = azurerm_resource_group.hapm.name
  version                       = "16"
  sku_name                      = "GP_Standard_D2s_v3"
  storage_mb                    = 65536
  administrator_login           = var.postgres_admin_login
  public_network_access_enabled = false

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = true
  }
}

resource "azurerm_eventhub_namespace" "hapm" {
  name                = "${var.prefix}-eh"
  location            = azurerm_resource_group.hapm.location
  resource_group_name = azurerm_resource_group.hapm.name
  sku                 = "Standard"
}

resource "azurerm_storage_account" "artifacts" {
  name                            = substr(replace("${var.prefix}artifacts", "-", ""), 0, 24)
  location                        = azurerm_resource_group.hapm.location
  resource_group_name             = azurerm_resource_group.hapm.name
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"
}
