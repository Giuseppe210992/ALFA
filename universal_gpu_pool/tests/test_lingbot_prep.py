"""Preparazione delle foto per LingBot-Map: il caso reale «At least 1 image is required»."""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREP = ROOT / "adapters" / "lingbot_prep_images.py"
DEMO_EXTS = (".jpg", ".png", ".JPG")          # le uniche cercate da load_images() di LingBot (non ricorsivo)
try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def demo_finds(folder):
    return sorted(p for e in DEMO_EXTS for p in glob.glob(os.path.join(folder, "*" + e)))


def prep(src, dst, *extra):
    return subprocess.run([sys.executable, str(PREP), str(src), str(dst), *extra], capture_output=True, text=True)


class PrepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prep_"))
        self.src, self.dst = self.tmp / "src", self.tmp / "dst"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self, rel, data=b"x"):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def test_the_real_failure_is_reproduced_and_fixed(self):
        for rel in ("album/IMG_2.jpeg", "album/IMG_10.jpeg", "album/sub/Foto.PNG", "IMG_1.JPEG"):
            self.touch(rel, b"finto")
        for junk in (".nascosto.jpg", "__MACOSX/album/._IMG_2.jpeg", "note.txt", "film.mov"):
            self.touch(junk)
        self.assertEqual(demo_finds(str(self.src)), [], "così com'è, il demo di LingBot vede 0 foto")
        r = prep(self.src, self.dst)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Foto pronte: 4", r.stdout)
        found = demo_finds(str(self.dst))
        self.assertEqual(len(found), 4)
        self.assertEqual([os.path.splitext(os.path.basename(p))[0] for p in found], ["000001", "000002", "000003", "000004"])
        self.assertTrue(all(p.endswith((".jpg", ".png")) for p in found), "solo estensioni che il demo di LingBot riconosce")

    def test_natural_order_and_recursion(self):
        for name in ("p10", "p2", "p1", "p3"):
            self.touch(f"{name}.jpg", name.encode())
        r = prep(self.src, self.dst)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = [(self.dst / f).read_bytes() for f in sorted(os.listdir(self.dst))]
        if not HAVE_PIL:                       # senza Pillow le foto si copiano com'erano: l'ordine si legge dal contenuto
            self.assertEqual(got, [b"p1", b"p2", b"p3", b"p10"])

    def test_clear_error_when_no_photos(self):
        self.touch("a/dati.csv"); self.touch("a/b/dati2.csv"); self.touch("leggimi.txt")
        r = prep(self.src, self.dst)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Servono almeno 3 foto", r.stderr)
        self.assertIn(".csv", r.stderr); self.assertIn("2×.csv", r.stderr)

    def test_too_few_photos(self):
        self.touch("a.jpg"); self.touch("b.png")
        r = prep(self.src, self.dst)
        self.assertNotEqual(r.returncode, 0); self.assertIn("ne ho trovate 2", r.stderr)

    def test_file_instead_of_folder(self):
        self.touch("solo.zip")
        r = prep(self.src / "solo.zip", self.dst)
        self.assertNotEqual(r.returncode, 0); self.assertIn("è un file", r.stderr)

    @unittest.skipUnless(HAVE_PIL, "serve Pillow")
    def test_exif_rotation_resize_and_formats(self):
        from PIL import Image
        for i in range(2):
            Image.new("RGB", (40, 20), (i * 80, 10, 10)).save(self.src / f"w{i}.jpg") if (self.src.mkdir(exist_ok=True) or True) else None
        im = Image.new("RGB", (40, 20), (200, 0, 0))
        exif = Image.Exif(); exif[0x0112] = 6                            # «ruota di 90°»: come una foto verticale da telefono
        im.save(self.src / "tel.jpeg", exif=exif)
        Image.new("RGB", (4000, 3000), (0, 200, 0)).save(self.src / "enorme.JPG")
        Image.new("RGB", (30, 30), (0, 0, 200)).save(self.src / "web.webp")
        r = prep(self.src, self.dst, "--max-side", "800")
        self.assertEqual(r.returncode, 0, r.stderr)
        sizes = {}
        for f in sorted(os.listdir(self.dst)):
            with Image.open(self.dst / f) as x:
                sizes[f] = x.size
        self.assertEqual(len(sizes), 5)
        self.assertIn((20, 40), sizes.values(), "la rotazione EXIF va applicata (40x20 → 20x40)")
        self.assertTrue(all(max(s) <= 800 for s in sizes.values()), sizes)
        self.assertTrue(all(f.endswith(".jpg") for f in sizes), "il webp diventa jpg")
        self.assertEqual(len(demo_finds(str(self.dst))), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
