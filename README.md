# CubeTag64 — 64px Integer-Aligned Inventory Tag Studio & Static URL API for Brother PT-P300BT

**CubeTag64** turns the consumer **Brother P-Touch Cube (PT-P300BT)** (`MBH7BTZ42A-100101` PCB, 180 DPI, 12mm TZe laminated thermal-transfer tape) into a razor-sharp inventory tag printer for **Homebox**, **Snipe-IT**, **Grocy**, and **Shelf**.

![Hardware Proof on Brother PT-P300BT](docs/hardware-proof.jpg)

## Why CubeTag64?
1. **Zero Anti-Aliasing (Strict Integer Dots)**: Brother's mobile app scales QR/barcodes to non-integer pixel widths across the 64-dot printhead, blurring small codes. CubeTag64 encodes **DataMatrix (`ECC 200`)**, **QR v1 (`21x21`)**, and **Code 128B** directly at integer dot multiples (`3x3` or `4x4` physical thermal dots per module).
2. **Chain-Print Strips (No 24mm Waste)**: Because the manual cutter sits 24mm past the printhead, single-label printing wastes 2.4 cm of TZe tape per tag. CubeTag64 packs multiple inventory tags into a continuous strip with scissor cut guides and drives the printer with the PTCBP `no-feed` (`0x0C`) command.
3. **GitHub Pages Static URL Parameter API**: Hosted on GitHub Pages, CubeTag64 accepts query parameters (`?code=...`, `?items=...`, `&action=png|rawimg|py`) so inventory systems can link directly to it like an API with zero backend.

## GitHub Pages URL API Parameters

| Parameter | Values | Example | Description |
| :--- | :--- | :--- | :--- |
| `code`, `title`, `sub` | Strings | `?code=TL-014&title=TORX+T8&sub=Precision` | Generates a single inventory tag from URL parameters. |
| `items` | Semicolon-separated `CODE\|TITLE\|SUB` | `?items=IT-01\|USB-C\|2m;TL-14\|TORX\|T8` | Generates a multi-tag chain strip in a single URL call. |
| `sym` | `datamatrix` \| `qr` \| `code128` | `&sym=datamatrix` | Selects barcode symbology (default: `datamatrix`). |
| `scale` | `auto` \| `2` \| `3` \| `4` \| `5` | `&scale=4` | Locks integer thermal dots per module. |
| `layout` | `side` \| `compact` \| `codeonly` | `&layout=side` | Selects tag layout. |
| `action` | `png` \| `rawimg` \| `py` | `&action=png` | Auto-downloads the 64px PNG (`png`), renders a pure `<img>` (`rawimg`), or downloads `print_p300bt.py` (`py`). |

## License
MIT
