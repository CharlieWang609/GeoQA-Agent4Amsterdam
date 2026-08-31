#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Gated application-only in-place redeploy. Catalog ingestion, annotation,
# and pointer operations are deliberately outside this script.

set -euo pipefail

skip_gates=false
if [[ "${1:-}" == "--skip-gates" ]]; then
  skip_gates=true
elif [[ -n "${1:-}" ]]; then
  printf 'Usage: %s [--skip-gates]\n' "$0" >&2
  exit 2
fi

run() {
  printf '\n==> '
  printf '%q ' "$@"
  printf '\n'
  "$@"
}

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

image_tag() {
  local image="$1"
  [[ "$image" == *:* ]] || die "deployed image is not commit-tagged: $image"
  printf '%s\n' "${image##*:}"
}

registry_tag_exists() {
  local repository="$1" tag="$2" tags
  if ! tags=$(az acr repository show-tags \
    --name "$ACR_NAME" \
    --repository "$repository" \
    --output tsv); then
    die "could not verify immutable tags for $repository; refusing to push"
  fi
  grep -Fqx "$tag" <<<"$tags"
}

publish_image() {
  local repository="$1" dockerfile="$2" image staging_id staging_tag staging_image
  image="$REGISTRY/$repository:$SHA"
  if registry_tag_exists "$repository" "$SHA"; then
    printf '\n==> %s:%s already exists in ACR; skipping build and push.\n' \
      "$repository" "$SHA"
    return
  fi

  [[ -r /proc/sys/kernel/random/uuid ]] || \
    die "kernel UUID source is unavailable; cannot create a unique staging tag"
  staging_id=$(</proc/sys/kernel/random/uuid)
  staging_tag="redeploy-staging-${staging_id//-/}"
  staging_image="$REGISTRY/$repository:$staging_tag"

  run docker build \
    --file "$dockerfile" \
    --build-arg "GEOQA_CODE_COMMIT=$SHA" \
    --tag "$image" \
    .
  run docker tag "$image" "$staging_image"
  run docker push "$staging_image"

  # ACR import is an atomic create for the HEAD tag because --force defaults
  # to false. A concurrent publisher therefore causes failure, not overwrite.
  if ! az acr import \
    --name "$ACR_NAME" \
    --source "$staging_image" \
    --image "$repository:$SHA" \
    --output none; then
    az acr repository untag \
      --name "$ACR_NAME" \
      --image "$repository:$staging_tag" \
      --output none || true
    die "could not create immutable tag $repository:$SHA without overwrite"
  fi
  run az acr repository untag \
    --name "$ACR_NAME" \
    --image "$repository:$staging_tag" \
    --output none
  run az acr repository update \
    --name "$ACR_NAME" \
    --image "$repository:$SHA" \
    --write-enabled false \
    --delete-enabled false \
    --output none
}

print_rollback_guidance() {
  printf '\nRollback command (review its saved plan before applying):\n' >&2
  printf '  terraform -chdir=infra/terraform plan -out=rollback.tfplan' >&2
  printf ' -var=%q\n' \
    "web_api_image_tag=$ROLLBACK_TAG" >&2
  printf '  terraform -chdir=infra/terraform apply rollback.tfplan\n' >&2
  printf '%s\n' \
    'Rollback-pair rule: "The Catalog pointer and application revision are one rollback unit. Never repoint the Catalog without reverting the web image, and never revert the workload while leaving the five-Layer pointer active."' >&2
  printf '%s\n' \
    'This script did not change the Catalog pointer; verify it before executing any rollback.' >&2
}

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || \
  die "run this command from the GeoQA Agent Git worktree"
cd "$repo_root"

if [[ -n "$(git status --porcelain)" ]]; then
  die "redeploy requires a clean committed worktree"
fi

SHA=$(git rev-parse HEAD)
readonly SHA

printf '%s\n' \
  "Application-only redeploy for commit $SHA." \
  "No Catalog ingestion, annotation, or pointer operation is performed."

require_command pixi
if [[ "$skip_gates" == false ]]; then
  run bash -n scripts/redeploy.sh
  run pixi run test
  run pixi run typecheck
  run pixi run typecheck-web
  run pixi run build-web
else
  printf '\n==> Local test, typecheck, and web build gates skipped by operator request.\n'
fi

require_command az
require_command docker
require_command terraform

ENV_FILE=${ENV_FILE:-.env}
[[ -f "$ENV_FILE" ]] || die "deployment environment file not found: $ENV_FILE"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID is required in $ENV_FILE}"
: "${TF_VAR_resource_group_name:?TF_VAR_resource_group_name is required in $ENV_FILE}"

ACR_NAME=geoqaagentacr
REGISTRY="$ACR_NAME.azurecr.io"
WEB_APP_NAME=${TF_VAR_web_app_name:-ca-geoqa-web}
readonly ACR_NAME REGISTRY WEB_APP_NAME

