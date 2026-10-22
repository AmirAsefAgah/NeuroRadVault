"""Catalogue building from the bundled data (offline)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from vault.config import BUNDLED_DATA, Vault
from vault.enrich import ADDITIONS_HEADER
from vault.registry import build_catalog, detect, read_folder, short_name


class RegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = build_catalog(tempfile.mkdtemp(), offline=True)

    def test_every_dataset_is_there_once(self):
        ids = [d["id"] for d in self.cat["datasets"]]
        self.assertGreater(len(ids), 300)
        self.assertEqual(len(ids), len(set(ids)))

    def test_folders_are_unique_per_domain_and_priority(self):
        seen = set()
        for d in self.cat["datasets"]:
            key = (d["category"], d["priority"], d["folder"].lower())
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_priorities_are_consistent(self):
        for d in self.cat["datasets"]:
            if d["auto"]:
                self.assertIn(d["priority"], (1, 2))
                self.assertEqual(d["priority"] == 1, d["masks"])
            else:
                self.assertIn(d["priority"], (3, 4))
        self.assertGreater(sum(d["auto"] for d in self.cat["datasets"]), 50)

    def test_vault_paths_are_relative(self):
        v = Vault(tempfile.mkdtemp())
        d = next(x for x in self.cat["datasets"] if x["auto"])
        rel = v.dataset_rel(d)
        self.assertFalse(rel.startswith("/") or ":" in rel)
        self.assertEqual(v.relpath(v.abspath(rel)), rel)

    def test_later_domains_move_to_the_end(self):
        v = Vault(tempfile.mkdtemp())
        v.settings["later_domains"] = ["Psychiatric"]
        v.apply_domain_order()
        self.assertEqual(v.folders["Psychiatric"], "07_LATER_Psychiatric")
        self.assertEqual(v.folders["Neoplasm"], "01_Neoplasm")

    def test_detect_and_names(self):
        self.assertEqual(detect(["https://openneuro.org/datasets/ds003949"]), {"type": "openneuro", "arg": "ds003949"})
        self.assertEqual(detect(["https://zenodo.org/records/7960856"]), {"type": "zenodo", "arg": "7960856"})
        self.assertEqual(detect(["https://doi.org/10.6084/m9.figshare.1512427"])["type"], "figshare")
        self.assertIsNone(detect(["https://www.kaggle.com/datasets/x/y"]))
        self.assertEqual(short_name("Ischemic Stroke Lesion Segmentation (ISLES): 2022"), "ISLES 2022")
        self.assertEqual(short_name("UPENN-GBM (Multi-parametric MRI (mpMRI) scans)"), "UPENN-GBM")


class AdditionsTest(unittest.TestCase):
    def test_our_own_additions_join_the_catalogue(self):
        d = Path(tempfile.mkdtemp())
        for f in (BUNDLED_DATA / "registry").glob("*.csv"):
            shutil.copy(f, d / f.name)
        base = len(read_folder(d))
        (d / "additions.csv").write_text(ADDITIONS_HEADER + "Neoplasm,Our Test Glioma Set,,https://zenodo.org/records/123,"
                                         "2026,Open,,,MRI,,10,,,,NIFTI,Yes,,Glioma,,,,,\n"
                                         "Unknown,Bad category row,,,,,,,,,,,,,,,,,,,,,\n", encoding="utf-8")
        rows = read_folder(d)
        self.assertEqual(len(rows), base + 1)            # the row with an unknown category is ignored
        self.assertEqual(rows[-1]["category"], "Neoplasm")
        self.assertEqual(detect([rows[-1]["url"]]), {"type": "zenodo", "arg": "123"})


if __name__ == "__main__":
    unittest.main()
