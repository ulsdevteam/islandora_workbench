"""unittest tests for the export_member_media task -- both the
workbench_utils.py functions it added and the MemberMediaExporter class
in workbench_export.py. Does not require a live Drupal; all Drupal-facing
calls (get_media_list, get_term_name, issue_request, etc.) are mocked.

Companion to tests/unit_tests_workbench_config.py's convention of a
separate file per self-contained feature area, rather than appending to
the monolithic tests/unit_tests.py.
"""

import os
import sys
import csv
import argparse
import unittest
import tempfile
from unittest.mock import patch, MagicMock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import workbench_utils
import workbench_export
from workbench_export import MemberMediaExporter
from WorkbenchConfig import WorkbenchConfig


def make_config(**overrides):
    """Build a minimal config dict sufficient for MemberMediaExporter's
    pure-logic methods. Real runs use WorkbenchConfig.get_config(), but
    none of the methods tested here need anything beyond plain dict
    lookups/.get() calls.
    """
    config = {
        "host": "https://example.com",
        "username": "test",
        "password": "test",
        "export_file_directory": "/tmp/export_member_media_test",
        "include_member_media": True,
        "member_media_max_depth": None,
        "export_member_media_use_types": [],
        "export_member_media_report_only": False,
        "export_member_media_filename_format": "default",
        "export_file_url_instead_of_download": False,
        "progress_bar": False,
    }
    config.update(overrides)
    return config


def make_exporter(**config_overrides):
    return MemberMediaExporter(make_config(**config_overrides), args=None)


class TestBuildFilename(unittest.TestCase):
    """Covers the "default" and "export" filename formats, including the
    real bugs found during manual testing: the blank-weight segment
    (fixed with the "noweight" placeholder) and the "root" placeholder
    for the starting/root node.
    """

    def test_default_format(self):
        exporter = make_exporter(export_member_media_filename_format="default")
        result = exporter._build_filename(
            node_id="1332", weight="3", media_id="3507", media_index=1,
            media_use_label="Original-File", extension=".jp2",
        )
        self.assertEqual(result, "1332-3507-1-Original-File.jp2")

    def test_export_format_numeric_weight_is_zero_padded(self):
        exporter = make_exporter(export_member_media_filename_format="export")
        result = exporter._build_filename(
            node_id="1334", weight="3", media_id="3515", media_index=1,
            media_use_label="Original-File", extension=".jp2",
        )
        self.assertEqual(result, "1334-0003-0001-Original-File.jp2")

    def test_export_format_large_weight_and_index_still_pad_to_four_digits(self):
        exporter = make_exporter(export_member_media_filename_format="export")
        result = exporter._build_filename(
            node_id="1334", weight="127", media_id="3515", media_index=42,
            media_use_label="Original-File", extension=".jp2",
        )
        self.assertEqual(result, "1334-0127-0042-Original-File.jp2")

    def test_export_format_root_weight_is_not_padded(self):
        exporter = make_exporter(export_member_media_filename_format="export")
        result = exporter._build_filename(
            node_id="1162", weight="root", media_id="2997", media_index=1,
            media_use_label="Original-File", extension=".jpg",
        )
        self.assertEqual(result, "1162-root-0001-Original-File.jpg")

    def test_export_format_missing_weight_uses_noweight_placeholder(self):
        # Regression test: prior to the fix, an empty-string weight
        # produced a blank filename segment, e.g.
        # "1337--0001-Original-File.jp2" (double hyphen). This is exactly
        # what real data from a flat photograph collection with no
        # field_weight produced.
        exporter = make_exporter(export_member_media_filename_format="export")
        for missing_weight in (None, "", "not-a-number"):
            with self.subTest(weight=missing_weight):
                result = exporter._build_filename(
                    node_id="1337", weight=missing_weight, media_id="3527",
                    media_index=1, media_use_label="Original-File", extension=".jp2",
                )
                self.assertEqual(result, "1337-noweight-0001-Original-File.jp2")
                self.assertNotIn("--", result)


