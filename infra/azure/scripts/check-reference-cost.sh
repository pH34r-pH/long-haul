#!/usr/bin/env bash
# Fail closed unless the exact region has retail prices for the full reference stack.
set -euo pipefail

region="${1:?usage: check-reference-cost.sh <azure-region>}"
sku="${2:-Standard_B1ms}"
hours_per_month=730

case "$sku" in Standard_B1ms) ;; *) echo "refusing non-reference or oversized SKU: $sku" >&2; exit 2;; esac

api='https://prices.azure.com/api/retail/prices'
query() { curl --fail --silent --show-error --connect-timeout 10 --max-time 30 --retry 4 --retry-all-errors --retry-delay 3 --get "$api" --data-urlencode "\$filter=$1"; }
min_price() { jq -er "$1 | min"; }

vm="$(query "serviceName eq 'Virtual Machines' and armRegionName eq '$region' and armSkuName eq '$sku' and priceType eq 'Consumption'")"
vm_hourly="$(printf '%s' "$vm" | min_price '[.Items[] | select(.productName | contains("Windows") | not) | select(.meterName | test("Low Priority|Spot") | not) | .retailPrice]')"
disk="$(query "serviceName eq 'Storage' and armRegionName eq '$region' and priceType eq 'Consumption' and contains(productName, 'Standard HDD')")"
disk_monthly="$(printf '%s' "$disk" | min_price '[.Items[] | select(.skuName == "S4 LRS") | select(.meterName == "S4 LRS Disk") | .retailPrice]')"
ip="$(query "serviceName eq 'Virtual Network' and armRegionName eq '$region' and priceType eq 'Consumption'")"
ip_hourly="$(printf '%s' "$ip" | min_price '[.Items[] | select(.skuName == "Standard") | select(.meterName == "Standard IPv4 Static Public IP") | .retailPrice]')"

monthly="$(awk -v vm="$vm_hourly" -v disk="$disk_monthly" -v ip="$ip_hourly" -v hours="$hours_per_month" 'BEGIN { printf "%.2f", (vm + ip) * hours + disk }')"
printf 'Reference cost estimate (retail USD): VM $%s/hour; OS disk $%s/month; public IP $%s/hour; always-on total ~$%s/month.\n' "$vm_hourly" "$disk_monthly" "$ip_hourly" "$monthly"
awk -v vm="$vm_hourly" -v total="$monthly" 'BEGIN { exit !(vm <= 0.03 && total <= 30.00) }' || { echo 'refusing a VM above $0.03/hour or a stack above $30/month' >&2; exit 3; }
