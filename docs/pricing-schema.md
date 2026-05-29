# `pricing.json` schema

Update `docs/assets/data/pricing.json` quarterly. Bump `_meta.last_verified`.

| Key | Type | Notes |
|---|---|---|
| `_meta.currency` | string | ISO 4217 code shown in the calculator UI. |
| `_meta.last_verified` | string (YYYY-MM-DD) | Drives the "rates last verified" badge. |
| `regions.<id>.modifier` | number | Multiplier applied to every line item for the region. |
| `compute_hourly.<sku>` | number | USD/hour for the given AML managed online endpoint SKU. |
| `reservation_discount.<term>` | number | Decimal discount (0.31 = 31% off). |
| `requests_per_instance_per_minute` | number | Used to recommend instance count from target throughput. |
| `storage_per_gb_month.<tier>` | number | Hot / Cool / Archive USD per GB-month. |
| `storage_per_gb_month.transactions_per_10k` | number | USD per 10k blob transactions. |
| `networking.private_endpoint_hour` | number | USD per private endpoint per hour. |
| `networking.egress_per_gb` | number | USD per outbound GB. |
| `networking.expressroute_*_month` | number | Port + circuit USD per month. |
| `acr_standard_month` | number | ACR Standard tier flat. |
| `key_vault_per_10k_ops` | number | USD per 10k Key Vault ops. |
| `app_insights_per_gb` / `log_analytics_per_gb` | number | Per-GB ingestion. |
| `ai_search.<tier>_month` | number | Basic / S1 / S2 flat. |
| `cosmos_db.ru_per_100_per_hour` | number | RU/s pricing. |
| `azure_health_data_services.*` | number | DICOM service storage + transactions. `deid_per_instance_hour` is reserved for future use (kept at 0 — De-id is consumption-based per resource, not per hour). |
| `service_bus.standard_base_month` | number | Flat USD/month for a Service Bus Standard namespace (sessions queue). |
| `service_bus.standard_ops_per_million` | number | USD per million billable operations above the free tier. |
| `service_bus.free_ops_per_million` | number | Operations included free per month (in millions). |
| `event_grid.ops_per_million` | number | USD per million Event Grid operations above the free tier. |
| `event_grid.free_ops_per_month` | number | Operations included free per month. |
| `container_apps.*` | number | vCPU-second, GiB-second, per million requests. |
| `support_plan_month.<plan>` | number | Flat monthly support fee. |
| `defender_for_cloud_per_resource_month` | number | Per protected resource. |

**All values are placeholders.** Always re-validate against the official
[Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/) before quoting customers.