class TestSanitizeLabel(unittest.TestCase):

    def test_spaces_become_hyphens(self):
        self.assertEqual(MemberMediaExporter._sanitize_label("Original File"), "Original-File")

    def test_illegal_characters_are_stripped(self):
        self.assertEqual(
            MemberMediaExporter._sanitize_label('Foo/Bar\\Baz:Qux*?"<>|,'),
            "FooBarBazQux",
        )

    def test_plain_label_is_unchanged_besides_whitespace_trim(self):
        self.assertEqual(MemberMediaExporter._sanitize_label("  Thumbnail  "), "Thumbnail")


class TestResolveMediaUseLabelString(unittest.TestCase):

    def test_none_or_empty_returns_no_media_use_placeholder(self):
        exporter = make_exporter()
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(exporter._resolve_media_use_label_string(value), "NoMediaUse")

    @patch("workbench_export.get_term_name")
    def test_single_term_resolves_and_sanitizes(self, mock_get_term_name):
        mock_get_term_name.return_value = "Original File"
        exporter = make_exporter()
        self.assertEqual(exporter._resolve_media_use_label_string("16"), "Original-File")
        mock_get_term_name.assert_called_once_with(exporter.config, "16")

    @patch("workbench_export.get_term_name")
    def test_multiple_terms_join_with_plus(self, mock_get_term_name):
        mock_get_term_name.side_effect = lambda config, tid: {
            "15": "Service File", "16": "Original File",
        }[tid]
        exporter = make_exporter()
        self.assertEqual(
            exporter._resolve_media_use_label_string("15,16"),
            "Service-File+Original-File",
        )

    @patch("workbench_export.get_term_name")
    def test_unresolvable_term_falls_back_to_raw_id(self, mock_get_term_name):
        mock_get_term_name.return_value = False  # get_term_name's documented failure return
        exporter = make_exporter()
        self.assertEqual(exporter._resolve_media_use_label_string("99"), "99")

    @patch("workbench_export.get_term_name")
    def test_repeated_term_id_is_only_looked_up_once(self, mock_get_term_name):
        # Caching matters at scale: a run across hundreds of nodes sharing
        # the same handful of Media Use terms should not re-fetch the same
        # term name from Drupal for every single file.
        mock_get_term_name.return_value = "Original File"
        exporter = make_exporter()
        exporter._resolve_media_use_label_string("16")
        exporter._resolve_media_use_label_string("16")
        exporter._resolve_media_use_label_string("16")
        self.assertEqual(mock_get_term_name.call_count, 1)


class TestGetAllMediaFilesForNode(unittest.TestCase):
    """The most important regression coverage in this file: a synthetic
    media list standing in for what get_media_list() would normally
    fetch over HTTP, specifically exercising the query-string/extension
    bug found during manual testing against real Thumbnail Image derivatives.
    """

    def _media_list(self):
        return [
            {
                "mid": [{"value": "3507"}],
                "field_media_use": [{"target_id": "16"}],
                "field_media_file": [
                    {"url": "https://example.com/files/original.jp2"}
                ],
            },
            {
                "mid": [{"value": "3508"}],
                "field_media_use": [{"target_id": "5"}],
                "field_media_image": [
                    {
                        "url": "https://s3.example.com/thumb.jpg"
                        "?VersionId=c7JDtoqeVPDTqvclHLNuld0l5aG__FTJ"
                    }
                ],
            },
        ]

    @patch("workbench_utils.get_media_list")
    def test_query_string_is_stripped_from_filename_but_not_from_url(self, mock_get_media_list):
        # Regression test for the real bug found in manual testing: a
        # signed/tokenized URL's query string was being included as part
        # of the derived "filename", corrupting extension detection (e.g.
        # producing ".jpg?VersionId=..." as the "extension"). The fix
        # must strip the query string ONLY for filename purposes -- the
        # "url" field must remain fully intact, since signed URLs
        # typically require their query string to authenticate at all.
        mock_get_media_list.return_value = self._media_list()
        config = make_config()

        entries = workbench_utils.get_all_media_files_for_node(config, "1337")

        thumbnail_entry = next(e for e in entries if e["media_id"] == "3508")
        self.assertEqual(thumbnail_entry["filename"], "thumb.jpg")
        self.assertNotIn("?", thumbnail_entry["filename"])
        self.assertEqual(
            thumbnail_entry["url"],
            "https://s3.example.com/thumb.jpg?VersionId=c7JDtoqeVPDTqvclHLNuld0l5aG__FTJ",
        )

    @patch("workbench_utils.get_media_list")
    def test_plain_url_without_query_string_still_works(self, mock_get_media_list):
        mock_get_media_list.return_value = self._media_list()
        config = make_config()

        entries = workbench_utils.get_all_media_files_for_node(config, "1337")

        original_entry = next(e for e in entries if e["media_id"] == "3507")
        self.assertEqual(original_entry["filename"], "original.jp2")
        self.assertEqual(original_entry["media_use_type"], "16")

    @patch("workbench_utils.get_media_list")
    def test_no_media_returns_empty_list(self, mock_get_media_list):
        mock_get_media_list.return_value = []
        config = make_config()
        self.assertEqual(workbench_utils.get_all_media_files_for_node(config, "9999"), [])

    @patch("workbench_utils.get_media_list")
    def test_allowed_media_use_tids_filters_correctly(self, mock_get_media_list):
        mock_get_media_list.return_value = self._media_list()
        config = make_config()

        # Only the "Original File" (tid 16) media should come through.
        entries = workbench_utils.get_all_media_files_for_node(
            config, "1337", allowed_media_use_tids=["16"]
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["media_id"], "3507")

    @patch("workbench_utils.get_media_list")
    def test_no_filter_returns_all_media_use_types(self, mock_get_media_list):
        mock_get_media_list.return_value = self._media_list()
        config = make_config()
        entries = workbench_utils.get_all_media_files_for_node(
            config, "1337", allowed_media_use_tids=None
        )
        self.assertEqual(len(entries), 2)


