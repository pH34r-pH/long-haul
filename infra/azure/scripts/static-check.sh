#!/usr/bin/env bash
set -euo pipefail

test "$(rg -n 'Standard_(N|NC|ND|NV|H)\w*' infra/azure .github/workflows/azure-*.yml || true)" = ''
test "$(rg -n 'client[_-]?secret|AZURE_CLIENT[_]SECRET|BEGIN [A-Z ]+PRIVATE[ ]KEY|"'"'"type"'"'"[[:space:]]*:'"'"'"service_account"'"'"'' infra/azure .github/workflows/azure-*.yml || true)" = ''
test "$(rg -n 'osDiskSizeGb.*(3[3-9]|[4-9][0-9]|[1-9][0-9]{2,})' infra/azure || true)" = ''
rg -F 'assert targetResourceGroupIsExpected = resourceGroup().name == expectedResourceGroupName' infra/azure/main.bicep >/dev/null
