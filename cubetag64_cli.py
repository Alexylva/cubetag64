#!/usr/bin/env python3
"""
CubeTag64 CLI (`cubetag64_cli.py`) — Brother P-Touch Cube (PT-P300BT) 64px Tag Studio & Driver
Motherboard: MBH7BTZ42A-100101 | 180 DPI (64 active vertical dots on 12mm TZe tape)

Features:
  - Zero-dependency integer-pixel DataMatrix (ECC 200) & Code 128B + 5x7 1-bit font renderer
  - Homebox, Snipe-IT, Grocy, and Shelf CSV batch importer (`--csv inventory.csv`)
  - Pure-Python 64px PNG reader & writer (uses stdlib `zlib` & `struct`; optional Pillow fallback)
  - Hardware-verified PTCBP + TIFF PackBits (`4D 02`) Bluetooth RFCOMM driver (`pyserial`)
  - Full 32-byte `ESC i S` status inspector with human-readable Error Info 1/2 decoding
    and post-print completion handshake (`status[18] == 0x01`) before closing port.

Examples:
  python cubetag64_cli.py --csv homebox_export.csv --out strip.png
  python cubetag64_cli.py --csv snipeit_export.csv --preset snipeit --port COM5
  python cubetag64_cli.py --items "IT-001|USB-C PD|100W 2m;TL-014|TORX T8|Precision" --port COM5
  python cubetag64_cli.py --png strip.png --port /dev/rfcomm0
  python cubetag64_cli.py --port COM5 --status-only
"""

import argparse
import csv
import os
import struct
import sys
import time
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

# =============================================================================
# 1. DATA MATRIX (ECC 200) & CODE 128B INTEGER-PIXEL ENCODERS
# =============================================================================

DM_SPECS = [
    {"rows": 10, "cols": 10, "data_bytes": 3,  "ec_bytes": 5},
    {"rows": 12, "cols": 12, "data_bytes": 5,  "ec_bytes": 7},
    {"rows": 14, "cols": 14, "data_bytes": 8,  "ec_bytes": 10},
    {"rows": 16, "cols": 16, "data_bytes": 12, "ec_bytes": 12},
    {"rows": 18, "cols": 18, "data_bytes": 18, "ec_bytes": 14},
    {"rows": 20, "cols": 20, "data_bytes": 22, "ec_bytes": 18},
]


def encode_datamatrix_ascii(text: str) -> List[int]:
    cw: List[int] = []
    i = 0
    n = len(text)
    while i < n:
        c1 = ord(text[i])
        c2 = ord(text[i + 1]) if (i + 1 < n) else -1
        if 48 <= c1 <= 57 and 48 <= c2 <= 57:
            cw.append(130 + (c1 - 48) * 10 + (c2 - 48))
            i += 2
        else:
            cw.append((c1 & 0x7F) + 1)
            i += 1
    return cw


def compute_reed_solomon_dm(data_cw: Sequence[int], ec_len: int) -> List[int]:
    gf_exp = [0] * 512
    gf_log = [0] * 256
    x = 1
    for i in range(255):
        gf_exp[i] = x
        gf_log[x] = i
        x <<= 1
        if x & 256:
            x ^= 301
    for i in range(255, 512):
        gf_exp[i] = gf_exp[i - 255]

    g = [1]
    for i in range(1, ec_len + 1):
        nxt = [0] * (len(g) + 1)
        root = gf_exp[i]
        for j in range(len(g)):
            nxt[j] ^= g[j]
            if g[j] != 0:
                nxt[j + 1] ^= gf_exp[gf_log[g[j]] + gf_log[root]]
        g = nxt

    ecc = [0] * ec_len
    for b in data_cw:
        factor = b ^ ecc[0]
        ecc = ecc[1:] + [0]
        if factor != 0:
            log_f = gf_log[factor]
            for j in range(ec_len):
                g_coeff = g[j + 1]
                if g_coeff != 0:
                    ecc[j] ^= gf_exp[log_f + gf_log[g_coeff]]
    return ecc


