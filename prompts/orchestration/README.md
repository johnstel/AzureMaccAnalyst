# Orchestration Prompts

## Files
- `finops-orchestrator-agent-prompt.md`: runtime orchestration prompt
- `finops-orchestrator-playbook.md`: execution playbook and large-file strategy

## How to use
1. Run with `finops-orchestrator-agent-prompt.md` as the system prompt.
2. Inject only the needed sections from playbook per task.
3. Keep outputs aligned to the orchestrator schema for downstream automation.

## Large Export Note
For Azure invoice-detail exports that can reach multi-GB sizes (for example, 10GB), use staged aggregation and pass compact tables to agent steps rather than full raw CSV content.
