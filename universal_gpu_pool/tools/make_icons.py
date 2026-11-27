#!/usr/bin/env python3
"""Genera le icone dell'app (webapp/icon-*.png) senza font: un riquadro sfumato con una «G» disegnata a mano.
Uso: python tools/make_icons.py   (serve Pillow, solo per rigenerare le icone: il programma non lo richiede)"""
import math
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "webapp"


def gradient(size):
    img = Image.new("RGB", (size, size))
    px = img.load()
    c1, c2 = (91, 140, 255), (124, 92, 255)           # stessi colori del logo nella webapp
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2.0 * size)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))
    return img


def draw_g(img, scale=1.0):
    S = img.size[0] * 4                                 # disegno a risoluzione 4x e poi si riduce (bordi lisci)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c, r, w = S / 2, S * 0.30 * scale, S * 0.115 * scale
    box = (c - r, c - r, c + r, c + r)
    d.arc(box, start=38, end=322, fill="white", width=int(w))               # anello aperto a destra
    d.rectangle((c, c - w * 0.5, c + r + w * 0.5, c + w * 0.5), fill="white")  # barra della G
    d.rectangle((c + r - w * 0.5, c - w * 0.5, c + r + w * 0.5, c + r * 0.55), fill="white")
    layer = layer.resize(img.size, Image.LANCZOS)
    img.paste(layer, (0, 0), layer)
    return img


def rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main():
    for size in (192, 512):
        rounded(draw_g(gradient(size)), int(size * 0.22)).save(OUT / f"icon-{size}.png", optimize=True)
    # «maskable»: tutta la superficie colorata e simbolo nella zona sicura centrale (Android lo ritaglia come vuole)
    draw_g(gradient(512), scale=0.78).convert("RGBA").save(OUT / "icon-maskable-512.png", optimize=True)
    print("icone create in", OUT)


if __name__ == "__main__":
    main()
