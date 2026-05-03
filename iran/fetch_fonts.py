"""One-time helper: download the Vazirmatn variable font at build time.

Usage:
    python fetch_fonts.py

The script downloads the woff2 variable-font file from the official GitHub
release of Vazirmatn, writes it to iran/static/fonts/Vazirmatn.woff2, and
generates a matching @font-face CSS file at iran/static/fonts/vazirmatn.css.

It uses only the Python standard library (urllib, zipfile, pathlib) — no
extra pip packages are required.
"""

from __future__ import annotations

import io
import pathlib
import urllib.request
import zipfile

RELEASE_URL = (
    "https://github.com/rastikerdar/vazirmatn/releases/download/"
    "v33.003/vazirmatn-v33.003-fonts.zip"
)
FONT_PATH_IN_ZIP = "Fonts/Variable/Webfonts/woff2/Vazirmatn.woff2"

_HERE = pathlib.Path(__file__).parent
FONTS_DIR = _HERE / "static" / "fonts"
WOFF2_DEST = FONTS_DIR / "Vazirmatn.woff2"
CSS_DEST = FONTS_DIR / "vazirmatn.css"

CSS_CONTENT = """\
/* Vazirmatn — self-hosted, no CDN */
@font-face {
  font-family: 'Vazirmatn';
  font-style: normal;
  font-weight: 100 900;
  font-display: swap;
  src: url('/static/fonts/Vazirmatn.woff2') format('woff2');
  unicode-range: U+0600-06FF, U+200C-200E, U+2010-2011, U+204F, U+2212,
                 U+0020-007E, U+00A0-00FF;
}
"""


def main() -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    if WOFF2_DEST.exists():
        print(f"[fetch_fonts] Font already exists at {WOFF2_DEST} — skipping download.")
    else:
        print(f"[fetch_fonts] Downloading Vazirmatn font from {RELEASE_URL} …")
        with urllib.request.urlopen(RELEASE_URL) as response:  # noqa: S310
            zip_bytes = response.read()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            with zf.open(FONT_PATH_IN_ZIP) as font_file:
                WOFF2_DEST.write_bytes(font_file.read())

        print(f"[fetch_fonts] Saved font to {WOFF2_DEST}")

    CSS_DEST.write_text(CSS_CONTENT, encoding="utf-8")
    print(f"[fetch_fonts] Wrote CSS to {CSS_DEST}")


if __name__ == "__main__":
    main()
