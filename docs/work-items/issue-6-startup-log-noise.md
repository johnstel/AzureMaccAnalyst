# Work Item for Issue #6

Issue: https://github.com/johnstel/AzureMaccAnalyst/issues/6

## Scope
- Reduce Streamlit rerun startup log noise

## Implementation checklist
- [ ] Emit startup banner once per process/session
- [ ] Keep rerun diagnostics without startup spam

## Validation
- [ ] Run app against large CSV and compare warning count
- [ ] Confirm analysis still completes and exports successfully
