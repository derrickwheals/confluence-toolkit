#!/usr/bin/env python3
"""
Confluence Page Downloader - Download and convert Confluence pages to Markdown

Features:
- Downloads complete Confluence pages using REST API with pagination
- Converts Confluence storage format (XHTML) to clean Markdown
- Handles Confluence macros (code blocks with language, children lists, images)
- Downloads all attachments and creates local links
- Supports hierarchical child page downloads to subdirectories
- Creates YAML frontmatter with complete page metadata
- Retries with exponential backoff for failed downloads
- HTML debugging mode for troubleshooting transformations
"""

import os
import re
import sys
import time
import json
import html as html_lib
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from urllib.parse import urljoin, urlparse, quote

import requests
import yaml
from markdownify import markdownify as md

# Import shared credential discovery
try:
    from confluence_auth import get_confluence_credentials
except ImportError:
    print("ERROR: confluence_auth module not found. Ensure it's in the same directory.", file=sys.stderr)
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ConfluenceValidator:
    """Validates downloaded content against Confluence source."""

    def __init__(
        self,
        confluence_url: str,
        username: Optional[str],
        api_token: str,
        auth_method: str = 'basic',
    ):
        self.confluence_url = confluence_url.rstrip('/')
        parsed_base = urlparse(self.confluence_url)
        self.origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        # Supports both root context (/rest/api) and wiki context (/wiki/rest/api)
        self.api_base = urljoin(f"{self.confluence_url}/", "rest/api").rstrip('/')
        self.web_base = self.confluence_url
        self.auth_method = auth_method
        self.session = requests.Session()
        if auth_method == 'bearer':
            self.session.headers.update({'Authorization': f"Bearer {api_token.strip()}"})
        else:
            if not username:
                raise ValueError("Basic auth requires username")
            self.session.auth = (username, api_token)

    def get_page_info(self, page_id: str) -> Dict:
        """Get page metadata from Confluence."""
        url = f"{self.api_base}/content/{page_id}"
        params = {
            'expand': 'body.storage,version,space,ancestors,metadata.labels,history,children.page'
        }

        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def get_children(self, page_id: str) -> List[Dict]:
        """Get all child pages for a page."""
        children = []
        start = 0
        limit = 50

        while True:
            url = f"{self.api_base}/content/{page_id}/child/page"
            params = {
                'start': start,
                'limit': limit
            }

            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            children.extend(data.get('results', []))

            if len(data.get('results', [])) < limit:
                break
            start += limit

        return children

    def get_attachments(self, page_id: str) -> List[Dict]:
        """Get all attachments for a page."""
        attachments = []
        start = 0
        limit = 50

        while True:
            url = f"{self.api_base}/content/{page_id}/child/attachment"
            params = {
                'start': start,
                'limit': limit,
                'expand': 'version,metadata'
            }

            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            attachments.extend(data.get('results', []))

            if len(data.get('results', [])) < limit:
                break
            start += limit

        return attachments

    def download_attachment(self, attachment: Dict, output_dir: Path, page_title: str) -> Optional[Path]:
        """Download an attachment to the page-specific attachments directory."""
        try:
            download_url = attachment['_links'].get('download')
            if not download_url:
                logger.warning(f"No download URL for attachment: {attachment.get('title')}")
                return None

            # Construct full URL from origin to avoid /wiki path duplication issues.
            if download_url.startswith('/'):
                download_url = urljoin(self.origin, download_url)

            # Create page-specific attachments directory
            safe_page_title = self._sanitize_filename(page_title)
            attachments_dir = output_dir / f'{safe_page_title}_attachments'
            attachments_dir.mkdir(exist_ok=True)

            # Sanitize filename
            filename = self._sanitize_filename(attachment['title'])
            filepath = attachments_dir / filename

            # Download file
            logger.info(f"Downloading attachment: {filename}")
            response = self.session.get(download_url, stream=True)
            response.raise_for_status()

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"Downloaded: {filename} ({filepath.stat().st_size} bytes)")
            return filepath

        except Exception as e:
            logger.error(f"Error downloading attachment {attachment.get('title')}: {e}")
            return None

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for filesystem."""
        # Remove or replace invalid characters, replace spaces with underscores
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = re.sub(r'\s+', '_', filename)
        # Limit length
        if len(filename) > 200:
            name, ext = os.path.splitext(filename)
            filename = name[:200-len(ext)] + ext
        return filename


class ConfluenceDownloader:
    """Downloads and converts Confluence pages to Markdown."""

    def __init__(self, validator: ConfluenceValidator, output_dir: Path, save_html: bool = False, download_children: bool = False):
        self.validator = validator
        self.output_dir = output_dir
        self.output_dir.mkdir(exist_ok=True)
        self.save_html = save_html
        self.download_children = download_children
        self._children_cache: Dict[str, List[Dict]] = {}

        # Create HTML debug directory if requested
        if self.save_html:
            self.html_dir = output_dir / '_html_debug'
            self.html_dir.mkdir(exist_ok=True)

    def _get_children(self, page_id: str) -> List[Dict]:
        if page_id not in self._children_cache:
            self._children_cache[page_id] = self.validator.get_children(page_id)
        return self._children_cache[page_id]

    def download_page(self, page_id: str, max_retries: int = 3, target_dir: Optional[Path] = None, parent_title: Optional[str] = None) -> Tuple[bool, str]:
        """
        Download a single page with validation.

        Args:
            page_id: Confluence page ID to download
            max_retries: Number of retry attempts for failed downloads
            target_dir: Optional target directory (defaults to self.output_dir)
            parent_title: Optional parent page title for frontmatter updates

        Returns:
            Tuple of (success: bool, message: str)
        """
        if target_dir is None:
            target_dir = self.output_dir
        for attempt in range(max_retries):
            try:
                logger.info(f"Downloading page {page_id} (attempt {attempt + 1}/{max_retries})")

                # Get page info
                page_info = self.validator.get_page_info(page_id)

                # Extract metadata
                title = page_info['title']
                space_key = page_info['space']['key']
                version = page_info['version']['number']
                content_html = page_info['body']['storage']['value']

                logger.info(f"Page: {title} (v{version})")
                logger.info(f"HTML content length: {len(content_html)} characters")

                # STEP 1: Save original HTML for debugging if requested
                safe_title = self._sanitize_filename(title)
                if self.save_html:
                    # Save original (raw from API)
                    original_html_file = self.html_dir / f"original_{safe_title}.html"
                    with open(original_html_file, 'w', encoding='utf-8') as f:
                        f.write(content_html)

                    # Save formatted (pretty-printed)
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(content_html, 'html.parser')
                        formatted_html = soup.prettify()
                        formatted_html_file = self.html_dir / f"formatted_{safe_title}.html"
                        with open(formatted_html_file, 'w', encoding='utf-8') as f:
                            f.write(formatted_html)
                    except ImportError:
                        # If BeautifulSoup not available, just copy original
                        formatted_html_file = self.html_dir / f"formatted_{safe_title}.html"
                        with open(formatted_html_file, 'w', encoding='utf-8') as f:
                            f.write(content_html)

                    logger.info(f"Saved original and formatted HTML debug files")

                # Get attachments
                attachments = self.validator.get_attachments(page_id)
                logger.info(f"Found {len(attachments)} attachments")

                # Download attachments (pass sanitized page title for folder naming)
                safe_title = self._sanitize_filename(title)
                attachment_paths = {}
                for attachment in attachments:
                    path = self.validator.download_attachment(attachment, target_dir, safe_title)
                    if path:
                        attachment_paths[attachment['title']] = path

                # Convert children macro to HTML list before other transformations
                content_html = self._convert_children_macro(content_html, page_id)

                # Update image links in HTML to point to local attachments
                # This also applies code block and image transformations
                content_html = self._localize_attachment_links(content_html, attachment_paths)

                # Normalize storage markup to avoid markdown conversion data loss.
                content_html = self._normalize_storage_markup(content_html)

                # STEP 2: Save transformed HTML for debugging if requested
                if self.save_html:
                    transformed_html_file = self.html_dir / f"transformed_{safe_title}.html"
                    with open(transformed_html_file, 'w', encoding='utf-8') as f:
                        f.write(content_html)
                    logger.info(f"Saved transformed HTML debug file")

                # STEP 3: Convert HTML to Markdown using markdownify
                logger.info("Converting HTML to Markdown...")
                markdown_content = md(
                    content_html,
                    heading_style="ATX",
                    bullets="-",
                    code_language="",
                    strip=['script', 'style']
                )

                # Clean up markdown
                markdown_content = self._clean_markdown(markdown_content)

                # Save original markdown (before post-processing) for debugging
                if self.save_html:
                    original_md_file = self.html_dir / f"original_{safe_title}.md"
                    with open(original_md_file, 'w', encoding='utf-8') as f:
                        f.write(markdown_content)
                    logger.info(f"Saved original markdown debug file")

                # STEP 4: Post-process markdown to add language tags to code fences
                markdown_content = self._postprocess_code_languages(markdown_content)

                logger.info(f"Markdown content length: {len(markdown_content)} characters")

                # Validate size (markdown should be roughly 50-150% of HTML size due to formatting)
                size_ratio = len(markdown_content) / len(content_html) if content_html else 0
                if size_ratio < 0.3:
                    logger.warning(f"Markdown seems too small (ratio: {size_ratio:.2f})")
                elif size_ratio > 2.0:
                    logger.warning(f"Markdown seems too large (ratio: {size_ratio:.2f})")
                else:
                    logger.info(f"Size validation passed (ratio: {size_ratio:.2f})")

                # Create frontmatter (pass parent_title if in subdirectory)
                frontmatter = self._create_frontmatter(page_info, attachments, parent_title)

                # Generate filename (without page ID)
                safe_title = self._sanitize_filename(title)
                filename = f"{safe_title}.md"
                filepath = target_dir / filename

                # Write file
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write("---\n")
                    f.write(yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True))
                    f.write("---\n\n")
                    f.write(f"# {title}\n\n")
                    f.write(markdown_content)

                file_size = filepath.stat().st_size
                logger.info(f"✅ Downloaded: {filename} ({file_size:,} bytes)")

                # Download children if enabled
                if self.download_children:
                    children_data = self._get_children(page_id)
                    if children_data:
                        # Create subdirectory for children
                        children_dir = target_dir / f"{safe_title}_Children"
                        children_dir.mkdir(exist_ok=True)
                        logger.info(f"📁 Downloading {len(children_data)} children to {children_dir.relative_to(self.output_dir)}/")

                        child_failures = []
                        for child in children_data:
                            child_id = child['id']
                            child_title = child['title']
                            logger.info(f"  ↳ Child: {child_title} ({child_id})")
                            # Recursively download child page
                            child_success, child_message = self.download_page(child_id, max_retries, children_dir, title)
                            if not child_success:
                                child_failures.append(f"{child_title} ({child_id}): {child_message}")

                        if child_failures:
                            message = f"Failed child page download(s) for {title}: " + "; ".join(child_failures)
                            logger.error(message)
                            return False, message

                return True, f"Success: {filename} ({file_size:,} bytes)"

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    msg = f"Page {page_id} not found (404)"
                    logger.error(msg)
                    return False, msg
                elif e.response.status_code == 401:
                    msg = f"Authentication failed for page {page_id}"
                    logger.error(msg)
                    return False, msg
                else:
                    logger.error(f"HTTP error on attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)  # Exponential backoff
            except Exception as e:
                logger.error(f"Error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        return False, f"Failed after {max_retries} attempts"

    def _convert_code_blocks(self, html: str) -> str:
        """Convert Confluence code blocks to HTML with language markers for post-processing."""
        # Pattern to match <ac:structured-macro ac:name="code">...</ac:structured-macro>
        code_block_pattern = re.compile(
            r'<ac:structured-macro[^>]*ac:name="code"[^>]*>(.*?)</ac:structured-macro>',
            re.DOTALL
        )

        def replace_code_block(match):
            code_macro = match.group(1)

            # Extract language parameter if present
            lang_match = re.search(r'<ac:parameter ac:name="language">([^<]+)</ac:parameter>', code_macro)
            language = lang_match.group(1) if lang_match else ''

            # Extract code content from CDATA section
            content_match = re.search(r'<!\[CDATA\[(.*?)\]\]>', code_macro, re.DOTALL)
            if not content_match:
                # Try plain-text-body without CDATA
                content_match = re.search(r'<ac:plain-text-body>(.*?)</ac:plain-text-body>', code_macro, re.DOTALL)

            if not content_match:
                return match.group(0)  # Keep original if we can't parse it

            code_content = content_match.group(1)

            # STEP 2: Create HTML with language marker as <p> tag before code block
            # This marker will be converted to text by markdownify, then post-processed
            if language:
                return f'<p>code-lang:{language}</p><pre><code>{code_content}</code></pre>'
            else:
                return f'<pre><code>{code_content}</code></pre>'

        return code_block_pattern.sub(replace_code_block, html)

    def _convert_children_macro(self, html: str, page_id: str) -> str:
        """Convert Confluence children macro to a markdown list placeholder."""
        # Pattern to match <ac:structured-macro ac:name="children">...</ac:structured-macro>
        children_macro_pattern = re.compile(
            r'<ac:structured-macro[^>]*ac:name="children"[^>]*>.*?</ac:structured-macro>',
            re.DOTALL
        )

        def replace_children_macro(match):
            # Get actual children from API
            try:
                children_data = self._get_children(page_id)
                if children_data:
                    # Create HTML list that will be converted to markdown
                    items = []
                    for child in children_data:
                        child_title = child['title']
                        safe_child_title = self._sanitize_filename(child_title)

                        # If download_children enabled, link to subdirectory
                        if self.download_children:
                            current_title = ""  # Will be set when we have page_info
                            # For now, just create a plain list - paths will be in frontmatter
                            items.append(f"<li>{child_title}</li>")
                        else:
                            items.append(f"<li>{child_title}</li>")

                    return "<ul>\n" + "\n".join(items) + "\n</ul>"
                else:
                    return ""  # No children, remove macro
            except Exception as e:
                logger.warning(f"Error converting children macro: {e}")
                return ""  # Remove macro on error

        return children_macro_pattern.sub(replace_children_macro, html)

    def _localize_attachment_links(self, html: str, attachment_paths: Dict[str, Path]) -> str:
        """Replace Confluence attachment URLs with local file paths."""
        # First, convert Confluence code blocks to standard HTML
        html = self._convert_code_blocks(html)

        # Then convert Confluence <ac:image> tags to standard <img> tags
        html = self._convert_confluence_images(html, attachment_paths)

        # Then replace any remaining attachment URLs
        for attachment_name, local_path in attachment_paths.items():
            # Get relative path from output_dir
            rel_path = local_path.relative_to(self.output_dir)

            # Replace various forms of attachment URLs
            patterns = [
                # Standard attachment URL
                re.compile(rf'/wiki/download/attachments/\d+/{re.escape(quote(attachment_name))}', re.IGNORECASE),
                # Thumbnail URL
                re.compile(rf'/wiki/download/thumbnails/\d+/{re.escape(quote(attachment_name))}', re.IGNORECASE),
                # Simple filename reference
                re.compile(rf'(?<=src=")(?:[^"]*/)?{re.escape(attachment_name)}(?=")', re.IGNORECASE),
            ]

            for pattern in patterns:
                html = pattern.sub(str(rel_path), html)

        return html

    def _normalize_storage_markup(self, html: str) -> str:
        """
        Normalize Confluence storage XHTML before markdown conversion.

        This preserves nested content inside inline <code> tags (for example links
        or Confluence macro tags) and decodes encoded URL entities like '&amp;'.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return html

        soup = BeautifulSoup(html, 'html.parser')

        # Expand selected rich-text macros into their body content so nested
        # images, tables, and links survive markdown conversion.
        self._expand_rich_text_macros(soup)
        self._unwrap_layout_tags(soup)
        self._expand_tables_with_images(soup)
        self._separate_images_from_headings(soup)

        # Replace Confluence-specific tags with explicit placeholders so
        # markdownify cannot silently drop them.
        self._replace_confluence_links_with_placeholders(soup)
        self._replace_confluence_macros_with_placeholders(soup)
        self._replace_remaining_confluence_tags_with_placeholders(soup)

        # markdownify drops nested structure inside <code> and keeps only visible
        # text. Flatten nested data into explicit text first so links/macros survive.
        for code_tag in soup.find_all('code'):
            if code_tag.parent and getattr(code_tag.parent, 'name', None) == 'pre':
                continue
            if not list(code_tag.children):
                continue

            flattened = self._flatten_inline_code_content(code_tag)
            if flattened:
                code_tag.clear()
                code_tag.append(flattened)

        # Decode encoded query-string delimiters in URLs.
        for tag_name, attr_name in (('a', 'href'), ('img', 'src')):
            for tag in soup.find_all(tag_name):
                value = tag.get(attr_name)
                if isinstance(value, str):
                    tag[attr_name] = html_lib.unescape(value)

        return str(soup)

    def _expand_rich_text_macros(self, soup) -> None:
        """Replace selected rich-text macros with their inner content."""
        changed = True
        while changed:
            changed = False
            macro_tags = list(
                soup.find_all(
                    lambda t: (
                        getattr(t, 'name', '')
                        and (
                            getattr(t, 'name') == 'ac:structured-macro'
                            or getattr(t, 'name').endswith(':structured-macro')
                        )
                    )
                )
            )
            for macro_tag in macro_tags:
                rich_body = macro_tag.find(
                    lambda t: (
                        getattr(t, 'name', '')
                        and (
                            getattr(t, 'name') == 'ac:rich-text-body'
                            or getattr(t, 'name').endswith(':rich-text-body')
                        )
                    ),
                    recursive=False,
                )
                if not rich_body:
                    continue

                children = list(rich_body.contents)
                if not children:
                    macro_tag.decompose()
                    changed = True
                    continue

                for child in reversed(children):
                    macro_tag.insert_after(child.extract())
                macro_tag.decompose()
                changed = True

    def _expand_tables_with_images(self, soup) -> None:
        """Flatten layout tables that primarily embed images."""
        tables = list(soup.find_all('table'))
        for table in tables:
            if not table.find('img'):
                continue

            replacement_nodes = []
            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all(['td', 'th'], recursive=False)
                for cell in cells:
                    block = soup.new_tag('div')
                    for child in list(cell.contents):
                        block.append(child.extract())
                    if block.find('img') or block.get_text(" ", strip=True):
                        replacement_nodes.append(block)

            if not replacement_nodes:
                continue

            for node in reversed(replacement_nodes):
                table.insert_after(node)
            table.decompose()

    def _unwrap_layout_tags(self, soup) -> None:
        """Flatten Confluence layout wrappers so nested content survives conversion."""
        layout_tags = list(
            soup.find_all(
                lambda t: (
                    getattr(t, 'name', '')
                    and getattr(t, 'name') in {'ac:layout', 'ac:layout-section', 'ac:layout-cell'}
                )
            )
        )
        for layout_tag in layout_tags:
            layout_tag.unwrap()

    def _separate_images_from_headings(self, soup) -> None:
        """Move inline images out of headings so markdownify preserves them."""
        for heading in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            images = heading.find_all('img')
            if not images:
                continue

            for image in reversed(images):
                paragraph = soup.new_tag('p')
                paragraph.append(image.extract())
                heading.insert_after(paragraph)

    def _replace_confluence_links_with_placeholders(self, soup) -> None:
        """Replace <ac:link> blocks with markdown-safe placeholders."""
        link_tags = list(
            soup.find_all(
                lambda t: (
                    getattr(t, 'name', '')
                    and (
                        getattr(t, 'name') == 'ac:link'
                        or getattr(t, 'name').endswith(':link')
                    )
                )
            )
        )
        for link_tag in link_tags:
            placeholder = self._serialize_confluence_link(link_tag)
            link_tag.replace_with(soup.new_string(placeholder))

    def _replace_confluence_macros_with_placeholders(self, soup) -> None:
        """Replace remaining Confluence macros with explicit placeholders."""
        macro_tags = list(
            soup.find_all(
                lambda t: (
                    getattr(t, 'name', '')
                    and (
                        getattr(t, 'name') == 'ac:structured-macro'
                        or getattr(t, 'name').endswith(':structured-macro')
                    )
                )
            )
        )
        for macro_tag in macro_tags:
            placeholder = self._serialize_confluence_macro(macro_tag, include_body=True)
            macro_tag.replace_with(soup.new_string(placeholder))

    def _replace_remaining_confluence_tags_with_placeholders(self, soup) -> None:
        """Replace any remaining ac:/ri: tags to avoid markdownify drops."""
        remaining_tags = list(
            soup.find_all(
                lambda t: (
                    getattr(t, 'name', '')
                    and (
                        getattr(t, 'name').startswith('ac:')
                        or getattr(t, 'name').startswith('ri:')
                    )
                )
            )
        )
        for tag in remaining_tags:
            placeholder = self._serialize_confluence_tag(tag)
            tag.replace_with(soup.new_string(placeholder))

    def _flatten_inline_code_content(self, code_tag) -> str:
        """Render nested inline-code content into explicit plain text."""
        try:
            from bs4 import NavigableString, Tag
        except ImportError:
            return code_tag.get_text(" ", strip=True)

        def render_node(node) -> str:
            if isinstance(node, NavigableString):
                return str(node)

            if not isinstance(node, Tag):
                return str(node)

            tag_name = (node.name or '').lower()

            if tag_name == 'a':
                label = node.get_text(" ", strip=True)
                href = html_lib.unescape(node.get('href', '')).strip()
                if label and href:
                    return f"{label} ({href})"
                return label or href

            if tag_name.endswith(':structured-macro') or tag_name == 'ac:structured-macro':
                return self._serialize_confluence_macro(node)

            if tag_name.startswith('ac:') or tag_name.startswith('ri:'):
                return self._serialize_confluence_tag(node)

            return ''.join(render_node(child) for child in node.children)

        flattened = ''.join(render_node(child) for child in code_tag.children)
        flattened = html_lib.unescape(flattened)
        flattened = re.sub(r'\s+', ' ', flattened).strip()
        return flattened

    def _serialize_confluence_link(self, link_tag) -> str:
        """Serialize Confluence link tags into explicit placeholders."""
        page_tag = link_tag.find(
            lambda t: getattr(t, 'name', '') in ('ri:page', 'page')
            or getattr(t, 'name', '').endswith(':page')
        )
        attachment_tag = link_tag.find(
            lambda t: getattr(t, 'name', '') in ('ri:attachment', 'attachment')
            or getattr(t, 'name', '').endswith(':attachment')
        )
        url_tag = link_tag.find(
            lambda t: getattr(t, 'name', '') in ('ri:url', 'url')
            or getattr(t, 'name', '').endswith(':url')
        )

        link_text = self._extract_confluence_link_text(link_tag)

        if page_tag:
            return self._format_placeholder(
                'confluence-link',
                {
                    'type': 'page',
                    'title': page_tag.get('ri:content-title') or page_tag.get('content-title'),
                    'space': page_tag.get('ri:space-key') or page_tag.get('space-key'),
                    'id': page_tag.get('ri:content-id') or page_tag.get('content-id'),
                    'text': link_text,
                },
            )

        if attachment_tag:
            return self._format_placeholder(
                'confluence-link',
                {
                    'type': 'attachment',
                    'filename': attachment_tag.get('ri:filename') or attachment_tag.get('filename'),
                    'text': link_text,
                },
            )

        if url_tag:
            return self._format_placeholder(
                'confluence-link',
                {
                    'type': 'url',
                    'url': url_tag.get('ri:value') or url_tag.get('value'),
                    'text': link_text,
                },
            )

        return self._format_placeholder(
            'confluence-link',
            {
                'type': 'unknown',
                'text': link_text or link_tag.get_text(" ", strip=True),
            },
        )

    def _extract_confluence_link_text(self, link_tag) -> str:
        """Extract display text from ac:link rich/plain-text body elements."""
        plain_text_body = link_tag.find(
            lambda t: (
                getattr(t, 'name', '')
                and (
                    getattr(t, 'name') == 'ac:plain-text-link-body'
                    or getattr(t, 'name').endswith(':plain-text-link-body')
                )
            )
        )
        if plain_text_body:
            text = plain_text_body.get_text(" ", strip=True)
            if text:
                return text

        rich_text_body = link_tag.find(
            lambda t: (
                getattr(t, 'name', '')
                and (
                    getattr(t, 'name') == 'ac:link-body'
                    or getattr(t, 'name').endswith(':link-body')
                )
            )
        )
        if rich_text_body:
            text = rich_text_body.get_text(" ", strip=True)
            if text:
                return text

        # Fallback to direct textual nodes that are not resource tags.
        texts = []
        for child in link_tag.children:
            name = getattr(child, 'name', '')
            if name and (name.startswith('ri:') or name.startswith('ac:')):
                continue
            text = getattr(child, 'get_text', lambda *args, **kwargs: str(child))(" ", strip=True) \
                if hasattr(child, 'get_text') else str(child).strip()
            if text:
                texts.append(text)

        return ' '.join(texts).strip()

    def _serialize_confluence_macro(self, macro_tag, include_body: bool = False) -> str:
        """Serialize a Confluence macro to readable plain text."""
        macro_name = (
            macro_tag.get('ac:name')
            or macro_tag.get('name')
            or 'unknown'
        )

        params = []
        for param in macro_tag.find_all(
            lambda t: (
                getattr(t, 'name', '')
                and (
                    getattr(t, 'name') == 'ac:parameter'
                    or getattr(t, 'name').endswith(':parameter')
                )
            )
        ):
            param_name = param.get('ac:name') or param.get('name') or 'param'
            param_value = param.get_text(" ", strip=True)
            if param_value:
                params.append(f"{param_name}={param_value}")

        body_text = ''
        if include_body:
            rich_body = macro_tag.find(
                lambda t: (
                    getattr(t, 'name', '')
                    and (
                        getattr(t, 'name') == 'ac:rich-text-body'
                        or getattr(t, 'name').endswith(':rich-text-body')
                    )
                )
            )
            if rich_body:
                body_text = rich_body.get_text(" ", strip=True)
            else:
                plain_body = macro_tag.find(
                    lambda t: (
                        getattr(t, 'name', '')
                        and (
                            getattr(t, 'name') == 'ac:plain-text-body'
                            or getattr(t, 'name').endswith(':plain-text-body')
                        )
                    )
                )
                if plain_body:
                    body_text = plain_body.get_text(" ", strip=True)

        fields = {'name': macro_name}
        if params:
            fields['params'] = '; '.join(params)
        if body_text:
            fields['body'] = body_text

        return self._format_placeholder('confluence-macro', fields)

    def _serialize_confluence_tag(self, tag) -> str:
        """Serialize other Confluence XML tags to avoid silent text loss."""
        tag_name = (tag.name or '').lower()

        if tag_name.endswith(':page') or tag_name == 'ri:page':
            return self._format_placeholder(
                'confluence-resource',
                {
                    'type': 'page',
                    'title': tag.get('ri:content-title') or tag.get('content-title'),
                    'space': tag.get('ri:space-key') or tag.get('space-key'),
                    'id': tag.get('ri:content-id') or tag.get('content-id'),
                },
            )

        if tag_name.endswith(':url') or tag_name == 'ri:url':
            return self._format_placeholder(
                'confluence-resource',
                {
                    'type': 'url',
                    'url': tag.get('ri:value') or tag.get('value'),
                },
            )

        if tag_name.endswith(':attachment') or tag_name == 'ri:attachment':
            filename = tag.get('ri:filename') or tag.get('filename')
            if filename:
                return self._format_placeholder(
                    'confluence-resource',
                    {
                        'type': 'attachment',
                        'filename': filename,
                    },
                )

        text = tag.get_text(" ", strip=True)
        if text:
            return text

        attrs = []
        for key, value in tag.attrs.items():
            if isinstance(value, list):
                value = ' '.join(value)
            attrs.append(f"{key}={value}")

        if attrs:
            return self._format_placeholder(
                'confluence-resource',
                {'tag': tag_name, 'attrs': ' '.join(attrs)},
            )
        return self._format_placeholder('confluence-resource', {'tag': tag_name})

    def _format_placeholder(self, kind: str, fields: Dict[str, Optional[str]]) -> str:
        """Build a markdown-safe placeholder string."""
        parts = []
        for key, value in fields.items():
            if value is None:
                continue
            value_str = self._sanitize_placeholder_value(str(value))
            if not value_str:
                continue
            parts.append(f'{key}="{value_str}"')
        suffix = kind.upper().replace('CONFLUENCE-', '').replace('-', ':')
        kind_token = f"CONFLUENCE:{suffix}"
        return f"{kind_token}({', '.join(parts)})"

    def _sanitize_placeholder_value(self, value: str) -> str:
        """Normalize placeholder values to avoid malformed markdown tokens."""
        value = html_lib.unescape(value)
        value = re.sub(r'\s+', ' ', value).strip()
        value = value.replace('\\', '\\\\').replace('"', '\\"')
        return value

    def _convert_confluence_images(self, html: str, attachment_paths: Dict[str, Path]) -> str:
        """Convert Confluence <ac:image> tags to standard HTML <img> tags with local paths."""
        # Pattern to match <ac:image>...</ac:image> blocks
        ac_image_pattern = re.compile(
            r'<ac:image[^>]*>(.*?)</ac:image>',
            re.DOTALL
        )

        def replace_ac_image(match):
            ac_image_block = match.group(0)
            inner_content = match.group(1)

            # Extract filename from <ri:attachment ri:filename="...">
            filename_match = re.search(r'<ri:attachment[^>]+ri:filename="([^"]+)"', inner_content)
            if not filename_match:
                return ac_image_block  # Keep original if no filename found

            filename = filename_match.group(1)

            # Find the local path for this attachment
            local_path = attachment_paths.get(filename)
            if not local_path:
                # Try without URL parameters
                base_filename = filename.split('?')[0]
                local_path = attachment_paths.get(base_filename)

            if not local_path:
                return ac_image_block  # Keep original if attachment not found

            # Get relative path
            rel_path = local_path.relative_to(self.output_dir)
            rel_path_str = quote(str(rel_path), safe="/._-")

            # Extract alt text from ac:alt attribute
            alt_match = re.search(r'ac:alt="([^"]*)"', ac_image_block)
            alt_text = alt_match.group(1) if alt_match else filename

            # Create standard HTML img tag
            return f'<img src="{rel_path_str}" alt="{alt_text}" />'

        return ac_image_pattern.sub(replace_ac_image, html)

    def _clean_markdown(self, markdown: str) -> str:
        """Clean up markdown formatting."""
        # Remove excessive blank lines
        markdown = re.sub(r'\n{3,}', '\n\n', markdown)

        # Fix code blocks - ensure proper spacing
        markdown = re.sub(r'```(\w*)\n+', r'```\1\n', markdown)
        markdown = re.sub(r'\n+```', r'\n```', markdown)

        # Clean up table formatting
        markdown = re.sub(r'\|\s*\n\s*\|', '|\n|', markdown)

        # Remove trailing whitespace
        lines = [line.rstrip() for line in markdown.split('\n')]
        markdown = '\n'.join(lines)

        return markdown.strip() + '\n'

    def _postprocess_code_languages(self, markdown: str) -> str:
        """STEP 4: Post-process markdown to add language tags to code fences."""
        # Pattern: code-lang:LANGUAGE followed by newline and code fence
        # Replace with just the code fence with language
        pattern = re.compile(
            r'code-lang:(\w+)\s*\n\s*```\s*\n',
            re.MULTILINE
        )

        def add_lang_to_fence(match):
            language = match.group(1)
            return f'```{language}\n'

        markdown = pattern.sub(add_lang_to_fence, markdown)

        # Also handle case where there might be extra whitespace
        # code-lang:json\n\n```
        pattern2 = re.compile(
            r'code-lang:(\w+)\s*\n+```',
            re.MULTILINE
        )
        markdown = pattern2.sub(lambda m: f'```{m.group(1)}\n', markdown)

        return markdown

    def _create_frontmatter(self, page_info: Dict, attachments: List[Dict], parent_title: Optional[str] = None) -> Dict:
        """Create YAML frontmatter from page info with hierarchy.

        Args:
            page_info: Page metadata from Confluence API
            attachments: List of attachment metadata
            parent_title: If in a subdirectory, the parent page title for path calculation
        """
        # Construct full Confluence URL
        confluence_url = f"{self.validator.web_base}{page_info['_links']['webui']}"

        frontmatter = {
            'title': page_info['title'],
            'confluence_url': confluence_url,
            'confluence': {
                'id': page_info['id'],
                'space': page_info['space']['key'],
                'version': page_info['version']['number'],
                'type': page_info['type']
            },
            'exported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'exported_by': 'confluence_downloader',
            'validation': {
                'html_content_length': len(page_info['body']['storage']['value']),
                'status': 'validated'
            }
        }

        # Add full breadcrumb path from ancestors
        if page_info.get('ancestors'):
            breadcrumb = []
            for ancestor in page_info['ancestors']:
                breadcrumb.append({
                    'id': ancestor['id'],
                    'title': ancestor['title']
                })
            # Add current page to breadcrumb
            breadcrumb.append({
                'id': page_info['id'],
                'title': page_info['title']
            })
            frontmatter['breadcrumb'] = breadcrumb

        # Add direct parent info if available
        if page_info.get('ancestors'):
            parent = page_info['ancestors'][-1]
            safe_parent_title = self._sanitize_filename(parent['title'])
            # If we're in a subdirectory (parent_title provided), parent is up one level
            parent_path = f"../{safe_parent_title}.md" if parent_title else f"{safe_parent_title}.md"
            frontmatter['parent'] = {
                'id': parent['id'],
                'title': parent['title'],
                'file': parent_path
            }

        # Add children if available
        children_data = self._get_children(page_info['id'])
        if children_data:
            # If download_children is enabled, children will be in subdirectory
            current_title = self._sanitize_filename(page_info['title'])
            if self.download_children:
                # Children are in {Page_Title}_Children/ subdirectory
                frontmatter['children'] = [
                    {
                        'id': child['id'],
                        'title': child['title'],
                        'file': f"{current_title}_Children/{self._sanitize_filename(child['title'])}.md"
                    }
                    for child in children_data
                ]
            else:
                # Children are in same directory
                frontmatter['children'] = [
                    {
                        'id': child['id'],
                        'title': child['title'],
                        'file': f"{self._sanitize_filename(child['title'])}.md"
                    }
                    for child in children_data
                ]

        # Add labels if available
        if page_info.get('metadata', {}).get('labels', {}).get('results'):
            labels = [label['name'] for label in page_info['metadata']['labels']['results']]
            frontmatter['confluence']['labels'] = labels

        # Add attachment info
        if attachments:
            frontmatter['attachments'] = [
                {
                    'id': att['id'],
                    'title': att['title'],
                    'media_type': att['metadata'].get('mediaType', 'unknown'),
                    'file_size': att.get('extensions', {}).get('fileSize', 0)
                }
                for att in attachments
            ]

        return frontmatter

    def _sanitize_filename(self, title: str) -> str:
        """Convert title to safe filename."""
        # Replace spaces with underscores, remove special chars
        safe = re.sub(r'[^\w\s-]', '', title)
        safe = re.sub(r'[-\s]+', '_', safe)
        # Limit length
        if len(safe) > 100:
            safe = safe[:100]
        return safe.strip('_')


