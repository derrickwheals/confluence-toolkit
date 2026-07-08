#!/usr/bin/env python3
"""Safe Confluence page create/update workflow.

This wrapper enforces a loss-prevention flow:
1) Always converts user supplied text to Confluence storage format before upload
2) For page edits, patches that storage fragment into the original storage body
   so only the requested section changes
3) Always backs up the current Confluence page to local disk in three formats:
   - Confluence storage format HTML
   - Markdown
   - Confluence wiki markup
4) Always previews the exact storage-format diff
5) Always asks for explicit interactive confirmation
6) Then performs the real create/update through the REST API
7) Optionally applies one or more labels to the page afterwards (--labels)
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

try:
    from confluence_auth import get_confluence_credentials
except ImportError:
    print("ERROR: confluence_auth module not found. Ensure it's in the same directory.", file=sys.stderr)
    sys.exit(1)


def run_checked(cmd: List[str]) -> None:
    """Run a subprocess and fail fast on non-zero exit codes."""
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def apply_storage_edit(
    original_storage: str,
    fragment_storage: str,
    *,
    insert_at: Optional[str] = None,
    insert_before: Optional[str] = None,
    insert_after: Optional[str] = None,
    replace_selection: Optional[str] = None,
) -> str:
    """Apply one storage-format fragment edit without altering surrounding storage."""
    operations = [
        insert_at is not None,
        insert_before is not None,
        insert_after is not None,
        replace_selection is not None,
    ]
    if sum(operations) != 1:
        raise ValueError(
            "Specify exactly one edit location: --insert-at, --insert-before, "
            "--insert-after, or --replace-selection."
        )

    if insert_at:
        if insert_at == "top":
            return fragment_storage + original_storage
        if insert_at == "bottom":
            return original_storage + fragment_storage
        raise ValueError("--insert-at must be either 'top' or 'bottom'.")

    if insert_before is not None:
        index = original_storage.find(insert_before)
        if index == -1:
            raise ValueError("--insert-before marker was not found in original storage.")
        return original_storage[:index] + fragment_storage + original_storage[index:]

    if insert_after is not None:
        index = original_storage.find(insert_after)
        if index == -1:
            raise ValueError("--insert-after marker was not found in original storage.")
        insert_index = index + len(insert_after)
        return original_storage[:insert_index] + fragment_storage + original_storage[insert_index:]

    if replace_selection is not None:
        index = original_storage.find(replace_selection)
        if index == -1:
            raise ValueError("--replace-selection text was not found in original storage.")
        end_index = index + len(replace_selection)
        return original_storage[:index] + fragment_storage + original_storage[end_index:]

    raise AssertionError("unreachable")


def load_text_argument(value: Optional[str], repo_root: Path) -> Optional[str]:
    """Load marker/selection text from @file syntax or return the literal value."""
    if value is None:
        return None
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.is_absolute():
            path = repo_root / path
        return path.read_text(encoding="utf-8")
    return value


def convert_input_to_storage(content_file: Path, input_format: str) -> Tuple[Dict, str, str, List[str]]:
    """Convert user supplied content to Confluence storage format."""
    if input_format == "storage":
        content = content_file.read_text(encoding="utf-8")
        return {}, content, content_file.stem.replace("_", " "), []

    from upload_confluence_v2 import convert_markdown_to_storage, parse_markdown_file

    frontmatter, markdown_content, title = parse_markdown_file(content_file)
    storage, attachments = convert_markdown_to_storage(markdown_content)
    return frontmatter, storage, title, attachments


def resolve_attachment_paths(attachments: List[str], content_file: Path) -> List[str]:
    """Resolve relative attachment paths against the source content directory."""
    resolved = []
    for attachment in attachments:
        path = Path(attachment)
        if not path.is_absolute():
            candidate = content_file.parent / path
            path = candidate if candidate.exists() else path
        resolved.append(str(path))
    return resolved


def make_session(env_file: Optional[str]) -> Tuple[requests.Session, str, str]:
    """Create an authenticated requests session for Confluence REST calls."""
    creds = get_confluence_credentials(env_file)
    base_url = creds["url"].rstrip("/")
    api_base = urljoin(f"{base_url}/", "rest/api").rstrip("/")
    session = requests.Session()
    if creds.get("auth_method") == "basic":
        session.auth = (creds["username"], creds["token"])
    else:
        session.headers.update({"Authorization": f"Bearer {creds['token'].strip()}"})
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return session, base_url, api_base


def get_page(session: requests.Session, api_base: str, page_id: str) -> Dict:
    """Fetch page metadata and original Confluence storage format."""
    response = session.get(
        f"{api_base}/content/{page_id}",
        params={"expand": "body.storage,version,space,ancestors"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def update_page_storage(
    session: requests.Session,
    api_base: str,
    page: Dict,
    new_storage: str,
    version_message: str,
) -> Dict:
    """Update a page with a fully prepared Confluence storage body."""
    payload = {
        "id": page["id"],
        "type": page.get("type", "page"),
        "title": page["title"],
        "space": {"key": page["space"]["key"]},
        "body": {"storage": {"value": new_storage, "representation": "storage"}},
        "version": {
            "number": int(page["version"]["number"]) + 1,
            "minorEdit": False,
            "message": version_message,
        },
    }
    response = session.put(
        f"{api_base}/content/{page['id']}",
        data=json.dumps(payload),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def create_page_storage(
    session: requests.Session,
    api_base: str,
    *,
    space_key: str,
    title: str,
    storage: str,
    parent_id: Optional[str],
) -> Dict:
    """Create a page from already converted Confluence storage format."""
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }
    if parent_id:
        payload["ancestors"] = [{"id": parent_id}]

    response = session.post(f"{api_base}/content", data=json.dumps(payload), timeout=60)
    response.raise_for_status()
    return response.json()


def set_page_labels(session: requests.Session, api_base: str, page_id: str, labels: List[str]) -> Dict:
    """Add labels to a page. This only adds labels; it never removes existing ones."""
    payload = [{"prefix": "global", "name": label} for label in labels]
    response = session.post(
        f"{api_base}/content/{page_id}/label",
        data=json.dumps(payload),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def print_storage_diff(original_storage: str, new_storage: str) -> None:
    """Print a unified diff of the exact storage body that would be sent."""
    print("\nStorage-format diff:")
    print("-" * 72)
    diff = difflib.unified_diff(
        original_storage.splitlines(),
        new_storage.splitlines(),
        fromfile="original.storage",
        tofile="proposed.storage",
        lineterm="",
    )
    for line in diff:
        print(line)
    print("-" * 72)


def generate_wiki_backups(backup_dir: Path, convert_script: Path) -> List[Path]:
    """Generate .wiki backups from markdown backups."""
    md_files = sorted(
        p for p in backup_dir.glob("*.md")
        if not p.name.startswith("proposed_")
    )
    if not md_files:
        raise RuntimeError(
            f"No markdown backup files found in {backup_dir} after download step."
        )

    created = []
    for md_file in md_files:
        wiki_file = md_file.with_suffix(".wiki")
        run_checked(
            [
                "python3",
                str(convert_script),
                str(md_file),
                str(wiki_file),
                "--strip-frontmatter",
            ]
        )
        created.append(wiki_file)

    return created


def require_tty_confirmation(expected: str) -> None:
    """Require an interactive confirmation before sending updates."""
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Refusing to upload without interactive confirmation (stdin is not a TTY)."
        )

    print("\nConfirmation required before updating Confluence.")
    print(f"Type exactly: {expected}")
    entered = input("> ").strip()

    if entered != expected:
        raise RuntimeError("Confirmation text did not match. Update aborted.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely create or edit Confluence pages using storage-format conversion and exact storage patches",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Insert converted Markdown at the top of an existing page\n"
            "  python3 scripts/safe_update_confluence.py workspace/intro.md --page-id 123456789 --insert-at top\n\n"
            "  # Replace an exact storage selection using converted Markdown\n"
            "  python3 scripts/safe_update_confluence.py workspace/new-section.md --page-id 123456789 --replace-selection @workspace/old-section.storage\n\n"
            "  # Create a new page from converted Markdown\n"
            "  python3 scripts/safe_update_confluence.py workspace/new-page.md --space DEV --title 'New Page'\n\n"
            "  # Create a page and apply labels in the same step\n"
            "  python3 scripts/safe_update_confluence.py workspace/new-page.md --space DEV --title 'New Page' "
            "--labels tis-report,report-platform\n"
        ),
    )

    parser.add_argument("content_file", help="User supplied content to convert to storage format")
    parser.add_argument("--page-id", help="Confluence page ID to edit")
    parser.add_argument("--space", help="Space key for creating a new page")
    parser.add_argument("--title", help="Page title for creating a new page")
    parser.add_argument("--parent-id", help="Parent page ID for creating a new page")
    parser.add_argument(
        "--input-format",
        choices=["markdown", "storage"],
        default="markdown",
        help="Input format for content_file. Markdown is converted with md2cf; storage is used as storage format.",
    )
    parser.add_argument(
        "--insert-at",
        choices=["top", "bottom"],
        help="Insert the converted storage fragment at the top or bottom of the original page storage.",
    )
    parser.add_argument(
        "--insert-before",
        help="Insert before this exact storage text. Use @file to read marker text from a file.",
    )
    parser.add_argument(
        "--insert-after",
        help="Insert after this exact storage text. Use @file to read marker text from a file.",
    )
    parser.add_argument(
        "--replace-selection",
        help="Replace this exact storage text. Use @file to read selection text from a file.",
    )
    parser.add_argument(
        "--labels",
        help="Comma-separated Confluence labels to apply to the page after create/update "
        "(e.g. tis-report,report-platform). Only adds labels; never removes existing ones.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env credentials file")
    parser.add_argument(
        "--backups-dir",
        default="backups",
        help="Base directory for backups (default: backups)",
    )
    parser.add_argument(
        "--force-reupload",
        action="store_true",
        help="Pass through to upload script to re-upload existing attachments",
    )
    parser.add_argument(
        "--skip-dry-run",
        action="store_true",
        help="Skip dry-run preview (not recommended)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    content_file = (repo_root / args.content_file).resolve() if not Path(args.content_file).is_absolute() else Path(args.content_file)

    if not content_file.exists():
        print(f"ERROR: Content file not found: {content_file}", file=sys.stderr)
        return 1

    scripts_dir = repo_root / "scripts"
    download_script = scripts_dir / "download_confluence.py"
    convert_script = scripts_dir / "convert_markdown_to_wiki.py"

    if not download_script.exists() or not convert_script.exists():
        print("ERROR: Required scripts are missing in ./scripts", file=sys.stderr)
        return 1

    if bool(args.page_id) == bool(args.space):
        print("ERROR: Specify exactly one of --page-id for edits or --space for creates.", file=sys.stderr)
        return 1

    edit_location_count = sum(
        value is not None
        for value in [
            args.insert_at,
            args.insert_before,
            args.insert_after,
            args.replace_selection,
        ]
    )
    if args.page_id and edit_location_count != 1:
        print(
            "ERROR: Page edits require exactly one of --insert-at, --insert-before, "
            "--insert-after, or --replace-selection.",
            file=sys.stderr,
        )
        return 1
    if args.space and edit_location_count:
        print("ERROR: Create mode does not accept insert or replace location options.", file=sys.stderr)
        return 1

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_key = args.page_id or f"create-{args.space}"
    backup_dir = (repo_root / args.backups_dir / backup_key / timestamp).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("SAFE CONFLUENCE STORAGE UPDATE")
    print("=" * 72)
    print(f"Mode: {'edit existing page' if args.page_id else 'create new page'}")
    if args.page_id:
        print(f"Page ID: {args.page_id}")
    else:
        print(f"Space: {args.space}")
    print(f"Content: {content_file}")
    print(f"Input format: {args.input_format}")
    print(f"Backup dir: {backup_dir}")

    try:
        frontmatter, fragment_storage, extracted_title, attachments = convert_input_to_storage(
            content_file,
            args.input_format,
        )
        attachments = resolve_attachment_paths(attachments, content_file)
        proposed_copy = backup_dir / f"proposed_source_{content_file.name}"
        shutil.copy2(content_file, proposed_copy)
        fragment_file = backup_dir / "proposed_fragment.storage"
        fragment_file.write_text(fragment_storage, encoding="utf-8")
        print(f"Saved proposed source snapshot: {proposed_copy}")
        print(f"Saved converted storage fragment: {fragment_file}")
        print(f"Converted storage fragment length: {len(fragment_storage)} characters")
        print(f"Attachments found in supplied content: {len(attachments)}")

        session, base_url, api_base = make_session(args.env_file)

        if args.page_id:
            # 1) Always back up existing page content first.
            run_checked(
                [
                    "python3",
                    str(download_script),
                    args.page_id,
                    "--env-file",
                    args.env_file,
                    "--output-dir",
                    str(backup_dir),
                    "--save-html",
                ]
            )

            html_debug_dir = backup_dir / "_html_debug"
            html_files = sorted(html_debug_dir.glob("original_*.html"))
            if not html_files:
                raise RuntimeError(
                    "Storage HTML backup files were not generated. "
                    f"Expected original_*.html in {html_debug_dir}"
                )

            wiki_backups = generate_wiki_backups(backup_dir, convert_script)
            markdown_backups = sorted(
                p for p in backup_dir.glob("*.md")
                if not p.name.startswith("proposed_")
            )

            page = get_page(session, api_base, args.page_id)
            original_storage = page["body"]["storage"]["value"]
            insert_before = load_text_argument(args.insert_before, repo_root)
            insert_after = load_text_argument(args.insert_after, repo_root)
            replace_selection = load_text_argument(args.replace_selection, repo_root)
            new_storage = apply_storage_edit(
                original_storage=original_storage,
                fragment_storage=fragment_storage,
                insert_at=args.insert_at,
                insert_before=insert_before,
                insert_after=insert_after,
                replace_selection=replace_selection,
            )
            proposed_storage_file = backup_dir / "proposed_page.storage"
            proposed_storage_file.write_text(new_storage, encoding="utf-8")

            print(f"Storage HTML backups: {len(html_files)} file(s)")
            print(f"Markdown backups: {len(markdown_backups)} file(s)")
            print(f"Wiki backups: {len(wiki_backups)} file(s)")
            print(f"Saved proposed full storage body: {proposed_storage_file}")
            print(f"Original storage length: {len(original_storage)} characters")
            print(f"Proposed storage length: {len(new_storage)} characters")
            if not args.skip_dry_run:
                print_storage_diff(original_storage, new_storage)

            require_tty_confirmation(f"update {args.page_id}")
            result = update_page_storage(
                session=session,
                api_base=api_base,
                page=page,
                new_storage=new_storage,
                version_message="Safe storage-format section edit",
            )
            page_id_for_attachments = result["id"]
        else:
            title = args.title or frontmatter.get("title") or extracted_title
            new_storage = fragment_storage
            proposed_storage_file = backup_dir / "proposed_page.storage"
            proposed_storage_file.write_text(new_storage, encoding="utf-8")
            print(f"Title: {title}")
            print(f"Saved proposed full storage body: {proposed_storage_file}")
            if not args.skip_dry_run:
                print("\nStorage-format create preview:")
                print("-" * 72)
                print(new_storage[:2000])
                if len(new_storage) > 2000:
                    print("...")
                print("-" * 72)

            require_tty_confirmation(f"create {args.space}")
            result = create_page_storage(
                session=session,
                api_base=api_base,
                space_key=args.space,
                title=title,
                storage=new_storage,
                parent_id=args.parent_id,
            )
            page_id_for_attachments = result["id"]

        if attachments:
            from upload_confluence_v2 import _upload_attachments
            from confluence_auth import get_confluence_client

            confluence = get_confluence_client(env_file=args.env_file)
            _upload_attachments(
                confluence,
                page_id_for_attachments,
                attachments,
                skip_existing=not args.force_reupload,
            )

        if args.labels:
            labels = [label.strip() for label in args.labels.split(",") if label.strip()]
            if labels:
                set_page_labels(session, api_base, page_id_for_attachments, labels)
                print(f"Applied labels: {', '.join(labels)}")

    except subprocess.CalledProcessError as exc:
        print(f"\nERROR: Command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode
    except requests.RequestException as exc:
        print(f"\nERROR: Confluence REST request failed: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    web_url = base_url + result.get("_links", {}).get("webui", "")
    print("\nConfluence operation completed successfully.")
    print(f"Page ID: {result.get('id')}")
    print(f"Version: {result.get('version', {}).get('number', 'unknown')}")
    if web_url:
        print(f"URL: {web_url}")
    print(f"Backup available at: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
