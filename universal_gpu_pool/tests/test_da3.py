"""Depth Anything 3: ricetta, scelta del motore e programma di esecuzione (con un DA3 finto, senza GPU)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

from core.recipes import DA3_FRAMES, DA3_REPO, da3_cfg, lingbot_cfg  # noqa: E402


class Da3RecipeTest(unittest.TestCase):
    def test_recipe_shape(self):
        cfg = da3_cfg({"video": "u"}, {})
        run = cfg["run"]
        self.assertIn("_gpool_prep_images.py {video} /tmp/gpool_frames --fps 5", run)
        self.assertIn("_gpool_da3_runner.py --repo . --images /tmp/gpool_frames --out {OUT} --model large --max-frames 80", run)
        self.assertEqual(cfg["input_key"], "video")
        self.assertIn("_gpool_da3_runner.py", cfg["files"]); self.assertIn("_gpool_prep_images.py", cfg["files"])
        self.assertNotIn("-e .", " ".join(cfg["setup"]), "il pacchetto non si installa: richiede la cronologia git")
        self.assertIn("einops", cfg["setup"][0])
        img = da3_cfg({"images": "u"}, {"da3_model": "small", "max_frames": 33})
        self.assertIn("--model small --max-frames 33", img["run"]); self.assertEqual(img["input_key"], "images")
        self.assertNotIn("--fps", img["run"])
        with self.assertRaises(ValueError):
            da3_cfg({"video": "u"}, {"da3_model": "enorme"})
        self.assertEqual(sorted(DA3_FRAMES), ["base", "large", "small"])

    def test_adapter_picks_engine(self):
        from adapters.lingbot_map_adapter import LingBotMapAdapter
        ad = LingBotMapAdapter()
        items = [{"video": "https://example.com/a.mp4"}]
        da3 = ad.plan(items, {"runtime": "kaggle", "options": {"engine": "da3"}}, {})[0]["payload"]["cfg"]
        self.assertIn("Depth-Anything-3", da3["repo_url"]); self.assertIn("_gpool_da3_runner.py", da3["run"])
        lb = ad.plan(items, {"runtime": "kaggle", "options": {}}, {})[0]["payload"]["cfg"]
        self.assertIn("lingbot-map", lb["repo_url"].lower()); self.assertIn("_gpool_lingbot_runner.py", lb["run"])
        with self.assertRaises(ValueError):
            ad.plan(items, {"runtime": "kaggle", "options": {"engine": "boh"}}, {})
        self.assertEqual(DA3_REPO, "https://github.com/ByteDance-Seed/Depth-Anything-3")


@unittest.skipUnless(HAVE_NUMPY, "numpy non installato")
class Da3RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="da3_"))
        self.frames = self.tmp / "frames"; self.frames.mkdir()
        for i in range(40):
            (self.frames / ("%06d.jpg" % (i + 1))).write_bytes(b"x")
        self.out = self.tmp / "out"; self.calls = self.tmp / "calls.jsonl"

    def run_runner(self, *extra, **env):
        e = dict(os.environ, PYTHONPATH=str(ROOT / "tests" / "stub_da3_torch"), GPOOL_STUB_CALLS=str(self.calls),
                 **{k: str(v) for k, v in env.items()})
        return subprocess.run([sys.executable, str(ROOT / "adapters" / "da3_runner.py"), "--repo", str(ROOT / "tests" / "stub_da3"),
                               "--images", str(self.frames), "--out", str(self.out), *extra],
                              capture_output=True, text=True, env=e, timeout=120)

    def calls_list(self):
        return [json.loads(l) for l in self.calls.read_text().splitlines()]

    def test_saves_everything(self):
        r = self.run_runner("--model", "base", "--max-frames", "12")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("scene.glb", "camera_poses.json", "predictions.npz", "summary.json"):
            self.assertTrue((self.out / f).is_file(), f)
        summ = json.loads((self.out / "summary.json").read_text())
        self.assertEqual((summ["frames_used"], summ["frames_available"], summ["attempts"], summ["glb"]), (12, 40, 1, True))
        poses = json.loads((self.out / "camera_poses.json").read_text())["frames"]
        self.assertEqual(len(poses), 12)
        self.assertEqual(self.calls_list()[0]["model"], "depth-anything/DA3-BASE")
        self.assertEqual(self.calls_list()[0]["ref"], "middle")        # video: si parte dal fotogramma centrale
        self.assertIn("librerie facoltative assenti", r.stdout)       # open3d/moviepy/evo mancanti non fermano l'import

    def test_halves_frames_on_out_of_memory(self):
        r = self.run_runner("--model", "large", "--max-frames", "40", GPOOL_STUB_MAX=15)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sizes = [c["n"] for c in self.calls_list()]
        self.assertEqual(sizes, [40, 20, 10][:len(sizes)])
        self.assertEqual(sizes[-1], 10)
        self.assertIn("riprovo con la metà", r.stdout)
        self.assertEqual(json.loads((self.out / "summary.json").read_text())["attempts"], 3)
        self.assertEqual(self.calls_list()[0]["model"], "depth-anything/DA3-LARGE-1.1")

    def test_other_errors_are_not_swallowed(self):
        (self.frames / "000001.jpg").unlink()
        for f in list(self.frames.iterdir())[1:]:
            f.unlink()                                     # una sola immagine
        r = self.run_runner()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("almeno 2 immagini", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