def load_configuration(env_file: Optional[str] = None, output_override: Optional[str] = None) -> Dict:
    """
    Load configuration using shared credential discovery.

    Args:
        env_file: Optional path to specific .env file
        output_override: Optional output directory override

    Returns:
        Dict with confluence_url, username, api_token, auth_method, output_dir
    """
    try:
        creds = get_confluence_credentials(env_file=env_file)
    except ValueError as e:
        logger.error(f"Credential discovery failed: {e}")
        logger.info("\nCreate one of these files with credentials:")
        logger.info("  .env, .env.confluence, .env.jira, .env.atlassian")
        logger.info("\nRequired variables:")
        logger.info("  CONFLUENCE_URL=https://yourcompany.atlassian.net")
        logger.info("  Basic:  CONFLUENCE_USERNAME=your.email@company.com + CONFLUENCE_API_TOKEN")
        logger.info("  Bearer: CONFLUENCE_BEARER_TOKEN or CONFLUENCE_API_TOKEN")
        logger.info("\nGet API Token: https://id.atlassian.com/manage-profile/security/api-tokens")
        sys.exit(1)

    return {
        'confluence_url': creds['url'],
        'username': creds.get('username'),
        'api_token': creds['token'],
        'auth_method': creds.get('auth_method', 'basic'),
        'output_dir': output_override or os.getenv('CONFLUENCE_OUTPUT_DIR', 'confluence_docs')
    }


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Download Confluence pages to Markdown',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Single page
  %(prog)s 123456789

  # Multiple pages
  %(prog)s 123456 456789 789012

  # From file
  %(prog)s --page-ids-file page_ids.txt

  # With child pages in subdirectories
  %(prog)s --download-children 123456789

  # With HTML debugging
  %(prog)s --save-html 123456789

  # Custom output directory
  %(prog)s --output-dir ./docs 123456789

  # Custom .env file
  %(prog)s --env-file /path/to/.env 123456789
        '''
    )

    parser.add_argument('page_ids', nargs='*', help='Page IDs to download')
    parser.add_argument('--env-file', default='.env', help='Path to .env file (default: .env)')
    parser.add_argument('--output-dir', help='Output directory (overrides .env CONFLUENCE_OUTPUT_DIR)')
    parser.add_argument('--download-children', action='store_true', help='Download child pages to subdirectories')
    parser.add_argument('--save-html', action='store_true', help='Save intermediate HTML files for debugging')
    parser.add_argument('--page-ids-file', help='File containing page IDs (one per line)')

    args = parser.parse_args()

    # Load configuration
    config = load_configuration(args.env_file, args.output_dir)

    # Setup output directory
    output_dir = Path(config['output_dir'])
    output_dir.mkdir(exist_ok=True)

    # Initialize validator and downloader
    validator = ConfluenceValidator(
        config['confluence_url'],
        config['username'],
        config['api_token'],
        config.get('auth_method', 'basic'),
    )
    downloader = ConfluenceDownloader(validator, output_dir, save_html=args.save_html, download_children=args.download_children)

    if args.save_html:
        logger.info("HTML debug mode enabled - saving original XHTML to _html_debug/")
    if args.download_children:
        logger.info("Child page download enabled - children will be downloaded to {Parent_Name}_Children/ subdirectories")

    # Get page IDs
    if args.page_ids_file:
        page_ids_file = Path(args.page_ids_file)
        if not page_ids_file.exists():
            logger.error(f"Page IDs file not found: {page_ids_file}")
            sys.exit(1)

        with open(page_ids_file) as f:
            page_ids = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith('#')
            ]
    elif args.page_ids:
        page_ids = args.page_ids
    else:
        logger.error("No page IDs specified. Use page_ids arguments or --page-ids-file")
        parser.print_help()
        sys.exit(1)

    # Download pages
    logger.info(f"Downloading {len(page_ids)} pages...")
    results = []

    for i, page_id in enumerate(page_ids, 1):
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing page {i}/{len(page_ids)}: {page_id}")
        logger.info(f"{'='*60}")

        success, message = downloader.download_page(page_id)
        results.append({
            'page_id': page_id,
            'success': success,
            'message': message
        })

        # Rate limiting
        if i < len(page_ids):
            time.sleep(1)

    # Print summary
    logger.info(f"\n{'='*60}")
    logger.info("DOWNLOAD SUMMARY")
    logger.info(f"{'='*60}")

    successful = sum(1 for r in results if r['success'])
    failed = len(results) - successful

    logger.info(f"✅ Successful: {successful}/{len(results)}")
    logger.info(f"❌ Failed: {failed}/{len(results)}")

    if failed > 0:
        logger.info("\nFailed pages:")
        for result in results:
            if not result['success']:
                logger.info(f"  - {result['page_id']}: {result['message']}")

    # Write results to JSON
    results_file = output_dir / 'download_results.json'
    with open(results_file, 'w') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'total': len(results),
            'successful': successful,
            'failed': failed,
            'results': results
        }, f, indent=2)

    logger.info(f"\nResults saved to: {results_file}")


if __name__ == '__main__':
    main()
