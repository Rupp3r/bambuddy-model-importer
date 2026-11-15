import unittest

from app import (
    SESSION_TOKEN_TTL_SECONDS,
    create_session_token,
    verify_session_token,
    _archive_license,
    _smithsonian_stl_options,
    parse_archive_id,
    parse_nasa_github_folder,
    parse_nasa_slug,
    parse_nih_id,
    parse_printables_id,
    parse_smithsonian_id,
    parse_thingiverse_id,
    parse_wikimedia_title,
    normalize_source_tags,
    needs_model_folder,
    safe_filename,
    safe_folder_name,
    uploaded_file_ids,
)


class SourceUrlParsingTests(unittest.TestCase):
    def test_printables_model_url(self):
        self.assertEqual(
            parse_printables_id("https://www.printables.com/model/123456-example"),
            "123456",
        )

    def test_nasa_official_github_folder_url(self):
        self.assertEqual(
            parse_nasa_github_folder(
                "https://github.com/nasa/NASA-3D-Resources/tree/master/3D%20Printing/Apollo%20Lunar%20Module"
            ),
            "3D Printing/Apollo Lunar Module",
        )

    def test_nasa_github_blob_file_resolves_to_its_folder(self):
        self.assertEqual(
            parse_nasa_github_folder(
                "https://github.com/nasa/NASA-3D-Resources/blob/master/3D%20Printing/Apollo%20Lunar%20Module/model.stl"
            ),
            "3D Printing/Apollo Lunar Module",
        )

    def test_nasa_science_model_url(self):
        self.assertEqual(
            parse_nasa_slug("https://science.nasa.gov/3d-resources/advanced-composition-explorer/"),
            "advanced-composition-explorer",
        )

    def test_legacy_nasa_model_url(self):
        self.assertEqual(
            parse_nasa_slug("https://nasa3d.arc.nasa.gov/detail/apollo-11"),
            "apollo-11",
        )

    def test_nih_3d_entry_url(self):
        self.assertEqual(
            parse_nih_id("https://3d.nih.gov/entries/3DPX-000914"),
            "3DPX-000914",
        )

    def test_smithsonian_3d_object_url(self):
        model_id = "d8c6457e-4ebc-11ea-b77f-2e728ce88125"
        self.assertEqual(
            parse_smithsonian_id(f"https://3d.si.edu/object/3d/command-module:{model_id}"),
            model_id,
        )

    def test_smithsonian_only_offers_printable_stl_downloads(self):
        model_id = "d8c6457e-4ebc-11ea-b77f-2e728ce88125"
        rows = [
            {
                "title": "Apollo object",
                "content": {
                    "usage": "Download3D",
                    "model_type": "stl",
                    "uri": f"https://3d-api.si.edu/content/document/3d_package:{model_id}/model.zip",
                    "model_url": f"3d_package:{model_id}",
                },
            },
            {
                "title": "Preview only",
                "content": {
                    "usage": "Image2D",
                    "model_type": "jpg",
                    "uri": "https://3d-api.si.edu/content/document/preview.jpg",
                    "model_url": f"3d_package:{model_id}",
                },
            },
            {
                "title": "Wrong host",
                "content": {
                    "usage": "Download3D",
                    "model_type": "stl",
                    "uri": "https://files.example.invalid/model.zip",
                    "model_url": f"3d_package:{model_id}",
                },
            },
        ]
        files = _smithsonian_stl_options(rows)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["kind"], "smithsonian_stl")
        self.assertEqual(files[0]["name"], "model.zip")

    def test_wikimedia_file_page_url(self):
        self.assertEqual(
            parse_wikimedia_title("https://commons.wikimedia.org/wiki/File:Test_model.stl"),
            "Test_model.stl",
        )

    def test_wikimedia_index_php_url(self):
        self.assertEqual(
            parse_wikimedia_title("https://commons.wikimedia.org/w/index.php?title=File%3ATest_model.stl"),
            "Test_model.stl",
        )

    def test_archive_thingiverse_snapshot_url(self):
        self.assertEqual(
            parse_archive_id("https://archive.org/details/thingiverse-123456"),
            "thingiverse-123456",
        )

    def test_archive_download_file_url(self):
        self.assertEqual(
            parse_archive_id("https://archive.org/download/thingiverse-123456/model.zip"),
            "thingiverse-123456",
        )

    def test_archive_license_display(self):
        self.assertEqual(
            _archive_license("https://creativecommons.org/licenses/by-nc-sa/4.0/"),
            "CC BY-NC-SA 4.0",
        )

    def test_thingiverse_thing_url(self):
        self.assertEqual(
            parse_thingiverse_id("https://www.thingiverse.com/thing:123456"),
            "123456",
        )

    def test_lookalike_hosts_are_not_accepted(self):
        self.assertIsNone(parse_nasa_slug("https://science.nasa.gov.attacker.example/3d-resources/model"))
        self.assertIsNone(parse_nasa_github_folder("https://github.com.attacker.example/nasa/NASA-3D-Resources/tree/master/3D%20Printing/model"))
        self.assertIsNone(parse_nih_id("https://3d.nih.gov.attacker.example/entries/123"))
        self.assertIsNone(parse_wikimedia_title("https://commons.wikimedia.org.attacker.example/wiki/File:Model.stl"))
        self.assertIsNone(parse_archive_id("https://archive.org.attacker.example/details/thingiverse-123456"))

    def test_filename_is_reduced_to_safe_basename(self):
        self.assertEqual(safe_filename("../../part.STL"), "part.STL")

    def test_unsupported_file_extension_is_rejected(self):
        with self.assertRaises(Exception):
            safe_filename("model.gltf")