class TestResolveMediaUseTermIds(unittest.TestCase):

    def test_numeric_ids_pass_through_unchanged(self):
        result = workbench_utils.resolve_media_use_term_ids(make_config(), ["16", "5"])
        self.assertEqual(result, ["16", "5"])

    @patch("workbench_utils.get_all_representations_of_term")
    def test_names_are_resolved_via_lookup(self, mock_lookup):
        mock_lookup.return_value = {"term_id": "16"}
        result = workbench_utils.resolve_media_use_term_ids(make_config(), ["Original File"])
        self.assertEqual(result, ["16"])

    @patch("workbench_utils.get_all_representations_of_term")
    def test_unresolvable_value_is_skipped_not_fatal(self, mock_lookup):
        mock_lookup.return_value = False  # get_all_representations_of_term's documented failure return
        result = workbench_utils.resolve_media_use_term_ids(make_config(), ["Nonexistent Type"])
        self.assertEqual(result, [])


class TestGetAllowedMediaUseTids(unittest.TestCase):
    """MemberMediaExporter._get_allowed_media_use_tids() -- this method
    went through several behavior changes during manual testing (warn
    and continue with fewer types -> hard-fail only if NOTHING resolves
    -> hard-fail on ANY unresolved value, to match check_input()'s
    strictness), and had zero test coverage until now despite being the
    source of a real, dangerous bug found in production testing: an
    empty resolved list is falsy in Python, and was being treated the
    same as "no filter" (export everything), silently doing the OPPOSITE
    of what a misconfigured filter asked for.
    """

    def test_no_filter_configured_returns_none(self):
        exporter = make_exporter(export_member_media_use_types=[])
        with patch("workbench_export.resolve_media_use_term_ids") as mock_resolve:
            result = exporter._get_allowed_media_use_tids()
        self.assertIsNone(result)
        mock_resolve.assert_not_called()

    @patch("workbench_export.resolve_media_use_term_ids")
    def test_all_values_resolve_returns_resolved_list(self, mock_resolve):
        mock_resolve.return_value = ["16", "5"]
        exporter = make_exporter(
            export_member_media_use_types=["Original File", "Thumbnail Image"]
        )
        result = exporter._get_allowed_media_use_tids()
        self.assertEqual(result, ["16", "5"])

    @patch("workbench_export.resolve_media_use_term_ids")
    def test_any_unresolved_value_hard_fails(self, mock_resolve):
        # Regression test for the real bug: 1 of 2 configured values
        # resolving must NOT be treated as "close enough" -- it must
        # stop the run, matching check_input()'s --check-time strictness
        # exactly, per the explicit design decision to make both layers
        # consistent with each other.
        mock_resolve.return_value = ["16"]  # only 1 resolved, 2 were configured
        exporter = make_exporter(
            export_member_media_use_types=["Original File", "Thumbnail"]
        )
        with self.assertRaises(SystemExit) as exit_info:
            exporter._get_allowed_media_use_tids()
        self.assertEqual(exit_info.exception.code, 1)

    @patch("workbench_export.resolve_media_use_term_ids")
    def test_zero_values_resolve_also_hard_fails(self, mock_resolve):
        mock_resolve.return_value = []  # nothing resolved at all
        exporter = make_exporter(export_member_media_use_types=["Thumbnail"])
        with self.assertRaises(SystemExit) as exit_info:
            exporter._get_allowed_media_use_tids()
        self.assertEqual(exit_info.exception.code, 1)

    @patch("workbench_export.resolve_media_use_term_ids")
    def test_resolution_is_only_performed_once_per_run(self, mock_resolve):
        # Caching matters here too: resolve_media_use_term_ids() itself
        # makes HTTP calls for name/URI lookups, so this should not be
        # re-run on every node in a large collection.
        mock_resolve.return_value = ["16"]
        exporter = make_exporter(export_member_media_use_types=["Original File"])
        exporter._get_allowed_media_use_tids()
        exporter._get_allowed_media_use_tids()
        exporter._get_allowed_media_use_tids()
        self.assertEqual(mock_resolve.call_count, 1)