if [[ -n "${TF_VAR_container_registry_name:-}" && \
  "$TF_VAR_container_registry_name" != "$ACR_NAME" ]]; then
  die "TF_VAR_container_registry_name must be $ACR_NAME for this in-place redeploy"
fi

run az account set --subscription "$AZURE_SUBSCRIPTION_ID"

current_web_image=$(az containerapp show \
  --name "$WEB_APP_NAME" \
  --resource-group "$TF_VAR_resource_group_name" \
  --query 'properties.template.containers[0].image' \
  --output tsv)

[[ -n "$current_web_image" ]] || die "could not read the currently deployed web/API image"
ROLLBACK_TAG=$(image_tag "$current_web_image")
readonly ROLLBACK_TAG

printf '\nCurrently deployed rollback tag: %s\n' "$ROLLBACK_TAG"
printf '  web/API: %s\n' "$current_web_image"

if ! registry_tag_exists geoqa-web-api "$SHA"; then
  run az acr login --name "$ACR_NAME"
fi
publish_image geoqa-web-api docker/web-api.Dockerfile

plan_dir=$(mktemp -d)
plan_file="$plan_dir/redeploy.tfplan"
plan_log="$plan_dir/plan.log"
plan_display="$plan_dir/plan.txt"
cleanup() {
  rm -f -- "$plan_file" "$plan_log" "$plan_display"
  rmdir "$plan_dir" 2>/dev/null || true
}
trap cleanup EXIT

printf '\n==> terraform -chdir=infra/terraform plan (saved plan)\n'
if ! terraform -chdir=infra/terraform plan \
  -input=false \
  -no-color \
  -out="$plan_file" \
  -var="web_api_image_tag=$SHA" >"$plan_log" 2>&1; then
  cat "$plan_log" >&2
  die "Terraform could not create the saved redeploy plan"
fi
terraform -chdir=infra/terraform show -no-color "$plan_file" >"$plan_display"
cat "$plan_display"

plan_summary=$(grep -E '^(Plan:|No changes\.)' "$plan_display" || true)
target_web_image="$REGISTRY/geoqa-web-api:$SHA"

if [[ "$plan_summary" == "No changes." ]]; then
  if [[ "$ROLLBACK_TAG" != "$SHA" ]]; then
    die "Terraform reported no changes before the target commit was deployed"
  fi
  printf '\nCommit %s is already deployed; no Terraform apply is needed.\n' "$SHA"
  exit 0
fi

expected_summary='Plan: 0 to add, 1 to change, 0 to destroy.'
web_image_changes=$(grep -Fc -- "-> \"$target_web_image\"" "$plan_display" || true)
if [[ "$plan_summary" != "$expected_summary" || "$web_image_changes" -ne 1 ]]; then
  printf '\nERROR: Terraform plan gate rejected this rollout.\n' >&2
  printf 'Expected exactly: %s\n' "$expected_summary" >&2
  printf 'Observed: %s\n' "${plan_summary:-no plan summary}" >&2
  printf '%s\n' \
    'The web/API image must change to the target commit with no resource additions or destroys.' >&2
  printf 'The complete saved-plan diff is displayed above; nothing was applied.\n' >&2
  print_rollback_guidance
  exit 1
fi

reply=""
printf '%s\n' \
  'Review the displayed plan and confirm that the single in-place change is limited to the web/API image.'
printf '\nApply this exact saved plan? [y/N] '
read -r reply || true
if [[ ! "$reply" =~ ^[Yy]$ ]]; then
  printf 'Redeploy cancelled; the saved plan was not applied.\n'
  exit 1
fi

if ! run terraform -chdir=infra/terraform apply -input=false "$plan_file"; then
  printf 'ERROR: Terraform apply failed.\n' >&2
  print_rollback_guidance
  exit 1
fi

revision_name=""
for ((attempt = 1; attempt <= 30; attempt += 1)); do
  revision_name=$(az containerapp revision list \
    --name "$WEB_APP_NAME" \
    --resource-group "$TF_VAR_resource_group_name" \
    --query "[?properties.healthState=='Healthy' && properties.trafficWeight==\`100\` && properties.template.containers[0].image=='$target_web_image'].name | [0]" \
    --output tsv)
  if [[ -n "$revision_name" ]]; then
    break
  fi
  printf 'Waiting for Healthy revision with 100%% traffic (%d/30)...\n' "$attempt"
  sleep 10
done

if [[ -z "$revision_name" ]]; then
  printf 'ERROR: no Healthy revision for %s reached 100%% traffic within 5 minutes.\n' \
    "$target_web_image" >&2
  print_rollback_guidance
  exit 1
fi

printf '\nApplication-only redeploy complete.\n'
printf '  SHA: %s\n' "$SHA"
printf '  Serving revision: %s\n' "$revision_name"
printf '  Previous rollback tag: %s\n' "$ROLLBACK_TAG"
printf '%s\n' '  Catalog ingestion, annotation, and pointer operations were not performed.'
print_rollback_guidance
