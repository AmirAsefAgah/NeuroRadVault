"""Vault bookkeeping survives damaged JSON files (power cut, unplugged drive)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from vault.config import Vault, read_json, write_json


class JsonFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_backup_is_used_when_the_file_is_damaged(self):
        p = self.tmp / "state.json"
        write_json(p, {"a": 1}, backup=True)
        write_json(p, {"a": 2}, backup=True)              # .bak now holds {"a": 1}
        p.write_bytes(b"\x00" * 40)                        # what NTFS/exFAT can leave after a power cut
        self.assertEqual(read_json(p, {}), {"a": 1})
        self.assertFalse(p.exists())                       # damaged file moved aside, not overwritten later
        self.assertEqual(len(list(self.tmp.glob("state.json.corrupt-*"))), 1)

    def test_empty_file_without_backup_gives_default(self):
        p = self.tmp / "queue.json"
        p.write_text("", encoding="utf-8")
        self.assertEqual(read_json(p, []), [])
        self.assertEqual(read_json(self.tmp / "missing.json", "d"), "d")

    def test_lost_settings_keep_the_later_folders(self):
        for name in ("01_Cerebrovascular", "02_LATER_Neoplasm", "05_LATER_Spine"):
            (self.tmp / name).mkdir()
        v = Vault(self.tmp)
        v.create()
        v.settings_path.write_text("", encoding="utf-8")   # truncated settings.json, no .bak
        v2 = Vault(self.tmp)
        self.assertEqual(sorted(v2.settings["later_domains"]), ["Neoplasm", "Spinal"])
        self.assertEqual(v2.folders["Cerebrovascular"], "01_Cerebrovascular")
        self.assertTrue(v2.folders["Neoplasm"].endswith("_LATER_Neoplasm"))


if __name__ == "__main__":
    unittest.main()