def generate_datamatrix_grid(text: str) -> List[List[int]]:
    cw = encode_datamatrix_ascii(text or "0")
    spec = DM_SPECS[-1]
    for s in DM_SPECS:
        if len(cw) <= s["data_bytes"]:
            spec = s
            break

    padded = list(cw[: spec["data_bytes"]])
    if len(padded) < spec["data_bytes"]:
        padded.append(129)
        while len(padded) < spec["data_bytes"]:
            r = ((149 * (len(padded) + 1)) % 253) + 1
            padded.append((129 + r) if (129 + r) <= 254 else (129 + r - 254))

    ecc = compute_reed_solomon_dm(padded, spec["ec_bytes"])
    all_cw = padded + ecc

    nrow = spec["rows"] - 2
    ncol = spec["cols"] - 2
    matrix_map = [-1] * (nrow * ncol)

    def module(r: int, c: int, ch_idx: int, bit: int) -> None:
        if r < 0:
            r += nrow
            c += 4 - ((nrow + 4) % 8)
        if c < 0:
            c += ncol
            r += 4 - ((ncol + 4) % 8)
        v = (all_cw[ch_idx] >> (8 - bit)) & 1
        matrix_map[r * ncol + c] = v

    def utah(r: int, c: int, ch_idx: int) -> None:
        module(r - 2, c - 2, ch_idx, 1)
        module(r - 2, c - 1, ch_idx, 2)
        module(r - 1, c - 2, ch_idx, 3)
        module(r - 1, c - 1, ch_idx, 4)
        module(r - 1, c,     ch_idx, 5)
        module(r,     c - 2, ch_idx, 6)
        module(r,     c - 1, ch_idx, 7)
        module(r,     c,     ch_idx, 8)

    def corner1(ch_idx: int) -> None:
        module(nrow - 1, 0, ch_idx, 1)
        module(nrow - 1, 1, ch_idx, 2)
        module(nrow - 1, 2, ch_idx, 3)
        module(0, ncol - 2, ch_idx, 4)
        module(0, ncol - 1, ch_idx, 5)
        module(1, ncol - 1, ch_idx, 6)
        module(2, ncol - 1, ch_idx, 7)
        module(3, ncol - 1, ch_idx, 8)

    def corner2(ch_idx: int) -> None:
        module(nrow - 3, 0, ch_idx, 1)
        module(nrow - 2, 0, ch_idx, 2)
        module(nrow - 1, 0, ch_idx, 3)
        module(0, ncol - 4, ch_idx, 4)
        module(0, ncol - 3, ch_idx, 5)
        module(0, ncol - 2, ch_idx, 6)
        module(0, ncol - 1, ch_idx, 7)
        module(1, ncol - 1, ch_idx, 8)

    def corner3(ch_idx: int) -> None:
        module(nrow - 3, 0, ch_idx, 1)
        module(nrow - 2, 0, ch_idx, 2)
        module(nrow - 1, 0, ch_idx, 3)
        module(0, ncol - 2, ch_idx, 4)
        module(0, ncol - 1, ch_idx, 5)
        module(1, ncol - 1, ch_idx, 6)
        module(2, ncol - 1, ch_idx, 7)
        module(3, ncol - 1, ch_idx, 8)

    def corner4(ch_idx: int) -> None:
        module(nrow - 1, 0, ch_idx, 1)
        module(nrow - 1, ncol - 1, ch_idx, 2)
        module(0, ncol - 3, ch_idx, 3)
        module(0, ncol - 2, ch_idx, 4)
        module(0, ncol - 1, ch_idx, 5)
        module(1, ncol - 3, ch_idx, 6)
        module(1, ncol - 2, ch_idx, 7)
        module(1, ncol - 1, ch_idx, 8)

    chr_idx = 0
    row = 4
    col = 0
    while True:
        if row == nrow and col == 0:
            corner1(chr_idx)
            chr_idx += 1
        if row == nrow - 2 and col == 0 and (ncol % 4 != 0):
            corner2(chr_idx)
            chr_idx += 1
        if row == nrow - 2 and col == 0 and (ncol % 8 == 4):
            corner3(chr_idx)
            chr_idx += 1
        if row == nrow + 4 and col == 2 and (ncol % 8 == 0):
            corner4(chr_idx)
            chr_idx += 1

        while True:
            if row < nrow and col >= 0 and matrix_map[row * ncol + col] == -1:
                utah(row, col, chr_idx)
                chr_idx += 1
            row -= 2
            col += 2
            if not (row >= 0 and col < ncol):
                break
        row += 1
        col += 3

        while True:
            if row >= 0 and col < ncol and matrix_map[row * ncol + col] == -1:
                utah(row, col, chr_idx)
                chr_idx += 1
            row += 2
            col -= 2
            if not (row < nrow and col >= 0):
                break
        row += 3
        col += 1

        if not (row < nrow or col < ncol):
            break

    if matrix_map[nrow * ncol - 1] == -1:
        matrix_map[nrow * ncol - 1] = 1
        matrix_map[(nrow - 1) * ncol - 2] = 1

    grid: List[List[int]] = []
    for r in range(spec["rows"]):
        line = [0] * spec["cols"]
        for c in range(spec["cols"]):
            if r == spec["rows"] - 1 or c == 0:
                line[c] = 1
            elif r == 0:
                line[c] = 1 if (c % 2 == 0) else 0
            elif c == spec["cols"] - 1:
                line[c] = 1 if (r % 2 == 1) else 0
            else:
                val = matrix_map[(r - 1) * ncol + (c - 1)]
                line[c] = 1 if val == 1 else 0
        grid.append(line)
    return grid


CODE128_PATTERNS = [
    "11011001100","11001101100","11001100110","10010011000","10010001100","10001001100","10011001000","10011000100",
    "10001100100","11001001000","11001000100","11000100100","10110011100","10011011100","10011001110","10111001100",
    "10011101100","10011100110","11001110010","11001011100","11001001110","11011100100","11001110100","11101101110",
    "11101001100","11100101100","11100100110","11101100100","11100110100","11100110010","11011011000","11011000110",
    "11000110110","10100011000","10001011000","10001000110","10110001000","10001101000","10001100010","11010001000",
    "11000101000","11000100010","10110111000","10110001110","10001101110","10111011000","10111000110","10001110110",
    "11101110110","11010001110","11000101110","11011101000","11011100010","11011101110","11101011000","11101000110",
    "11100010110","11101101000","11101100010","11100011010","11101111010","11001000010","11110001010","10100110000",
    "10100001100","10010110000","10010000110","10000101100","10000100110","10110010000","10110000100","10011010000",
    "10011000010","10000110100","10000110010","11000010010","11001010000","11110111010","11000010100","10001111010",
    "10100111100","10010111100","10010011110","10111100100","10011110100","10011110010","11110100100","11110010100",
    "11110010010","11011011110","11011110110","11110110110","10101111000","10100011110","10001011110","10111101000",
    "10111100010","11110101000","11110100010","10111011110","10111101110","11101011110","11110101110","11010000100",
    "11010010000","11010011100","1100011101011",
]


