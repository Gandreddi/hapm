variable "prefix" {
  type        = string
  description = "Short name used in every resource name, for example hapm-dev."
}

variable "location" {
  type        = string
  description = "Azure region approved for HAPM."
}

variable "postgres_admin_login" {
  type        = string
  description = "Postgres administrator login. Its password is generated and stored in Key Vault."
  default     = "hapmadmin"
}
