# Work Item for Issue #5

Issue: https://github.com/johnstel/AzureMaccAnalyst/issues/5

## Scope
- Add in-run cache for retail price lookups

## Implementation checklist
- [ ] Add thread-safe in-memory cache for (service, sku, region)
- [ ] Use cache in CSV and advisor lookup paths
- [ ] Add cache hit/miss logging

## Validation
- [ ] Run app against large CSV and compare warning count
- [ ] Confirm analysis still completes and exports successfully
