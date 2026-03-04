# Work Item for Issue #4

Issue: https://github.com/johnstel/AzureMaccAnalyst/issues/4

## Scope
- Add reliability tuning knobs for timeout and lookup concurrency

## Implementation checklist
- [ ] Add env var for CSV price lookup worker count
- [ ] Tune safer defaults for timeout and worker count
- [ ] Document knobs in finops_app README

## Validation
- [ ] Run app against large CSV and compare warning count
- [ ] Confirm analysis still completes and exports successfully
