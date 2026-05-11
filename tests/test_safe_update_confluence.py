import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from safe_update_confluence import apply_storage_edit  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