def generate_code128_bars(text: str) -> str:
    clean = "".join(ch for ch in (text or "0") if 32 <= ord(ch) <= 126)
    codes = [104]
    checksum = 104
    for i, ch in enumerate(clean):
        val = ord(ch) - 32
        codes.append(val)
        checksum += val * (i + 1)
    codes.append(checksum % 103)
    codes.append(106)
    return "".join(CODE128_PATTERNS[c] for c in codes)


# =============================================================================
# 2. 5x7 MONOCHROME BITMAP FONT & 64px 1-BIT CANVAS RENDERER
# =============================================================================

FONT_5X7: Dict[str, List[int]] = {
    " ": [0x00,0x00,0x00,0x00,0x00],
    "#": [0x14,0x7F,0x14,0x7F,0x14],
    "-": [0x08,0x08,0x08,0x08,0x08],
    ".": [0x00,0x60,0x60,0x00,0x00],
    "/": [0x20,0x10,0x08,0x04,0x02],
    ":": [0x00,0x36,0x36,0x00,0x00],
    "_": [0x40,0x40,0x40,0x40,0x40],
    "0": [0x3E,0x51,0x49,0x45,0x3E],
    "1": [0x00,0x42,0x7F,0x40,0x00],
    "2": [0x42,0x61,0x51,0x49,0x46],
    "3": [0x21,0x41,0x45,0x4B,0x31],
    "4": [0x18,0x14,0x12,0x7F,0x10],
    "5": [0x27,0x45,0x45,0x45,0x39],
    "6": [0x3C,0x4A,0x49,0x49,0x30],
    "7": [0x01,0x71,0x09,0x05,0x03],
    "8": [0x36,0x49,0x49,0x49,0x36],
    "9": [0x06,0x49,0x49,0x29,0x1E],
    "A": [0x7E,0x11,0x11,0x11,0x7E],
    "B": [0x7F,0x49,0x49,0x49,0x36],
    "C": [0x3E,0x41,0x41,0x41,0x22],
    "D": [0x7F,0x41,0x41,0x22,0x1C],
    "E": [0x7F,0x49,0x49,0x49,0x41],
    "F": [0x7F,0x09,0x09,0x09,0x01],
    "G": [0x3E,0x41,0x49,0x49,0x7A],
    "H": [0x7F,0x08,0x08,0x08,0x7F],
    "I": [0x00,0x41,0x7F,0x41,0x00],
    "J": [0x20,0x40,0x41,0x3F,0x01],
    "K": [0x7F,0x08,0x14,0x22,0x41],
    "L": [0x7F,0x40,0x40,0x40,0x40],
    "M": [0x7F,0x02,0x0C,0x02,0x7F],
    "N": [0x7F,0x04,0x08,0x10,0x7F],
    "O": [0x3E,0x41,0x41,0x41,0x3E],
    "P": [0x7F,0x09,0x09,0x09,0x06],
    "Q": [0x3E,0x41,0x51,0x21,0x5E],
    "R": [0x7F,0x09,0x19,0x29,0x46],
    "S": [0x46,0x49,0x49,0x49,0x31],
    "T": [0x01,0x01,0x7F,0x01,0x01],
    "U": [0x3F,0x40,0x40,0x40,0x3F],
    "V": [0x1F,0x20,0x40,0x20,0x1F],
    "W": [0x3F,0x40,0x38,0x40,0x3F],
    "X": [0x63,0x14,0x08,0x14,0x63],
    "Y": [0x07,0x08,0x70,0x08,0x07],
    "Z": [0x61,0x51,0x49,0x45,0x43],
}


def measure_bitmap_text(text: str, scale: int = 2) -> int:
    n = len(text or "")
    return (n * 6 * scale - scale) if n > 0 else 0


def fill_rect(grid_64: List[List[int]], x: int, y: int, w: int, h: int, val: int = 1) -> None:
    max_w = len(grid_64[0]) if grid_64 else 0
    for yy in range(max(0, y), min(64, y + h)):
        row = grid_64[yy]
        for xx in range(max(0, x), min(max_w, x + w)):
            row[xx] = val


def draw_bitmap_text(grid_64: List[List[int]], text: str, x: int, y: int, scale: int = 2) -> int:
    s = (text or "").upper()
    cur_x = x
    for ch in s:
        glyph = FONT_5X7.get(ch, FONT_5X7[" "])
        for col in range(5):
            col_bits = glyph[col]
            for row in range(7):
                if (col_bits >> row) & 1:
                    fill_rect(grid_64, cur_x + col * scale, y + row * scale, scale, scale, 1)
        cur_x += 6 * scale
    return cur_x - x


