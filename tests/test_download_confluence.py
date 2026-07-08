import tempfile
import unittest
from pathlib import Path
import sys

import requests
from markdownify import markdownify as md


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from download_confluence import ConfluenceDownloader  # noqa: E402


class FakeResponse:
    status_code = 404


class FakeValidator:
    web_base = "https://confluence.example.test"
    user_display_names = {
        "user-123": "Manorma Kumari",
    }

    def get_page_info(self, page_id):
        if page_id == "child-missing":
            error = requests.exceptions.HTTPError("404 Client Error")
            error.response = FakeResponse()
            raise error

        return {
            "id": page_id,
            "title": "Parent Page",
            "type": "page",
            "space": {"key": "TSP"},
            "version": {"number": 7},
            "body": {"storage": {"value": "<p>Parent content</p>"}},
            "_links": {"webui": f"/pages/viewpage.action?pageId={page_id}"},
            "ancestors": [],
            "metadata": {"labels": {"results": []}},
        }

    def get_attachments(self, page_id):
        return []

    def get_children(self, page_id):
        if page_id == "parent":
            return [{"id": "child-missing", "title": "Missing Child"}]
        return []

    def get_user_display_name(self, user_key):
        return self.user_display_names.get(user_key)


class CountingValidator:
    web_base = "https://confluence.example.test"

    def __init__(self):
        self.children_calls = {}

    def get_page_info(self, page_id):
        titles = {
            "parent": "Parent Page",
            "child": "Child Page",
        }
        return {
            "id": page_id,
            "title": titles[page_id],
            "type": "page",
            "space": {"key": "TSP"},
            "version": {"number": 1},
            "body": {"storage": {"value": f"<p>{titles[page_id]} content</p>"}},
            "_links": {"webui": f"/pages/viewpage.action?pageId={page_id}"},
            "ancestors": [],
            "metadata": {"labels": {"results": []}},
        }

    def get_attachments(self, page_id):
        return []

    def get_children(self, page_id):
        self.children_calls[page_id] = self.children_calls.get(page_id, 0) + 1
        if page_id == "parent":
            return [{"id": "child", "title": "Child Page"}]
        return []


class DownloadConfluenceTests(unittest.TestCase):
    def test_child_download_failure_makes_parent_tree_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = ConfluenceDownloader(
                FakeValidator(),
                Path(tmpdir),
                download_children=True,
            )

            success, message = downloader.download_page("parent")

        self.assertFalse(success)
        self.assertIn("child-missing", message)
        self.assertIn("Missing Child", message)

    def test_child_lists_are_fetched_once_per_page_when_downloading_tree(self):
        validator = CountingValidator()
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = ConfluenceDownloader(
                validator,
                Path(tmpdir),
                download_children=True,
            )

            success, message = downloader.download_page("parent")

        self.assertTrue(success, message)
        self.assertEqual(validator.children_calls, {"parent": 1, "child": 1})

    def test_data_tables_with_images_remain_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = ConfluenceDownloader(FakeValidator(), Path(tmpdir))

            normalized = downloader._normalize_storage_markup(
                """
                <table>
                  <tbody>
                    <tr><th>Sl#</th><th>Client</th><th>Evidence</th></tr>
                    <tr>
                      <td>1</td>
                      <td>United Overseas Bank Group - UOB</td>
                      <td><img src="Phoenix_pilot_client_QC_attachments/uob.png" alt="uob.png" /></td>
                    </tr>
                  </tbody>
                </table>
                """
            )

        self.assertIn("<table", normalized)
        self.assertIn("<th>Sl#</th>", normalized)
        self.assertIn("<td>United Overseas Bank Group - UOB</td>", normalized)

    def test_table_images_remain_markdown_image_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = ConfluenceDownloader(FakeValidator(), Path(tmpdir))

            normalized = downloader._normalize_storage_markup(
                """
                <table>
                  <tbody>
                    <tr><th>Evidence</th></tr>
                    <tr>
                      <td><img src="Phoenix_pilot_client_QC_attachments/uob.png" alt="uob.png" /></td>
                    </tr>
                  </tbody>
                </table>
                """
            )
            markdown = downloader._clean_markdown(
                md(
                    normalized,
                    heading_style="ATX",
                    bullets="-",
                    code_language="",
                    strip=["script", "style"],
                )
            )

        self.assertIn("| ![uob.png](Phoenix_pilot_client_QC_attachments/uob.png) |", markdown)

    def test_mixed_table_header_cells_become_markdown_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = ConfluenceDownloader(FakeValidator(), Path(tmpdir))

            normalized = downloader._normalize_storage_markup(
                """
                <table>
                  <tbody>
                    <tr><th>Sl#</th><td>Action owner</td></tr>
                    <tr><td>1</td><td>Team A</td></tr>
                  </tbody>
                </table>
                """
            )
            markdown = downloader._clean_markdown(
                md(
                    normalized,
                    heading_style="ATX",
                    bullets="-",
                    code_language="",
                    strip=["script", "style"],
                )
            )

        self.assertTrue(markdown.startswith("| Sl# | Action owner |\n| --- | --- |"))

    def test_user_mentions_in_table_cells_become_display_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = ConfluenceDownloader(FakeValidator(), Path(tmpdir))

            normalized = downloader._normalize_storage_markup(
                """
                <table>
                  <tbody>
                    <tr><th>Defect raised by</th></tr>
                    <tr>
                      <td><ac:link><ri:user ri:userkey="user-123" /></ac:link></td>
                    </tr>
                  </tbody>
                </table>
                """
            )
            markdown = downloader._clean_markdown(
                md(
                    normalized,
                    heading_style="ATX",
                    bullets="-",
                    code_language="",
                    strip=["script", "style"],
                )
            )

        self.assertIn("| Manorma Kumari |", markdown)
        self.assertNotIn("CONFLUENCE:LINK", markdown)


if __name__ == "__main__":
    unittest.main()
