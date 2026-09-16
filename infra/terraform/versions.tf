# SPDX-License-Identifier: GPL-3.0-only

terraform {
  required_version = "~> 1.15"

  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.1"
    }
  }
}
