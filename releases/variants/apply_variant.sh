#!/usr/bin/env bash
# Helper to create the 3 long-lived release branches (Architecture p.10, p.17) once Git is initialized.
# Run manually when you are ready to commit and push branches for the reusable CI workflow (PB-05).
set -euo pipefail

declare -A BRANCH_PATCHES=(
  ["release/orders-itemised-tax"]="releases/variants/orders-itemised-tax.patch|feat(orders): itemised tax lines"
  ["release/payments-fraud-n1"]="releases/variants/payments-fraud-n1.patch|feat(payments): historical velocity fraud score"
  ["release/payments-rebuild"]="releases/variants/payments-rebuild.patch|chore(payments): rebuild base image with updated dependencies"
)

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
for branch in "${!BRANCH_PATCHES[@]}"; do
  IFS='|' read -r patch_file commit_msg <<< "${BRANCH_PATCHES[$branch]}"
  git checkout -B "$branch" "$CURRENT_BRANCH"
  git apply "$patch_file"
  git add -A
  git commit -m "$commit_msg"
  echo "Prepared branch $branch ($(git rev-parse --short HEAD))"
done
git checkout "$CURRENT_BRANCH"