def render_single_label(
    code: str,
    title: str,
    subtitle: str,
    symbology: str = "datamatrix",
    module_scale: str = "auto",
    layout: str = "side",
) -> List[List[int]]:
    """Returns a 64-row 2D list (`grid[y][x]`, 1 = black thermal dot, 0 = white)."""
    if symbology == "code128":
        bits = generate_code128_bars(code)
        chosen_scale = 2 if module_scale == "auto" else min(3, int(module_scale))
        quiet_px = 10
        code_width = quiet_px * 2 + len(bits) * chosen_scale
        text_width = (
            max(measure_bitmap_text(code, 2), measure_bitmap_text(title, 2)) + 16
            if layout == "side"
            else 0
        )
        width = code_width + text_width
        canvas = [[0] * width for _ in range(64)]
        for i, b in enumerate(bits):
            if b == "1":
                fill_rect(canvas, quiet_px + i * chosen_scale, 4, chosen_scale, 40, 1)
        draw_bitmap_text(canvas, code, quiet_px + 4, 48, 2)
        if layout == "side" and text_width > 0:
            draw_bitmap_text(canvas, title[:12], code_width + 6, 12, 2)
            if subtitle:
                draw_bitmap_text(canvas, subtitle[:14], code_width + 6, 36, 2)
        return canvas

    dm = generate_datamatrix_grid(code)
    rows = len(dm)
    cols = len(dm[0])
    if module_scale == "auto":
        chosen_scale = max(1, 60 // rows)
    else:
        chosen_scale = int(module_scale)
        if rows * chosen_scale > 64:
            chosen_scale = max(1, 64 // rows)

    matrix_h = rows * chosen_scale
    matrix_w = cols * chosen_scale
    quiet_left = 8
    quiet_right = 8
    top_y = (64 - matrix_h) // 2

    text_block_w = 0
    if layout == "side":
        l1 = measure_bitmap_text(code, 2)
        l2 = measure_bitmap_text(title[:14], 2)
        l3 = measure_bitmap_text(subtitle[:22], 1)
        text_block_w = max(l1, l2, l3) + 14
    elif layout == "compact":
        text_block_w = measure_bitmap_text(code, 2) + 12

    width = quiet_left + matrix_w + quiet_right + text_block_w
    canvas = [[0] * width for _ in range(64)]

    for r in range(rows):
        for c in range(cols):
            if dm[r][c]:
                fill_rect(canvas, quiet_left + c * chosen_scale, top_y + r * chosen_scale, chosen_scale, chosen_scale, 1)

    text_start_x = quiet_left + matrix_w + quiet_right
    if layout == "side":
        draw_bitmap_text(canvas, code, text_start_x, 6, 2)
        if title:
            draw_bitmap_text(canvas, title[:14], text_start_x, 26, 2)
        if subtitle:
            draw_bitmap_text(canvas, subtitle[:22], text_start_x, 46, 1)
    elif layout == "compact":
        draw_bitmap_text(canvas, code, text_start_x, 25, 2)

    return canvas


def render_strip_bitmap(
    items: Sequence[Tuple[str, str, str]],
    symbology: str = "datamatrix",
    module_scale: str = "auto",
    layout: str = "side",
    gap_px: int = 18,
    cut_marks: str = "dashed",
) -> List[List[int]]:
    labels = [render_single_label(c, t, s, symbology, module_scale, layout) for (c, t, s) in items]
    trailing_pad = max(10, (gap_px // 2) + 4) if cut_marks != "none" else 8
    total_width = sum(len(lbl[0]) for lbl in labels) + max(0, len(labels) - 1) * gap_px + 8 + trailing_pad
    strip = [[0] * total_width for _ in range(64)]

    def draw_cut_mark_at(x_pos: int) -> None:
        if cut_marks == "dashed":
            for y in range(0, 64, 6):
                fill_rect(strip, x_pos, y, 1, 3, 1)
        elif cut_marks == "ticks":
            fill_rect(strip, x_pos, 0, 2, 6, 1)
            fill_rect(strip, x_pos, 58, 2, 6, 1)

    cur_x = 8
    for idx, lbl in enumerate(labels):
        lw = len(lbl[0])
        for y in range(64):
            strip[y][cur_x : cur_x + lw] = lbl[y]
        cur_x += lw

        if idx < len(labels) - 1:
            draw_cut_mark_at(cur_x + (gap_px // 2))
            cur_x += gap_px
        elif cut_marks != "none":
            draw_cut_mark_at(cur_x + (trailing_pad // 2))

    return strip


# =============================================================================
# 3. CSV IMPORTER (HOMEBOX, SNIPE-IT, GROCY, SHELF & GENERIC)
# =============================================================================

def parse_csv_inventory(csv_path: str, preset: str = "auto") -> List[Tuple[str, str, str]]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ","
        if "\t" in sample.splitlines()[0]:
            delimiter = "\t"
        elif "," not in sample.splitlines()[0] and ";" in sample.splitlines()[0]:
            delimiter = ";"
        rows = [[cell.strip() for cell in row] for row in csv.reader(f, delimiter=delimiter) if any(cell.strip() for cell in row)]

    if not rows:
        return []

    hdr = [h.lower().replace("hb.", "").replace(" ", "_").replace("-", "_") for h in rows[0]]

    def find_col(candidates: Sequence[str]) -> int:
        for c in candidates:
            if c in hdr:
                return hdr.index(c)
        for c in candidates:
            for idx, h in enumerate(hdr):
                if c in h:
                    return idx
        return -1

    if preset == "homebox":
        code_idx = find_col(["asset_id", "import_ref", "id", "code"])
        title_idx = find_col(["name", "title", "item"])
        sub_idx = find_col(["location", "description", "labels", "parent"])
    elif preset == "snipeit":
        code_idx = find_col(["asset_tag", "tag", "id", "serial"])
        title_idx = find_col(["asset_name", "name", "model", "item_name"])
        sub_idx = find_col(["location", "category", "model_no", "serial", "department"])
    elif preset == "grocy":
        code_idx = find_col(["barcode", "grocycode", "id", "code", "asset_id"])
        title_idx = find_col(["name", "product", "title"])
        sub_idx = find_col(["location", "product_group", "category", "qu_id_stock"])
    else:
        code_idx = find_col(["asset_tag", "asset_id", "barcode", "code", "tag", "sku", "id"])
        title_idx = find_col(["asset_name", "name", "title", "model", "product", "item"])
        sub_idx = find_col(["location", "sub", "subtitle", "category", "description", "serial", "bin"])

    looks_like_header = (code_idx != -1) or (title_idx != -1)
    start_row = 1 if looks_like_header else 0

    if not looks_like_header:
        code_idx = 0
        title_idx = 1 if len(rows[0]) > 1 else 0
        sub_idx = 2 if len(rows[0]) > 2 else -1
    else:
        if code_idx == -1:
            code_idx = 0
        if title_idx == -1:
            title_idx = (1 if code_idx == 0 else 0) if len(rows[0]) > 1 else code_idx
        if sub_idx == -1 and len(rows[0]) > 2:
            for candidate_i in (0, 1, 2):
                if candidate_i not in (code_idx, title_idx):
                    sub_idx = candidate_i
                    break

    items: List[Tuple[str, str, str]] = []
    for r_idx in range(start_row, len(rows)):
        cols = rows[r_idx]
        code = (cols[code_idx] if code_idx < len(cols) and cols[code_idx] else f"IT-{r_idx:03d}").strip()
        title = (cols[title_idx] if (title_idx != -1 and title_idx < len(cols) and cols[title_idx]) else code).strip()
        sub = (cols[sub_idx] if (sub_idx != -1 and sub_idx < len(cols) and cols[sub_idx]) else "").strip()
        items.append((code, title, sub))
    return items


# =============================================================================
# 4. PURE-PYTHON 64px PNG READER & WRITER (ZERO EXTERNAL IMAGE DEPS)
# =============================================================================

def save_strip_png(strip_64: List[List[int]], out_path: str) -> None:
    """Writes a 64px-tall 8-bit grayscale PNG using only Python stdlib `zlib` & `struct`."""
    height = len(strip_64)
    width = len(strip_64[0]) if height else 0

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)  # Filter type 0 (None)
        for x in range(width):
            raw_rows.append(0x00 if strip_64[y][x] else 0xFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), 9)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", idat)
        + make_chunk(b"IEND", b"")
    )
    with open(out_path, "wb") as f:
        f.write(png_bytes)


def load_strip_png(png_path: str) -> List[List[int]]:
    """Loads a 64px-tall PNG into `strip[y][x]` (1 = black dot, 0 = white). Uses Pillow if available or stdlib PNG parser."""
    try:
        from PIL import Image  # type: ignore

        img = Image.open(png_path).convert("L")
        w, h = img.size
        if h != 64:
            raise ValueError(f"Expected 64px PNG height for PT-P300BT printhead, got {h}px.")
        px = img.load()
        return [[1 if px[x, y] < 128 else 0 for x in range(w)] for y in range(64)]
    except ImportError:
        pass

    with open(png_path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{png_path} is not a valid PNG file.")

    pos = 8
    width = height = bit_depth = color_type = 0
    idat_chunks = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", chunk_data)
        elif ctype == b"IDAT":
            idat_chunks.extend(chunk_data)
        elif ctype == b"IEND":
            break

    if height != 64:
        raise ValueError(f"Expected 64px PNG height for PT-P300BT printhead, got {height}px.")

    raw = zlib.decompress(bytes(idat_chunks))
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type, 1)
    stride = width * bpp
    strip = [[0] * width for _ in range(64)]
    prev_row = bytearray(stride)

    offset = 0
    for y in range(64):
        ftype = raw[offset]
        offset += 1
        cur_row = bytearray(raw[offset : offset + stride])
        offset += stride
        if ftype == 1:  # Sub
            for i in range(bpp, stride):
                cur_row[i] = (cur_row[i] + cur_row[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                cur_row[i] = (cur_row[i] + prev_row[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = cur_row[i - bpp] if i >= bpp else 0
                cur_row[i] = (cur_row[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = cur_row[i - bpp] if i >= bpp else 0
                b = prev_row[i]
                c = prev_row[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur_row[i] = (cur_row[i] + pr) & 0xFF
        prev_row = cur_row
        for x in range(width):
            lum = cur_row[x * bpp]
            strip[y][x] = 1 if lum < 128 else 0
    return strip


# =============================================================================
# 5. HARDWARE-VERIFIED PT-P300BT RASTER & TIFF PACKBITS (`4D 02`) DRIVER
# =============================================================================

ERROR_INFO_1_BITS = [
    (0x01, "No Media"),
    (0x02, "End of Media"),
    (0x04, "Cutter Jam"),
    (0x08, "Weak Batteries"),
    (0x10, "Printer in Use"),
    (0x20, "Printer Turned Off"),
    (0x40, "High-Voltage Adapter"),
    (0x80, "Fan Motor Error"),
]

ERROR_INFO_2_BITS = [
    (0x01, "Replace Media"),
    (0x02, "Expansion Buffer Full"),
    (0x04, "Communication Error"),
    (0x08, "Communication Buffer Full"),
    (0x10, "Cover Open"),
    (0x20, "Overheating"),
    (0x40, "Wrong Media"),
    (0x80, "System Error"),
]

MEDIA_TYPE_NAMES = {
    0x00: "No Media",
    0x01: "Laminated TZe",
    0x03: "Non-Laminated",
    0x11: "Heat-Shrink Tube (HSe)",
    0xFF: "Incompatible Media",
}

STATUS_TYPE_NAMES = {
    0x00: "Reply to Status Request (0x00)",
    0x01: "Printing Completed (0x01)",
    0x02: "Error Occurred (0x02)",
    0x04: "Turned Off (0x04)",
    0x05: "Notification (0x05)",
    0x06: "Phase Change (0x06)",
}


def decode_status_packet(status: bytes) -> Dict[str, object]:
    hex_str = " ".join(f"{b:02X}" for b in status[:32])
    if len(status) < 32 or status[0] != 0x80:
        return {"valid": False, "hex": hex_str, "errors": ["Invalid/short status packet"]}

    err1 = status[8]
    err2 = status[9]
    tape_width_mm = status[10]
    media_type = status[11]
    status_type = status[18]
    phase_type = status[19]

    errors: List[str] = []
    for mask, name in ERROR_INFO_1_BITS:
        if err1 & mask:
            errors.append(name)
    for mask, name in ERROR_INFO_2_BITS:
        if err2 & mask:
            errors.append(name)

    return {
        "valid": True,
        "hex": hex_str,
        "err1": err1,
        "err2": err2,
        "errors": errors,
        "tape_width_mm": tape_width_mm or 0x0C,
        "media_type": media_type or 0x01,
        "media_name": MEDIA_TYPE_NAMES.get(media_type, f"0x{media_type:02X}"),
        "status_type": status_type,
        "status_type_name": STATUS_TYPE_NAMES.get(status_type, f"0x{status_type:02X}"),
        "phase_type": phase_type,
        "phase_name": "Printing (0x01)" if phase_type == 0x01 else "Ready (0x00)",
    }


def strip_to_raster_columns(strip_64: List[List[int]], flip_y: bool = False) -> List[bytes]:
    """
    Converts a 64px-tall bitmap strip into 16-byte (128-bit) vertical columns.
    Hardware-verified on MBH7BTZ42A-100101 (`docs/hardware-proof.jpg`):
      Bits 0..31  (4B) = 0x00 top margin
      Bits 32..95 (8B) = 64 active printhead dots (`bitPos = 32 + y`)
      Bits 96..127(4B) = 0x00 bottom margin
    """
    width = len(strip_64[0]) if strip_64 else 0
    cols: List[bytes] = []
    for x in range(width):
        col_bytes = bytearray(16)
        for y in range(64):
            if strip_64[y][x]:
                bit_pos = (32 + (63 - y)) if flip_y else (32 + y)
                byte_idx = bit_pos // 8
                bit_offset = 7 - (bit_pos % 8)
                col_bytes[byte_idx] |= 1 << bit_offset
        cols.append(bytes(col_bytes))
    return cols


def packbits_encode_16(col16: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(col16)
    while i < n:
        run_len = 1
        while i + run_len < n and col16[i + run_len] == col16[i] and run_len < 128:
            run_len += 1
        if run_len >= 2:
            out.append((257 - run_len) & 0xFF)
            out.append(col16[i])
            i += run_len
        else:
            lit_start = i
            lit_len = 0
            while i < n and lit_len < 128:
                if i + 1 < n and col16[i] == col16[i + 1]:
                    break
                i += 1
                lit_len += 1
            out.append((lit_len - 1) & 0xFF)
            out.extend(col16[lit_start : lit_start + lit_len])
    return bytes(out)


def build_ptcbp_stream(
    raster_cols: Sequence[bytes],
    media_type: int = 0x01,
    tape_width_mm: int = 0x0C,
    overfeed_dots: int = 0,
) -> bytes:
    n_lines = len(raster_cols) + max(0, overfeed_dots)
    out = bytearray()
    # 4. Enter PTCBP Raster Mode (ESC i a 0x01)
    out.extend(b"\x1b\x69\x61\x01")
    # 5. Auto Status Notification (ESC i ! 0x00)
    out.extend(b"\x1b\x69\x21\x00")
    # 6. Print Info Header (ESC i z): 0xC4, media_type, tape_width_mm, 0x00, n_lines (4B LE), 0x00, 0x00
    out.extend(bytes([0x1B, 0x69, 0x7A, 0xC4, media_type & 0xFF, tape_width_mm & 0xFF, 0x00]))
    out.extend(struct.pack("<I", n_lines))
    out.extend(b"\x00\x00")
    # 7. Mode Flags: Auto-cut/mirror (1B 69 4D 40), Expanded cut/feed at end (1B 69 4B 08), Zero margin (1B 69 64 00 00)
    out.extend(b"\x1b\x69\x4d\x40")
    out.extend(b"\x1b\x69\x4b\x08")
    out.extend(b"\x1b\x69\x64\x00\x00")
    # 8. Enable TIFF PackBits Compression (4D 02) — MANDATORY on PT-P300BT
    out.extend(b"\x4d\x02")
    # 9. Stream Raster Columns ('Z' = 0x5A for all-zero column, 'G' = 0x47 <len_lo> 0x00 <packbits> for non-zero)
    for col16 in raster_cols:
        if all(b == 0 for b in col16):
            out.append(0x5A)
        else:
            packed = packbits_encode_16(col16)
            out.extend(bytes([0x47, len(packed) & 0xFF, (len(packed) >> 8) & 0xFF]) + packed)
    # 9B. Trailing cutter overfeed ('Z' = 0x5A zero-columns) so the last tag clears the 24mm cutter blade
    if overfeed_dots > 0:
        out.extend(b"\x5a" * overfeed_dots)
    # 10. Finalize: Print with feed (0x1A)
    out.append(0x1A)
    return bytes(out)


def print_over_rfcomm(
    port: str,
    raster_cols: Sequence[bytes],
    status_only: bool = False,
    overfeed_mm: float = 0.0,
) -> int:
    try:
        import serial  # type: ignore
    except ImportError:
        print("ERROR: pyserial is not installed. Run: pip install pyserial", file=sys.stderr)
        return 1

    print(f"[CubeTag64] Opening Bluetooth RFCOMM serial port {port} (9600 baud)...")
    with serial.Serial(port, baudrate=9600, timeout=1.5) as ser:
        # 1. Invalidate (100x 0x00) + 2. Initialize (1B 40)
        ser.write(b"\x00" * 100 + b"\x1b\x40")
        ser.flush()
        time.sleep(0.15)

        # 3. Status Handshake (1B 69 53)
        ser.write(b"\x1b\x69\x53")
        ser.flush()
        status_raw = ser.read(32)
        info = decode_status_packet(status_raw)

        if info["valid"]:
            print(f"[ESC i S] Raw 32B Hex : {info['hex']}")
            print(f"[ESC i S] Media       : {info['tape_width_mm']}mm {info['media_name']} (0x{info['media_type']:02X})")
            print(f"[ESC i S] Phase/State : {info['status_type_name']} | {info['phase_name']}")
            err_list = info["errors"]
            if err_list:
                print(f"[ESC i S] ERROR FLAGS : {', '.join(err_list)} (Err1=0x{info['err1']:02X}, Err2=0x{info['err2']:02X})", file=sys.stderr)
                return 2
            print("[ESC i S] Error Flags : 0x00 / 0x00 (OK)")
        else:
            print(f"[WARN] Did not receive full 32-byte status packet (got {len(status_raw)}B: {info['hex']}). Defaulting to 12mm TZe.")

        if status_only:
            return 0

        tape_width_mm = int(info.get("tape_width_mm", 0x0C))
        media_type = int(info.get("media_type", 0x01))
        overfeed_dots = max(0, int(round((overfeed_mm / 25.4) * 180.0)))

        payload = build_ptcbp_stream(
            raster_cols,
            media_type=media_type,
            tape_width_mm=tape_width_mm,
            overfeed_dots=overfeed_dots,
        )
        print(
            f"[PTCBP] Streaming {len(raster_cols)} tag cols + {overfeed_dots} cutter overfeed cols ({overfeed_mm:.1f}mm) "
            f"[{len(payload)} bytes total] for {tape_width_mm}mm TZe tape..."
        )

        # Stream in 256-byte chunks with 10ms pacing
        chunk_size = 256
        for off in range(0, len(payload), chunk_size):
            ser.write(payload[off : off + chunk_size])
            ser.flush()
            time.sleep(0.01)

        # Wait for post-print 32-byte completion notification (status[18] == 0x01) before closing port
        wait_budget = max(8.0, min(25.0, (len(raster_cols) / 80.0) + 6.0))
        deadline = time.time() + wait_budget
        saw_printing = False
        print("[PTCBP] Waiting for printer completion packet (status[18]==0x01) before closing RFCOMM port...")
        while time.time() < deadline:
            pkt = ser.read(32)
            if not pkt:
                continue
            post = decode_status_packet(pkt)
            if not post["valid"]:
                continue
            print(f"[Status] {post['status_type_name']} | {post['phase_name']} | Hex: {post['hex']}")
            if post["phase_type"] == 0x01:
                saw_printing = True
            if post["status_type"] == 0x01 or (saw_printing and post["status_type"] == 0x06 and post["phase_type"] == 0x00):
                print("[CubeTag64] Print completed cleanly!")
                time.sleep(0.25)
                return 0
            if post["status_type"] == 0x02 or post["errors"]:
                print(f"[ERROR] Printer reported error: {', '.join(post['errors'])}", file=sys.stderr)
                return 3

        time.sleep(0.5)
        print("[CubeTag64] Finished streaming strip.")
    return 0


# =============================================================================
# 6. CLI ENTRYPOINT
# =============================================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="CubeTag64 CLI — 64px Integer-Pixel Tag Generator & Brother PT-P300BT Bluetooth Driver"
    )
    parser.add_argument("--csv", metavar="FILE", help="Import inventory items from Homebox, Snipe-IT, Grocy, or generic CSV")
    parser.add_argument("--preset", choices=["auto", "homebox", "snipeit", "grocy"], default="auto", help="CSV column mapping preset")
    parser.add_argument("--items", metavar="STR", help="Semicolon-separated batch items: 'IT-01|USB-C|2m;TL-14|TORX|T8'")
    parser.add_argument("--code", metavar="ID", help="Single tag ID code (e.g., TL-014)")
    parser.add_argument("--title", metavar="STR", default="", help="Single tag title (e.g., 'TORX T8')")
    parser.add_argument("--sub", metavar="STR", default="", help="Single tag subtitle (e.g., 'Precision')")
    parser.add_argument("--png", metavar="FILE", help="Load an existing 64px-tall PNG strip instead of rendering items")
    parser.add_argument("--out", metavar="FILE", help="Save generated 64px 1-bit strip to PNG file")
    parser.add_argument("--sym", choices=["datamatrix", "code128"], default="datamatrix", help="Barcode symbology (default: datamatrix)")
    parser.add_argument("--scale", choices=["auto", "2", "3", "4", "5"], default="auto", help="Integer thermal dots per module")
    parser.add_argument("--layout", choices=["side", "compact", "codeonly"], default="side", help="Tag layout")
    parser.add_argument("--gap", type=int, default=18, help="Gap between chained tags in pixels (default: 18)")
    parser.add_argument("--overfeed-mm", type=float, default=0.0, help="Extra trailing blank feed in mm beyond the native ESC i K 0x08 cutter feed (default: 0.0)")
    parser.add_argument("--cut", choices=["dashed", "ticks", "none"], default="dashed", help="Scissor cut guide style")
    parser.add_argument("--flip-y", action="store_true", help="Use legacy mirrored Y-order (default is hardware-verified normal top-to-bottom)")
    parser.add_argument("--port", metavar="PORT", help="Bluetooth RFCOMM serial port (e.g., COM5 on Windows or /dev/rfcomm0 on Linux)")
    parser.add_argument("--status-only", action="store_true", help="Only query and decode the 32-byte ESC i S printer status packet")

    args = parser.parse_args(argv)

    if args.status_only:
        if not args.port:
            parser.error("--status-only requires --port (e.g., --port COM5)")
        return print_over_rfcomm(args.port, [], status_only=True)

    if args.png:
        strip_64 = load_strip_png(args.png)
        print(f"[CubeTag64] Loaded 64px x {len(strip_64[0])}px strip from {args.png}")
    else:
        items: List[Tuple[str, str, str]] = []
        if args.csv:
            items = parse_csv_inventory(args.csv, preset=args.preset)
            print(f"[CubeTag64] Imported {len(items)} inventory tags from CSV '{args.csv}' (preset={args.preset})")
        elif args.items:
            for raw_item in args.items.split(";"):
                if not raw_item.strip():
                    continue
                parts = [p.strip() for p in raw_item.split("|")]
                code = parts[0] if len(parts) > 0 and parts[0] else "IT-001"
                title = parts[1] if len(parts) > 1 and parts[1] else code
                sub = parts[2] if len(parts) > 2 else ""
                items.append((code, title, sub))
        elif args.code:
            items = [(args.code, args.title or args.code, args.sub)]
        else:
            items = [
                ("IT-001", "USB-C PD", "100W 2m"),
                ("IT-002", "RJ45 CAT6", "Patch 1.5m"),
                ("TL-014", "TORX T8", "Precision"),
                ("PT-089", "ESP32-S3", "DevKitC-1"),
            ]

        if not items:
            print("ERROR: No inventory items to render.", file=sys.stderr)
            return 1

        strip_64 = render_strip_bitmap(
            items,
            symbology=args.sym,
            module_scale=args.scale,
            layout=args.layout,
            gap_px=max(8, min(64, args.gap)),
            cut_marks=args.cut,
        )
        width_px = len(strip_64[0])
        length_mm = (width_px / 180.0) * 25.4
        saved_mm = max(0, (len(items) - 1) * 24)
        print(f"[CubeTag64] Rendered {len(items)} tag(s) -> 64px x {width_px}px ({length_mm:.1f}mm strip, {saved_mm}mm TZe tape saved)")

    if args.out:
        save_strip_png(strip_64, args.out)
        print(f"[CubeTag64] Saved 1-bit 64px PNG strip to {args.out}")

    if args.port:
        raster_cols = strip_to_raster_columns(strip_64, flip_y=args.flip_y)
        return print_over_rfcomm(args.port, raster_cols, status_only=False, overfeed_mm=args.overfeed_mm)

    if not args.out and not args.port:
        default_out = "cubetag64_strip.png"
        save_strip_png(strip_64, default_out)
        print(f"[CubeTag64] Saved 1-bit 64px PNG strip to {default_out} (pass --port COMx to print directly)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
