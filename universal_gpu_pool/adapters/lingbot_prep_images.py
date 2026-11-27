#!/usr/bin/env python3
"""Prepara le foto per LingBot-Map: cartella piatta, ordinata, solo .jpg/.png con nomi sequenziali.

Il `demo.py` di LingBot cerca solo `*.jpg`, `*.png`, `*.JPG` nella cartella indicata (senza sottocartelle):
foto `.jpeg`/`.PNG`/`.webp`, cartelle annidate o ZIP con struttura diversa risultavano «0 immagini».
Qui si cerca ovunque (ricorsivo, senza maiuscole/minuscole), si ordina in modo «naturale»
(img2 prima di img10), si corregge la rotazione EXIF dei telefoni, si riducono le foto enormi.

Se SORGENTE è un VIDEO (mp4, mov, webm…) ne estrae i fotogrammi in modo robusto: i video registrati dal browser
o dal telefono hanno spesso metadati dei fotogrammi sbagliati (es. 1000 fps), quindi si conta quanti fotogrammi ci
sono davvero e si campiona in modo uniforme (circa --fps al secondo, al massimo --max-frames in tutto).

Uso:  python lingbot_prep_images.py SORGENTE DESTINAZIONE [--max-side 1920] [--fps 10] [--max-frames 300]
Esce con codice 2 e un messaggio chiaro se non trova foto o ne trova troppo poche.
"""
import argparse
import os
import re
import shutil
import sys
from collections import Counter

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".mpg", ".mpeg"}
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


def pick_stride(n_frames, src_fps, target_fps=10.0, max_frames=300):
    """Ogni quanti fotogrammi tenerne uno. n_frames è il numero REALE di fotogrammi (contati), src_fps può essere
    errato (0, NaN, 1000 nei WebM del browser): lo si usa solo se plausibile (1-240)."""
    stride = 1
    try:
        if src_fps and 1.0 <= float(src_fps) <= 240.0 and float(src_fps) == float(src_fps):
            stride = max(1, int(round(float(src_fps) / float(target_fps))))
    except (TypeError, ValueError):
        pass
    if max_frames and n_frames > 0:
        stride = max(stride, -(-int(n_frames) // int(max_frames)))        # ceil
    return stride


def extract_video(src, dst, fps, max_frames, max_side):
    try:
        import cv2
    except ImportError:
        sys.exit("Per leggere i video serve OpenCV (cv2), che qui non è installato.")
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        sys.exit("Non riesco ad aprire il video «%s»: formato non supportato? Prova con un MP4 (H.264)." % os.path.basename(src))
    src_fps = cap.get(cv2.CAP_PROP_FPS)
    n = 0                                   # 1° passaggio: si contano i fotogrammi veri (il contatore dichiarato non è affidabile)
    while cap.grab():
        n += 1
    cap.release()
    if n < MIN_PHOTOS:
        sys.exit("Il video ha solo %d fotogrammi leggibili: serve un video più lungo (almeno qualche secondo)." % n)
    stride = pick_stride(n, src_fps, fps, max_frames)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    cap = cv2.VideoCapture(src)
    i = saved = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride == 0:
            h, w = frame.shape[:2]
            if max(h, w) > max_side:
                k = max_side / float(max(h, w))
                frame = cv2.resize(frame, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
            saved += 1
            cv2.imwrite(os.path.join(dst, "%06d.jpg" % saved), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        i += 1
    cap.release()
    if saved < MIN_PHOTOS:
        sys.exit("Dal video ho ricavato solo %d immagini (servono almeno %d)." % (saved, MIN_PHOTOS))
    print("Fotogrammi pronti: %d (dal video di %d fotogrammi, uno ogni %d; fps dichiarati: %s) in %s"
          % (saved, n, stride, src_fps, dst), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--max-side", type=int, default=1920)
    ap.add_argument("--fps", type=float, default=10.0, help="fotogrammi al secondo da tenere (solo video)")
    ap.add_argument("--max-frames", type=int, default=300, help="massimo di fotogrammi estratti (solo video)")
    a = ap.parse_args(argv)
    if os.path.isfile(a.src):
        if os.path.splitext(a.src)[1].lower() in VIDEO_EXT:
            return extract_video(a.src, a.dst, a.fps, a.max_frames, a.max_side)
        sys.exit("Mi aspettavo una cartella di foto o un video, ma è un file di tipo «%s»: %s"
                 % (os.path.splitext(a.src)[1] or "sconosciuto", a.src))
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
