import json
import unittest
from pathlib import Path
from unittest.mock import Mock
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from safe_update_confluence import apply_storage_edit, set_page_labels  # noqa: E402


class ApplyStorageEditTests(unittest.TestCase):
    def test_insert_at_top_preserves_original_body(self):
        original = "<p>Existing page</p><p>More</p>"
        fragment = "<p>New intro</p>"

        result = apply_storage_edit(
            original_storage=original,
            fragment_storage=fragment,
            insert_at="top",
        )

        self.assertEqual(result, "<p>New intro</p><p>Existing page</p><p>More</p>")

    def test_insert_after_marker_preserves_surrounding_storage(self):
        original = "<h1>A</h1><p>marker</p><table><tr><td>keep</td></tr></table>"
        fragment = "<p>Inserted</p>"

        result = apply_storage_edit(
            original_storage=original,
            fragment_storage=fragment,
            insert_after="<p>marker</p>",
        )

        self.assertEqual(
            result,
            "<h1>A</h1><p>marker</p><p>Inserted</p><table><tr><td>keep</td></tr></table>",
        )

    def test_replace_selection_changes_only_exact_selection(self):
        original = "<p>Before</p><p>old section</p><p>After</p>"
        fragment = "<p>new section</p>"

        result = apply_storage_edit(
            original_storage=original,
            fragment_storage=fragment,
            replace_selection="<p>old section</p>",
        )

        self.assertEqual(result, "<p>Before</p><p>new section</p><p>After</p>")

    def test_requires_exactly_one_edit_location(self):
        with self.assertRaises(ValueError):
            apply_storage_edit(
                original_storage="<p>Existing</p>",
                fragment_storage="<p>New</p>",
                insert_at="top",
                insert_after="<p>Existing</p>",
            )

    def test_missing_marker_fails_without_modifying_body(self):
        with self.assertRaises(ValueError):
            apply_storage_edit(
                original_storage="<p>Existing</p>",
                fragment_storage="<p>New</p>",
                insert_before="<p>Missing</p>",
            )


class SetPageLabelsTests(unittest.TestCase):
    def test_posts_one_label_object_per_label_to_the_label_endpoint(self):
        session = Mock()
        session.post.return_value = Mock(raise_for_status=Mock(), json=Mock(return_value={"results": []}))

        set_page_labels(session, "https://confluence.example/rest/api", "123456", ["tis-report", "report-platform"])

        session.post.assert_called_once()
        called_url, called_kwargs = session.post.call_args[0][0], session.post.call_args[1]
        self.assertEqual(called_url, "https://confluence.example/rest/api/content/123456/label")
        self.assertEqual(
            json.loads(called_kwargs["data"]),
            [{"prefix": "global", "name": "tis-report"}, {"prefix": "global", "name": "report-platform"}],
        )

    def test_raises_on_http_error(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError("boom")
        session.post.return_value = response

        with self.assertRaises(RuntimeError):
            set_page_labels(session, "https://confluence.example/rest/api", "123456", ["tis-report"])


if __name__ == "__main__":
    unittest.main()
