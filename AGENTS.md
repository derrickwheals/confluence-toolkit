# Agent Instructions: Portable Confluence Editing Toolkit

This folder provides a portable workflow for reading Confluence pages, editing Markdown locally, previewing changes, and updating Confluence safely from any GitHub repository.

## Required Skill

- Use the bundled `skills/mastering-confluence/SKILL.md` instructions for Confluence operations when a native installed `mastering-confluence` skill is not available.

## Authentication

- Use Confluence REST API authentication with `Authorization: Bearer <token>`.
- Load credentials from `.env` or an explicitly provided env file.
- Do not hardcode tokens in scripts, docs, or prompts.

## Storage Format Retrieval

When retrieving Confluence storage format directly, structure the API URL exactly as:

```text
https://confluence.nttltd.global.ntt/rest/api/content/<page-id>?expand=body.storage
```

Use the configured `CONFLUENCE_URL` for other Confluence instances.

## Safety Rules

- Always create a local backup of the target page before any update in all three formats:
  - Confluence storage format HTML
  - Markdown
  - Confluence wiki markup syntax
- Always run a dry-run preview before upload.
- Always ask for explicit user confirmation before sending updates to Confluence.
- Never upload page changes through MCP tools; use `scripts/upload_confluence_v2.py` or the safe wrapper.
- For existing page edits, always convert the user supplied edit text to Confluence storage format first, then insert or replace that converted fragment inside the original Confluence storage body.
- The only storage text that should change is the exact requested insertion or replacement. Do not re-render and upload the whole page from Markdown unless the user explicitly asks to replace the full page.

## Standard Update Flow

1. Download backup in Markdown and storage HTML:

```bash
python3 scripts/download_confluence.py <PAGE_ID> --env-file .env --output-dir backups/<PAGE_ID>/<TIMESTAMP> --save-html
```

2. Convert downloaded Markdown backup to wiki syntax:

```bash
python3 scripts/convert_markdown_to_wiki.py backups/<PAGE_ID>/<TIMESTAMP>/<PAGE>.md backups/<PAGE_ID>/<TIMESTAMP>/<PAGE>.wiki --strip-frontmatter
```

3. Put the user supplied edit text in `workspace/` as Markdown unless it is already Confluence storage format.
4. Preview the storage-fragment edit:

```bash
python3 scripts/safe_update_confluence.py workspace/<FRAGMENT>.md --page-id <PAGE_ID> --insert-at top --env-file .env
```

Use one explicit location option: `--insert-at`, `--insert-before`, `--insert-after`, or `--replace-selection`. Use `@file` for long exact storage markers or replacement selections.

5. Ask user to confirm update.
6. Upload update through the safe wrapper after confirmation.

## Standard Create Flow

For new pages, convert the user supplied text to Confluence storage format before sending the create request:

```bash
python3 scripts/safe_update_confluence.py workspace/<PAGE>.md --space <SPACE_KEY> --title "<PAGE TITLE>" --parent-id <PARENT_ID> --env-file .env
```

## Preferred Safe Command

Use the wrapper that enforces the full safe flow, including all three backup formats:

```bash
python3 scripts/safe_update_confluence.py workspace/<FRAGMENT>.md --page-id <PAGE_ID> --replace-selection @workspace/old-section.storage --env-file .env
```
