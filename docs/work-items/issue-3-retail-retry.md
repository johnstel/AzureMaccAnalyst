# Work Item for Issue #3

Issue: https://github.com/johnstel/AzureMaccAnalyst/issues/3

## Scope
- Improve Retail API resilience with retry/backoff

## Implementation checklist
- [ ] Add shared requests Session with HTTPAdapter + Retry
- [ ] Retry transient HTTP/TLS failures with backoff
- [ ] Log warning only after final attempt

## Validation
- [ ] Run app against large CSV and compare warning count
- [ ] Confirm analysis still completes and exports successfully
