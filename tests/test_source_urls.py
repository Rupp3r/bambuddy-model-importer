import unittest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import app as importer

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
    parse_makerworld_id,
    parse_shapekit_slug,
    parse_smithsonian_id,
    parse_thingiverse_id,
    parse_wikimedia_title,
    normalize_source_tags,
    source_url_tags,
    is_sliceable_filename,
    first_slicer_preset_refs,
    normalize_preset_ref,
    desktop_slicer_url,
    needs_model_folder,
    safe_filename,
    safe_folder_name,
    uploaded_file_ids,
)


class SourceUrlParsingTests(unittest.TestCase):
    def test_shapekit_page_resolves_public_zip(self):
        class Response:
            url="https://shapekit.cc/bird-feeder"
            text='<html><title>Bird Feeder | Shapekit</title><a href="/downloads/bird-feeder.zip">Download</a></html>'
            def raise_for_status(self): pass
        class Client:
            def __init__(self,*args,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def get(self,url): return Response()
        with patch.object(importer.httpx,"AsyncClient",Client):
            result=asyncio.run(importer.resolve_shapekit("https://shapekit.cc/bird-feeder"))
        self.assertEqual(result["title"],"Bird Feeder")
        self.assertEqual(result["files"][0]["kind"],"shapekit_zip")
        self.assertEqual(result["files"][0]["download_url"],"https://shapekit.cc/downloads/bird-feeder.zip")

    def test_makerworld_model_url(self):
        self.assertEqual(parse_makerworld_id("https://makerworld.com/en/models/123456"), "123456")
        self.assertEqual(parse_makerworld_id("https://makerworld.com/en/models/123456-bird-feeder"), "123456")
        self.assertIsNone(parse_makerworld_id("https://makerworld.com.attacker.invalid/models/123456"))

    def test_shapekit_model_url(self):
        self.assertEqual(parse_shapekit_slug("https://shapekit.cc/bird-feeder"), "bird-feeder")
        self.assertIsNone(parse_shapekit_slug("https://shapekit.cc.attacker.example/bird-feeder"))
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
    def test_source_url_tag_matches_short_source_link(self):
        self.assertEqual(source_url_tags("https://shapekit.cc/bird-feeder"), [
            "Source: https://shapekit.cc/bird-feeder",
        ])

    def test_long_source_url_falls_back_to_source_tag_with_exact_link_when_it_fits(self):
        url = "https://example.com/" + "x" * 40
        self.assertEqual(source_url_tags(url), ["Source", url])

    def test_source_tag_respects_bambuddy_tag_length_limit(self):
        self.assertEqual(source_url_tags("https://example.com/" + "x" * 80), ["Source"])

    def test_only_unsliced_stl_and_3mf_are_sliceable(self):
        self.assertTrue(is_sliceable_filename("bird feeder.stl"))
        self.assertTrue(is_sliceable_filename("bird feeder.3mf"))
        self.assertFalse(is_sliceable_filename("bird feeder.gcode"))
        self.assertFalse(is_sliceable_filename("bird feeder.gcode.3mf"))

    def test_default_slice_presets_follow_bambuddy_tier_order(self):
        self.assertEqual(first_slicer_preset_refs({
            "local": {"printer": [{"id": 1}], "process": [], "filament": []},
            "orca_cloud": {"printer": [{"id": "orca"}], "process": [], "filament": []},
            "cloud": {"printer": [], "process": [{"id": 2}], "filament": []},
            "standard": {"printer": [], "process": [], "filament": [{"id": 3}]},
        }), {
            "printer": {"source": "local", "id": "1"},
            "process": {"source": "cloud", "id": "2"},
            "filament": {"source": "standard", "id": "3"},
        })

    def test_desktop_slicer_url_uses_configured_protocol_and_encodes_filename(self):
        with patch.object(importer, "BAMBUDDY_URL", "http://bambuddy:8756/"):
            link = desktop_slicer_url(42, "short-token", "bird feeder.3mf", "orcaslicer")
        self.assertTrue(link.startswith("orcaslicer://open?file="))
        self.assertIn("%2520", link)

    def test_history_actions_stay_in_the_importer_tab_and_no_fake_slice_query_remains(self):
        self.assertNotIn("target='_blank'", importer.HTML)
        self.assertNotIn("&slice=1", importer.HTML)
        self.assertIn("fileAction.target='_self'", importer.HTML)
        self.assertNotIn("fileAction.onclick", importer.HTML)
        self.assertIn("fileAction.href=x.file_url", importer.HTML)
        self.assertIn("openSliceDialog(x.file_id", importer.HTML)
        self.assertIn('id="slicePrinter"', importer.HTML)
        self.assertIn('id="sliceProcess"', importer.HTML)

    def test_bambustudio_macos_protocol_is_used(self):
        with patch.object(importer, "BAMBUDDY_URL", "http://bambuddy:8756"):
            link = desktop_slicer_url(42, "token", "bird.3mf", "bambu_studio", "Mozilla Mac")
        self.assertTrue(link.startswith("bambustudioopen://"))

    def test_sidecar_history_action_queues_with_bambuddy_default_slicer_settings(self):
        class Response:
            def __init__(self, payload, status_code=200):
                self.payload = payload
                self.status_code = status_code
                self.text = ""
            def json(self):
                return self.payload

        calls = []
        replies = [
            Response({"use_slicer_api": True, "preferred_slicer": "orcaslicer"}),
            Response({"filename": "bird.3mf"}),
            Response({
                "local": {"printer": [{"id": 10}], "process": [{"id": 20}], "filament": [{"id": 30}]},
                "orca_cloud": {"printer": [], "process": [], "filament": []},
                "cloud": {"printer": [], "process": [], "filament": []},
                "standard": {"printer": [], "process": [], "filament": []},
            }),
            Response({"job_id": 123, "status": "queued"}, 202),
        ]
        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return replies.pop(0)

        with patch.object(importer, "bambuddy_request", side_effect=fake_request):
            action = asyncio.run(importer.prepare_slicer_action(42))

        self.assertEqual(action, {"mode": "sidecar", "slicer": "orcaslicer", "job_id": 123, "status": "queued"})
        self.assertEqual(calls[-1][1], "/library/files/42/slice")
        self.assertEqual(calls[-1][2]["json"]["printer_preset"], {"source": "local", "id": "10"})

    def test_desktop_history_action_uses_bambuddy_slicer_token(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload
                self.status_code = 200
                self.text = ""
            def json(self):
                return self.payload

        calls = []
        replies = [Response({"use_slicer_api": False, "preferred_slicer": "orcaslicer"}),
                   Response({"filename": "bird feeder.stl"}), Response({"token": "short-lived"})]
        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return replies.pop(0)

        with patch.object(importer, "BAMBUDDY_URL", "http://bambuddy:8756"), \
             patch.object(importer, "bambuddy_request", side_effect=fake_request):
            action = asyncio.run(importer.prepare_slicer_action(42, "Windows"))

        self.assertEqual(action["mode"], "desktop")
        self.assertTrue(action["slicer_url"].startswith("orcaslicer://open?file="))
        self.assertEqual(calls[-1][1], "/library/files/42/slicer-token")

    def test_slice_dialog_preset_refs_are_source_aware_and_validated(self):
        self.assertEqual(normalize_preset_ref({"source": "orca_cloud", "id": "p-42"}, "printer"),
                         {"source": "orca_cloud", "id": "p-42"})
        with self.assertRaises(importer.HTTPException):
            normalize_preset_ref({"source": "unknown", "id": "42"}, "printer")

    def test_interactive_sidecar_slice_posts_selected_presets_and_options(self):
        class Response:
            def __init__(self, payload, status_code=200):
                self.payload = payload
                self.status_code = status_code
                self.text = ""
            def json(self):
                return self.payload

        calls = []
        replies = [Response({"filename": "bird.3mf"}), Response({"job_id": 55, "status": "queued", "status_url": "/api/v1/slice-jobs/55"}, 202)]
        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return replies.pop(0)

        options = {
            "printer_preset": {"source": "local", "id": "10"},
            "process_preset": {"source": "orca_cloud", "id": "fast"},
            "filament_preset": {"source": "cloud", "id": "pla"},
            "bed_type": "High Temp Plate",
            "auto_arrange": True,
            "auto_orient": False,
        }
        with patch.object(importer, "bambuddy_request", side_effect=fake_request):
            result = asyncio.run(importer.queue_slice_with_options(42, options))

        posted = calls[-1][2]["json"]
        self.assertEqual(calls[-1][1], "/library/files/42/slice")
        self.assertEqual(posted["printer_preset"], options["printer_preset"])
        self.assertEqual(posted["process_preset"], options["process_preset"])
        self.assertEqual(posted["filament_presets"], [options["filament_preset"]])
        self.assertEqual(posted["bed_type"], "High Temp Plate")
        self.assertTrue(posted["auto_arrange"])
        self.assertFalse(posted["auto_orient"])
        self.assertEqual(result["job_id"], 55)
        self.assertEqual(result["mode"], "sidecar")

    def test_import_history_persists_file_and_source_links(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(importer, "HISTORY_DB", Path(directory) / "history.sqlite3"):
                importer.save_history({"title":"Bird Feeder","source":"Shapekit","source_url":"https://shapekit.cc/bird-feeder",
                    "imported_files":[{"id":42,"filename":"tray.stl"}]})
                rows=importer.read_history()
        self.assertEqual(rows[0]["file_id"],"42")
        self.assertEqual(rows[0]["url"],"https://shapekit.cc/bird-feeder")
        self.assertEqual(rows[0]["filename"],"tray.stl")

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

