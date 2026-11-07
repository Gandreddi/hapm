output "key_vault_uri" {
  value = azurerm_key_vault.hapm.vault_uri
}

output "aks_name" {
  value = azurerm_kubernetes_cluster.hapm.name
}
