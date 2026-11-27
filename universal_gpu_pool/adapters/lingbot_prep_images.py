#!/usr/bin/env python3
"""Prepara le foto per LingBot-Map: cartella piatta, ordinata, solo .jpg/.png con nomi sequenziali.

Il `demo.py` di LingBot cerca solo `*.jpg`, `*.png`, `*.JPG` nella cartella indicata (senza sottocartelle):
foto `.jpeg`/`.PNG`/`.webp`, cartelle annidate o ZIP con struttura diversa risultavano «0 immagini».
Qui si cerca ovunque (ricorsivo, senza maiuscole/minuscole), si ordina in modo «naturale»
(img2 prima di img10), si corregge la rotazione EXIF dei telefoni, si riducono le foto enormi.

Uso:  python lingbot_prep_images.py SORGENTE DESTINAZIONE [--max-side 1920]
Esce con codice 2 e un messaggio chiaro se non trova foto o ne trova troppo poche.
"""
import argparse
import os
import re
import shutil
import sys
from collections import Counter

IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif"}
MIN_PHOTOS = 3


def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p)]


def find_images(src):
    found, others = [], Counter()
    for dp, dn, fn in os.walk(src):
        dn[:] = sorted(d for d in dn if not d.startswith((".", "__MACOSX")))
        for f in fn:
            if f.startswith("."):
                continue
            ext = os.path.splitext(f)[1].lower()
            (found.append(os.path.join(dp, f)) if ext in IMAGE_EXT else others.update([ext or "(senza estensione)"]))
    found.sort(key=lambda p: natural_key(os.path.relpath(p, src)))
    return found, others


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--max-side", type=int, default=1920)
    a = ap.parse_args(argv)
    if os.path.isfile(a.src):
        sys.exit("Mi aspettavo una cartella di foto, ma è un file: " + a.src)
    paths, others = find_images(a.src)
    if len(paths) < MIN_PHOTOS:
        top = sorted(os.listdir(a.src))[:15] if os.path.isdir(a.src) else []
        extra = ", ".join(f"{n}×{e}" for e, n in others.most_common(6)) or "nessun altro file"
        sys.exit("Servono almeno %d foto (jpg, jpeg, png, webp…) ma ne ho trovate %d. Altri file presenti: %s. "
                 "Contenuto della cartella: %s" % (MIN_PHOTOS, len(paths), extra, top))
    try:
        from PIL import Image, ImageOps
        try:
            import pillow_heif                      # facoltativo: foto HEIC di iPhone
            pillow_heif.register_heif_opener()
        except Exception:
            pass
    except ImportError:
        Image = ImageOps = None
    shutil.rmtree(a.dst, ignore_errors=True)
    os.makedirs(a.dst)
    n, skipped = 0, []
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        dest_base = os.path.join(a.dst, "%06d" % (n + 1))
        ok = False
        if Image is not None:
            try:
                with Image.open(p) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    if max(im.size) > a.max_side:
                        im.thumbnail((a.max_side, a.max_side))
                    im.save(dest_base + ".jpg", quality=95)
                ok = True
            except Exception as e:
                if ext in (".jpg", ".jpeg", ".jpe", ".png"):       # non leggibile con PIL: lo si copia com'è
                    shutil.copyfile(p, dest_base + (".png" if ext == ".png" else ".jpg"))
                    ok = True
                else:
                    skipped.append("%s (%s)" % (os.path.basename(p), str(e)[:60]))
        elif ext in (".jpg", ".jpeg", ".jpe", ".png"):
            shutil.copyfile(p, dest_base + (".png" if ext == ".png" else ".jpg"))
            ok = True
        else:
            skipped.append("%s (serve Pillow per convertirlo)" % os.path.basename(p))
        n += 1 if ok else 0
    if skipped:
        print("Foto saltate (%d): %s" % (len(skipped), "; ".join(skipped[:8])), file=sys.stderr)
    if n < MIN_PHOTOS:
        sys.exit("Dopo il controllo restano solo %d foto utilizzabili (servono almeno %d)." % (n, MIN_PHOTOS))
    print("Foto pronte: %d (da %d file trovati) in %s" % (n, len(paths), a.dst), flush=True)


if __name__ == "__main__":
    main()
