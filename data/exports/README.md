# Exports Folder Guidance

This folder stores raw Azure invoice/cost export files.

## Handling very large files
- Keep raw files immutable.
- Do not load whole files into memory for analysis prompts.
- Build partitioned aggregate tables first (date/service/subscription/resource rollups).
- Store derived aggregates in a processing layer and reference those in agent prompts.

## Minimum metadata to track per file
- Source export name/path
- Billing period start/end
- Currency
- Row count
- File size
- Ingestion timestamp
- Hash/checksum (optional but recommended)
