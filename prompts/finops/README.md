# FinOps Prompts

## Files
- `azure-finops-agent-prompt.md`: original long-form prompt draft
- `azure-finops-agent-prompt.v2.md`: lean runtime prompt (recommended for active agent execution)
- `azure-finops-playbook.md`: companion playbook for deeper procedures and KPI guidance

## Recommended Usage
1. Use `azure-finops-agent-prompt.v2.md` as the system/runtime prompt.
2. Pull relevant sections from `azure-finops-playbook.md` as task-specific context.
3. Preserve the output contract from v2 for downstream orchestration compatibility.

## Why this split
- Lower token usage
- More deterministic agent outputs
- Easier maintenance and versioning
