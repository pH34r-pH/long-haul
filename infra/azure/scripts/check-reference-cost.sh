#!/usr/bin/env bash
# Query Microsoft retail list prices for the exact selected region before any deployment.
set -euo pipefail

region="${1:?usage: check-reference-cost.sh <azure-region>}"
sku="${2:-Standard_B2as_v2}"
hours_per_month=730

if [[ ! "$sku" =~ ^Standard_B2(as_v2|s)$ ]]; then
  echo "refusing non-reference (or GPU) SKU: $sku" >&2
  exit 2
fi

api='https://prices.azure.com/api/retail/prices'
query() {
  curl --fail --silent --show-error --get "$api" --data-urlencode "\$filter=$1"
}

vm_filter="serviceName eq 'Virtual Machines' and armRegionName eq '$region' and armSkuName eq '$sku' and priceType eq 'Consumption'"
vm_json="$(query "$vm_filter")"
vm_hourly="$(jq -er '[.Items[] | select(.productName | contains("Windows") | not) | select(.meterName | test("Low Priority|Spot") | not) | .retailPrice] | min' <<<"$vm_json")"

disk_filter="serviceName eq 'Storage' and armRegionName eq '$region' and priceType eq 'Consumption' and contains(productName, 'Standard SSD')"
disk_json="$(query "$disk_filter")"
disk_monthly="$(jq -er '[.Items[] | select(.skuName == "E6 LRS") | select(.meterName == "E6 LRS Disk") | .retailPrice] | min' <<<"$disk_json")"

ip_filter="serviceName eq 'Virtual Network' and armRegionName eq '$region' and priceType eq 'Consumption'"
ip_json="$(query "$ip_filter")"
ip_hourly="$(jq -er '[.Items[] | select(.skuName == "Standard") | select(.meterName == "Standard IPv4 Static Public IP") | .retailPrice] | min' <<<"$ip_json")"

monthly="$(awk -v vm="$vm_hourly" -v disk="$disk_monthly" -v ip="$ip_hourly" -v hours="$hours_per_month" 'BEGIN { printf "%.2f", (vm + ip) * hours + disk }')"
printf 'Reference cost estimate (retail USD): VM $%s/hour; OS disk $%s/month; public IP $%s/hour; always-on total ~$%s/month.\n' "$vm_hourly" "$disk_monthly" "$ip_hourly" "$monthly"
awk -v vm="$vm_hourly" -v total="$monthly" 'BEGIN { exit !(vm <= 0.10 && total <= 80.00) }' || { echo 'refusing configuration above $0.10/hour VM or $80/month estimate' >&2; exit 3; }