class ImportOrganizationTests(unittest.TestCase):
    def test_source_tags_are_normalized_and_deduplicated(self):
        self.assertEqual(
            normalize_source_tags(
                [{"name": " Miniature "}, {"name": "miniature"}],
                ["mechanical", "display model"],
                "should not become one concatenated tag",
                [123, None],
            ),
            ["Miniature", "mechanical", "display model", "should not become one concatenated tag"],
        )

    def test_empty_and_numeric_source_tags_are_ignored(self):
        self.assertEqual(normalize_source_tags(["", "   ", "12345", None]), [])

    def test_folder_name_is_safe_and_readable(self):
        self.assertEqual(
            safe_folder_name(r"Model: A/B? <test>"),
            "Model A B test",
        )

    def test_multiple_files_from_one_model_get_a_folder(self):
        selected = [
            {"id": "1", "kind": "stl", "name": "body.stl"},
            {"id": "2", "kind": "stl", "name": "lid.stl"},
        ]
        self.assertTrue(needs_model_folder("https://www.printables.com/model/123-example", selected))

    def test_single_normal_file_stays_in_library_root(self):
        selected = [{"id": "1", "kind": "stl", "name": "body.stl"}]
        self.assertFalse(needs_model_folder("https://www.printables.com/model/123-example", selected))

    def test_single_smithsonian_zip_gets_a_folder_for_extracted_meshes(self):
        selected = [{"id": "https://3d-api.si.edu/model.zip", "kind": "smithsonian_stl", "name": "model.zip"}]
        self.assertTrue(needs_model_folder(
            "https://3d.si.edu/object/3d/model:d8c6457e-4ebc-11ea-b77f-2e728ce88125",
            selected,
        ))

    def test_single_internet_archive_zip_gets_a_folder(self):
        selected = [{"id": "thing.zip", "kind": "archive", "name": "thing.zip"}]
        self.assertTrue(needs_model_folder("https://archive.org/details/thingiverse-123", selected))

    def test_uploaded_file_ids_include_files_expanded_from_archives(self):
        self.assertEqual(
            uploaded_file_ids([
                {"id": 10, "filename": "body.stl"},
                {"imported_files": [{"id": 11}, {"id": 12}]},
            ]),
            [10, 11, 12],
        )




class RememberedSessionTests(unittest.TestCase):
    def test_session_token_is_valid_for_same_password(self):
        token = create_session_token(secret="test-password", now=1_000_000)
        self.assertTrue(verify_session_token(token, secret="test-password", now=1_000_100))

    def test_session_token_is_invalid_after_password_changes(self):
        token = create_session_token(secret="old-password", now=1_000_000)
        self.assertFalse(verify_session_token(token, secret="new-password", now=1_000_100))

    def test_session_token_expires_after_one_year(self):
        token = create_session_token(secret="test-password", now=1_000_000)
        self.assertFalse(
            verify_session_token(
                token,
                secret="test-password",
                now=1_000_000 + SESSION_TOKEN_TTL_SECONDS + 1,
            )
        )

    def test_session_token_with_future_issue_time_is_rejected(self):
        token = create_session_token(secret="test-password", now=1_000_000)
        self.assertFalse(verify_session_token(token, secret="test-password", now=999_000))

    def test_malformed_session_token_is_rejected(self):
        self.assertFalse(verify_session_token("not-a-valid-session-token", secret="test-password", now=1_000_000))


if __name__ == "__main__":
    unittest.main()
