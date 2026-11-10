from datetime import datetime, timezone
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import cleanup_releases as cleaner

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
OLD = {"id": 1, "tag_name": "sonic-202505-1-1", "assets": [],
       "created_at": "2026-11-07T00:00:00Z", "updated_at": "2026-11-07T00:00:00Z", "draft": True}


class CleanupReleases(unittest.TestCase):
    def test_old_empty_draft_and_published_only_and_dry_run(self):
        recent = {**OLD, "id": 2, "created_at": "2026-11-08T00:00:00Z"}
        assets = {**OLD, "id": 3, "assets": [{"name": "metadata.json"}]}
        edited = {**OLD, "id": 4, "updated_at": "2026-11-09T00:00:00Z"}
        for draft in (True, False):
            with self.subTest(draft=draft), patch.object(cleaner, "api") as api:
                api.side_effect = [[{**OLD, "draft": draft}, recent, assets, edited], OLD, []]
                self.assertEqual(cleaner.cleanup("owner/repo", now=NOW), [1])
                self.assertFalse(any("DELETE" in call.args for call in api.call_args_list))

    def test_rechecks_assets_and_age_before_deletion(self):
        for current, assets in (({**OLD, "assets": [{"id": 5}]}, []),
                                ({**OLD, "updated_at": "2026-11-10T00:00:00Z"}, []),
                                (OLD, [{"id": 5}])):
            with self.subTest(current=current, assets=assets), patch.object(cleaner, "api") as api:
                api.side_effect = [[OLD], current, assets]
                self.assertEqual(cleaner.cleanup("owner/repo", apply=True, now=NOW), [])
                self.assertFalse(any("DELETE" in call.args for call in api.call_args_list))

    def test_lists_all_pages_before_deleting_and_keeps_tags(self):
        first = [{**OLD, "id": i, "assets": [{"id": 9}]} for i in range(100)]
        with patch.object(cleaner, "api") as api:
            api.side_effect = [first, [OLD], OLD, [], None]
            self.assertEqual(cleaner.cleanup("owner/repo", apply=True, now=NOW), [1])
            self.assertEqual(api.call_args_list[1].args[1], "/releases?per_page=100&page=2")
            api.assert_called_with("owner/repo", "/releases/1", "DELETE")
            self.assertTrue(all("/git/" not in call.args[1] for call in api.call_args_list))

    def test_api_failure_stops_cleanup_and_short_retention_rejected(self):
        with patch.object(cleaner, "api", side_effect=ValueError("API unavailable")):
            with self.assertRaises(ValueError):
                cleaner.cleanup("owner/repo", apply=True, now=NOW)
        with self.assertRaises(ValueError):
            cleaner.cleanup("owner/repo", days=1, now=NOW)
