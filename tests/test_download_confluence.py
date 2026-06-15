import tempfile
import unittest
from pathlib import Path
import sys

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from download_confluence import ConfluenceDownloader  # noqa: E402


class FakeResponse:
    status_code = 404


class FakeValidator:
    web_base = "https://confluence.example.test"

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


if __name__ == "__main__":
    unittest.main()
