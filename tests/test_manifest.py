"""The farm listing: the manifest, and the pictures it promises tiinyapp.farm.

Nothing in the app reads this file or these images, so no other test notices
when the listing drifts away from the release it is supposed to describe. That
drift is not hypothetical: the 0.1.0 screenshots were still on the listing three
versions later, advertising a chat page that no longer existed and a tagline the
footer had retired.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pocket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "manifests", "ainode-pocket.json")
SHOT_NOTES = os.path.join(ROOT, "docs", "screenshots", "README.md")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def manifest():
    with open(MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)


def readme():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
        return handle.read()


class TestManifest(unittest.TestCase):
    def test_the_listing_advertises_the_version_that_ships(self):
        self.assertEqual(manifest()["version"], pocket.__version__)

    def test_the_declared_port_is_the_one_the_server_defaults_to(self):
        from pocket import server as server_mod
        self.assertIn(server_mod.PORT, manifest()["requires"]["ports"])

    def test_every_advertised_screenshot_is_a_png_in_the_repository(self):
        shots = manifest()["screenshots"]
        self.assertTrue(shots, "the listing shows nothing with an empty list")
        for path in shots:
            full = os.path.join(ROOT, path)
            self.assertTrue(os.path.isfile(full), path + " is advertised and missing")
            with open(full, "rb") as handle:
                self.assertEqual(handle.read(8), PNG_MAGIC, path + " is not a PNG")

    def test_every_picture_the_readme_embeds_is_in_the_repository(self):
        for path in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme()):
            if path.startswith("http"):
                continue
            self.assertTrue(os.path.isfile(os.path.join(ROOT, path)),
                            path + " is embedded in the README and missing")


class TestScreenshots(unittest.TestCase):
    """The pictures cannot be read for what they show, so this is the next best
    thing: the shoot records which version it was taken at, and a version bump
    has to either re-shoot them or say out loud that the old ones still stand."""

    def test_the_shoot_is_recorded_as_this_version(self):
        with open(SHOT_NOTES, encoding="utf-8") as handle:
            found = re.search(r"^Taken at: (.+)$", handle.read(), re.M)
        self.assertIsNotNone(found, "docs/screenshots/README.md lost its version line")
        self.assertEqual(found.group(1).strip(), pocket.__version__,
                         "the screenshots were taken at an older version: re-shoot "
                         "them, or change the line on purpose")

    def test_the_shoot_covers_everything_the_listing_shows(self):
        with open(SHOT_NOTES, encoding="utf-8") as handle:
            notes = handle.read()
        for path in manifest()["screenshots"]:
            self.assertIn(os.path.basename(path), notes,
                          path + " is advertised and the shoot notes never mention it")


if __name__ == "__main__":
    unittest.main()