class TestCollectNodeAndMembers(unittest.TestCase):
    """Recursion, weight propagation, cycle protection, and max_depth --
    all mocked at the get_member_node_ids() boundary so no HTTP calls
    happen, and a genuine multi-level tree (something never exercised
    against real data as of this writing) can be tested deterministically.
    """

    def test_single_leaf_node_gets_root_weight(self):
        exporter = make_exporter()
        with patch.object(exporter, "get_member_node_ids", return_value=[]):
            result = exporter.collect_node_and_members("1332")
        self.assertEqual(result, {"1332": "root"})

    def test_one_level_of_members_gets_their_own_weight(self):
        exporter = make_exporter()

        def fake_members(parent_nid):
            if parent_nid == "1162":
                return [{"nid": "1332", "weight": "1"}, {"nid": "1333", "weight": "2"}]
            return []

        with patch.object(exporter, "get_member_node_ids", side_effect=fake_members):
            result = exporter.collect_node_and_members("1162")

        self.assertEqual(result, {"1162": "root", "1332": "1", "1333": "2"})

    def test_multi_level_recursion_propagates_weight_at_each_level(self):
        # This is the case that had never been exercised against real
        # data as of this writing -- a member that itself has members,
        # i.e. genuine multi-level recursion, not just one level deep.
        exporter = make_exporter()

        def fake_members(parent_nid):
            return {
                "1162": [{"nid": "1332", "weight": "1"}],
                "1332": [{"nid": "9001", "weight": "1"}],
            }.get(parent_nid, [])

        with patch.object(exporter, "get_member_node_ids", side_effect=fake_members):
            result = exporter.collect_node_and_members("1162")

        self.assertEqual(result, {"1162": "root", "1332": "1", "9001": "1"})

    def test_cycle_is_detected_and_does_not_infinite_loop(self):
        exporter = make_exporter()

        def fake_members(parent_nid):
            # 1332's "member" is 1162 itself -- a cycle.
            return {
                "1162": [{"nid": "1332", "weight": "1"}],
                "1332": [{"nid": "1162", "weight": "1"}],
            }.get(parent_nid, [])

        with patch.object(exporter, "get_member_node_ids", side_effect=fake_members):
            result = exporter.collect_node_and_members("1162")

        # 1162 is not re-added a second time; the cycle is broken cleanly.
        self.assertEqual(result, {"1162": "root", "1332": "1"})

    def test_max_depth_zero_excludes_all_members(self):
        exporter = make_exporter(member_media_max_depth=0)

        def fake_members(parent_nid):
            return [{"nid": "1332", "weight": "1"}] if parent_nid == "1162" else []

        with patch.object(exporter, "get_member_node_ids", side_effect=fake_members):
            result = exporter.collect_node_and_members("1162")

        self.assertEqual(result, {"1162": "root"})

    def test_max_depth_one_excludes_grandchildren_but_not_children(self):
        exporter = make_exporter(member_media_max_depth=1)

        def fake_members(parent_nid):
            return {
                "1162": [{"nid": "1332", "weight": "1"}],
                "1332": [{"nid": "9001", "weight": "1"}],
            }.get(parent_nid, [])

        with patch.object(exporter, "get_member_node_ids", side_effect=fake_members):
            result = exporter.collect_node_and_members("1162")

        self.assertEqual(result, {"1162": "root", "1332": "1"})

    def test_include_member_media_false_returns_only_the_starting_node(self):
        exporter = make_exporter(include_member_media=False)

        def fake_members(parent_nid):
            return [{"nid": "1332", "weight": "1"}] if parent_nid == "1162" else []

        with patch.object(exporter, "get_member_node_ids", side_effect=fake_members):
            result = exporter.collect_node_and_members("1162")

        self.assertEqual(result, {"1162": "root"})


