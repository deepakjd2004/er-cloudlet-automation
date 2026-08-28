# Use Akamai Terraform CLI to generate this file for existing cloudlet policy.
terraform {
  required_providers {
    akamai = {
      source  = "akamai/akamai"
      version = ">= 6.2.0"
    }
  }
  required_version = ">= 1.0"
}

provider "akamai" {
  edgerc         = var.edgerc_path
  config_section = var.config_section
}

resource "akamai_cloudlets_policy" "policy" {
  name          = "Test_DJ_ERedirect" # Replace with your desired policy name
  cloudlet_code = "ER"
  description   = "Test (Based on v2) "
  group_id      = "12345" # Replace with your group ID
  match_rules   = data.akamai_cloudlets_edge_redirector_match_rule.match_rules_er.json
  is_shared     = true
}

/*
resource "akamai_cloudlets_policy_activation" "policy_activation_staging" {
  policy_id = tonumber(akamai_cloudlets_policy.policy.id)
  network   = "staging"
  version   = akamai_cloudlets_policy.policy.version
}


resource "akamai_cloudlets_policy_activation" "policy_activation_prod" {
  policy_id             = tonumber(akamai_cloudlets_policy.policy.id)
  network               = "prod"
  version               = akamai_cloudlets_policy.policy.version
}*/
