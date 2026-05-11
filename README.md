# Portable Confluence Toolkit

Copy this folder into another GitHub repository to give that project the same Confluence download, edit, preview, and upload workflow used by this workspace.

## What Is Included

- `scripts/download_confluence.py`: downloads Confluence pages to Markdown and can save storage-format HTML.
- `scripts/upload_confluence_v2.py`: uploads Markdown to Confluence through the REST API.
- `scripts/safe_update_confluence.py`: storage-first wrapper that converts supplied content to Confluence storage format, patches only the requested storage section for edits, creates backups, previews the exact storage diff, requires explicit confirmation, then uploads.
- `scripts/convert_markdown_to_wiki.py`: converts Markdown backups to Confluence wiki markup.
- `scripts/confluence_auth.py`: shared credential discovery and bearer/basic auth support.
- `requirements.txt`: Python package dependencies.
- `.env.example`: credential template.
- `skills/mastering-confluence/`: bundled Confluence operating instructions and references.
- `workspace/`, `backups/`, and `log/`: standard local working folders.

## Install In Another Repo

1. Copy `portable-confluence-toolkit/` into the target repo.
2. From inside the copied folder, create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Create credentials:

```bash
cp .env.example .env
```

4. Edit `.env` and set:

```text
CONFLUENCE_URL=https://confluence.nttltd.global.ntt
CONFLUENCE_AUTH_METHOD=bearer
CONFLUENCE_API_TOKEN=<token>
```

## Download A Page

```bash
python3 scripts/download_confluence.py <PAGE_ID> --env-file .env --output-dir workspace --save-html
```

Add `--download-children` if you need the page hierarchy.

## Safely Edit A Page

Put the Markdown fragment to insert or replace in `workspace/`, then run one of:

```bash
python3 scripts/safe_update_confluence.py workspace/<FRAGMENT>.md --page-id <PAGE_ID> --insert-at top --env-file .env
python3 scripts/safe_update_confluence.py workspace/<FRAGMENT>.md --page-id <PAGE_ID> --insert-after @workspace/marker.storage --env-file .env
python3 scripts/safe_update_confluence.py workspace/<FRAGMENT>.md --page-id <PAGE_ID> --replace-selection @workspace/old-section.storage --env-file .env
```

The wrapper will:

1. Convert the supplied content to Confluence storage format before editing.
2. Back up the current Confluence page under `backups/<PAGE_ID>/<timestamp>/`.
3. Save storage-format HTML in `_html_debug/original_*.html`.
4. Save Markdown backup files.
5. Generate `.wiki` backups from the Markdown backups.
6. Patch the converted storage fragment into the original storage body at the exact requested location.
7. Save the proposed fragment and full proposed storage body.
8. Show a dry-run storage-format diff.
9. Require the confirmation text `update <PAGE_ID>`.
10. Upload through the REST API only after confirmation.

For page edits, the wrapper requires one explicit location option: `--insert-at`, `--insert-before`, `--insert-after`, or `--replace-selection`. This avoids re-rendering the full page and keeps unchanged storage text intact.

## Safely Create A Page

Put the new page Markdown in `workspace/`, then run:

```bash
python3 scripts/safe_update_confluence.py workspace/<PAGE>.md --space <SPACE_KEY> --title "<PAGE TITLE>" --parent-id <PARENT_ID> --env-file .env
```

The wrapper converts the supplied Markdown to Confluence storage format before creating the page. Use `--input-format storage` only when the file is already Confluence storage format.

## Preview Without Uploading

Use `scripts/safe_update_confluence.py` without confirming the requested text. It saves `proposed_fragment.storage`, `proposed_page.storage`, and prints the exact storage diff before asking for confirmation.

## Notes For Human Review

Last action taken:
- 2026-05-06 13:29 AEST: Created this portable toolkit from the Confluence editing workspace scripts and bundled Confluence skill instructions.

Next steps:
- Copy this folder into the target repo.
- Create `.env` from `.env.example` in the copied folder.
- Run a download of a known page ID to confirm credentials and URL are correct before making edits.
- Use `scripts/safe_update_confluence.py` for edits so storage conversion, section-only patching, backups, dry-run preview, and confirmation are enforced.

## Important Constraints

- Do not use MCP tools for Confluence uploads; use `scripts/safe_update_confluence.py` for safe edits and creates, or `scripts/upload_confluence_v2.py` only when replacing a whole page is intentional.
- For existing page edits, never upload a fully re-rendered Markdown page unless the user explicitly asks to replace the whole page. Convert the supplied edit text to storage format and patch that fragment into the original storage body.
- Do not commit `.env` or bearer tokens.
- Convert Mermaid or PlantUML diagrams to image files before upload, then reference them with standard Markdown image syntax.
