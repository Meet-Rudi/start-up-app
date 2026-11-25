#!/usr/bin/env python3
"""
Build the self-contained adoption request page for the company website.

    python web/adoption-request/build.py --api https://<id>.lambda-url.eu-central-1.on.aws/

Reads adoption-request.src.html and writes rudi-adoption-request.html next to it: one file with
the brand stylesheet, the fonts, the logo, the mascot and the favicon inlined, the endpoint
filled in, and a Content-Security-Policy that lets the page talk to that endpoint only. Nothing
in it points back at this repository, so it can be put at any URL as it is.

`python deploy.py prospect-intake` runs this after each deploy with the deployed URL.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
BRAND = os.path.abspath(os.path.join(HERE, "..", "brand"))
SOURCE = os.path.join(HERE, "adoption-request.src.html")
OUTPUT = os.path.join(HERE, "rudi-adoption-request.html")
MIME = {".png": "image/png", ".woff2": "font/woff2", ".svg": "image/svg+xml"}


def data_uri(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as fh:
        return "data:{};base64,{}".format(
            MIME[ext], base64.b64encode(fh.read()).decode("ascii")
        )


def inline_css() -> str:
    with open(os.path.join(BRAND, "rudi-brand.css"), encoding="utf-8") as fh:
        css = fh.read()

    def font(m: re.Match) -> str:
        return f'url("{data_uri(os.path.join(BRAND, m.group(1)))}")'

    css = re.sub(r'url\("(fonts/[^"]+)"\)', font, css)
    left = re.findall(r'url\((?!"?data:)[^)]*\)', css)
    if left:
        sys.exit("!! the stylesheet still loads {}".format(", ".join(left)))
    return css


def build(api: str) -> str:
    parts = urlsplit(api)
    if parts.scheme != "https" or not parts.netloc:
        sys.exit(f"!! --api must be an https URL, got {api!r}")
    origin = "https://" + parts.netloc

    with open(SOURCE, encoding="utf-8") as fh:
        html = fh.read()

    swaps = [
        (
            '<link rel="stylesheet" href="../brand/rudi-brand.css">',
            f"<style>\n{inline_css()}</style>",
        ),
        (
            'href="../brand/assets/favicon.png"',
            'href="{}"'.format(data_uri(os.path.join(BRAND, "assets", "favicon.png"))),
        ),
        (
            '<meta name="rudi-intake-url" content="">',
            (
                f'<meta name="rudi-intake-url" content="{api}">\n'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
                f"connect-src {origin}; base-uri 'none'; form-action 'none'\">"
            ),
        ),
    ]
    for old, new in swaps:
        if html.count(old) != 1:
            sys.exit(f"!! expected exactly one {old!r} in the source page")
        html = html.replace(old, new)

    for name in ("rudi-logo.png", "rudi-mascot.png"):
        old = f'src="../brand/assets/{name}"'
        if old not in html:
            sys.exit(f"!! the source page no longer uses {name}")
        html = html.replace(
            old, 'src="{}"'.format(data_uri(os.path.join(BRAND, "assets", name)))
        )

    # Nothing may point at a file beside the page: only page anchors, data and https links.
    left = re.findall(r'(?:src|href)="(?!https://|data:|#)[^"]*"', html)
    if left:
        sys.exit("!! the page still points at {}".format(", ".join(sorted(set(left)))))
    return html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--api", required=True, help="the intake endpoint (Function URL)"
    )
    parser.add_argument("--out", default=OUTPUT, help="where to write the page")
    args = parser.parse_args()
    html = build(args.api.strip())
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    size_kb = len(html.encode("utf-8")) // 1024
    print(f"Page built: {args.out} ({size_kb} KB, endpoint {args.api.strip()})")


if __name__ == "__main__":
    main()