class TestGetStartingNodeIds(unittest.TestCase):
    """node_id vs input_csv precedence -- confirmed manually earlier
    (node_id silently wins if both are set), codified here.

    Unlike every other test class in this file, these build their config
    via the REAL WorkbenchConfig class (writing a temp YAML file) rather
    than the minimal make_config() dict above. get_csv_data() -- called
    internally by _get_starting_node_ids() when reading input_csv -- has
    many more config key dependencies (delimiter, csv_headers, id_field,
    subdelimiter, ignore_csv_columns, and others) than MemberMediaExporter's
    own methods need, and hand-maintaining a matching list of defaults
    here would be fragile (silently wrong if WorkbenchConfig.py's own
    defaults ever change). Using the real class, with .validate mocked
    out to skip live network checks, matches the proven pattern already
    used in tests/unit_tests_workbench_config.py.
    """

    def _build_config(self, node_id=None, input_csv_path=None):
        yaml_lines = [
            "task: export_member_media",
            "host: https://example.com",
            "username: test",
            "password: test",
        ]
        if node_id is not None:
            yaml_lines.append(f'node_id: "{node_id}"')
        if input_csv_path is not None:
            yaml_lines.append(f"input_csv: {input_csv_path}")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as config_file:
            config_file.write("\n".join(yaml_lines) + "\n")
            config_file_path = config_file.name

        self.addCleanup(os.unlink, config_file_path)

        args = argparse.Namespace(
            config=config_file_path, check=False, get_csv_template=False,
            quick_delete_node=None, quick_delete_media=None, contactsheet=False,
        )

        with patch("WorkbenchConfig.logging"):
            return WorkbenchConfig(args).get_config()

    def test_node_id_takes_precedence_over_input_csv(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["node_id"])
            writer.writerow(["9999"])
            csv_path = f.name
        self.addCleanup(os.unlink, csv_path)

        config = self._build_config(node_id="1162", input_csv_path=csv_path)
        exporter = MemberMediaExporter(config, args=None)
        self.assertEqual(exporter._get_starting_node_ids(), ["1162"])

    def test_input_csv_is_read_when_node_id_absent(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["node_id"])
            writer.writerow(["1162"])
            writer.writerow(["1332"])
            csv_path = f.name
        self.addCleanup(os.unlink, csv_path)

        config = self._build_config(input_csv_path=csv_path)
        exporter = MemberMediaExporter(config, args=None)
        self.assertEqual(exporter._get_starting_node_ids(), ["1162", "1332"])

    @patch("workbench_utils.get_nid_from_url_alias")
    def test_rows_missing_node_id_are_skipped(self, mock_get_nid_from_url_alias):
        # An empty node_id value isn't purely numeric, so get_csv_data()
        # itself treats it as a possible URL alias and tries to resolve
        # it via a live HTTP call -- real, unconditional behavior of
        # shared code, not a test-only quirk. get_csv_data() is defined
        # in workbench_utils.py, so get_nid_from_url_alias must be
        # patched there, NOT on workbench_export (which only matters for
        # names looked up from code that actually lives in
        # workbench_export.py itself).
        mock_get_nid_from_url_alias.return_value = False

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["node_id"])
            writer.writerow(["1162"])
            writer.writerow([""])
            csv_path = f.name
        self.addCleanup(os.unlink, csv_path)

        config = self._build_config(input_csv_path=csv_path)
        exporter = MemberMediaExporter(config, args=None)
        self.assertEqual(exporter._get_starting_node_ids(), ["1162"])


if __name__ == "__main__":
    unittest.main()