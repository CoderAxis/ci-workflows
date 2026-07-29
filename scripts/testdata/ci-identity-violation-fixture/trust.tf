# Violation fixture for controls/ci-identity.yaml — every control has at least one case here.
#
# This exists so a detector cannot be quietly narrowed into a no-op. A checker that reports nothing
# reads identically to a clean repository, which is how the four rename outages this catalog was
# written for stayed invisible in the first place. Each block below is a real defect that occurred in
# inboxxhq-infra, reduced to its smallest reproducing form.

locals {
  oidc_provider_sub = "token.actions.githubusercontent.com:sub"
}

# CID-0001. Classic spelling only. This is what every one of the four recurrences looked like: it
# admits every repository you have today and refuses whichever one gets renamed tomorrow.
resource "aws_iam_role" "classic_only" {
  name = "fixture-classic-only"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringLike = {
          (local.oidc_provider_sub) = [
            "repo:${var.github_org}/${var.app_repo}:ref:refs/heads/main",
          ]
        }
      }
    }]
  })
}

# CID-0002. Both spellings present, but the classic one reaches a subject the immutable one does not,
# so `pull_request` access silently disappears the first time this repository is renamed.
resource "aws_iam_role" "asymmetric" {
  name = "fixture-asymmetric"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringLike = {
          (local.oidc_provider_sub) = [
            "repo:${var.github_org}/*:ref:refs/heads/main",
            "repo:${var.github_org}/*:pull_request",
            "repo:${var.github_org}@${var.github_org_id}/*:ref:refs/heads/main",
          ]
        }
      }
    }]
  })
}

# CID-0003. The owner id is wildcarded, which keeps the immutable syntax and discards the only reason
# for it: a recycled owner name would satisfy this again.
resource "aws_iam_role" "wildcard_owner_id" {
  name = "fixture-wildcard-owner-id"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringLike = {
          (local.oidc_provider_sub) = [
            "repo:${var.github_org}/*:ref:refs/heads/main",
            "repo:${var.github_org}@*/*:ref:refs/heads/main",
          ]
        }
      }
    }]
  })
}

# CID-0004, the inversion. Written while following the correct precedent commit, which is why this
# needs a machine check rather than a reviewer: it pins the mutable name and wildcards the immutable
# id, so it breaks on rename AND readmits a recycled name.
data "aws_iam_policy_document" "backwards_pin" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_org}/${var.app_repo}:environment:production",
        "repo:${var.github_org}@${var.github_org_id}/${var.app_repo}@*:environment:production",
      ]
    }
  }
}

# CID-0004, the dead-pattern case. An immutable owner with a bare repository name can never match:
# the immutable format always carries the repository id and it cannot be removed, so this line reads
# as coverage while granting nothing.
data "aws_iam_policy_document" "dead_pattern" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_org}/${var.app_repo}:ref:refs/heads/main",
        "repo:${var.github_org}@${var.github_org_id}/${var.app_repo}:ref:refs/heads/main",
      ]
    }
  }
}
