"""The link-preview image: the page's headline figures as a 1200x630 PNG in its dark theme."""

import base64
import io
import re
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from .core import ROOT

CAP = 100_000 * 10**6  # USDC: the proposal's spending cap, the page's TARGET
W, H = 1200, 630
BG, SURFACE, BORDER = "#0f1210", "#161a17", "#262c28"
INK, MUTED, FAINT = "#edf1ed", "#97a39c", "#626d66"


def whole(raw):
    """Whole USDC rounded to nearest, as the page shows its figures."""
    return f"{(int(raw or 0) + 500_000) // 10**6:,}"


def figures(data):
    """Proceeds, budget spent and budget remaining; dashes when the report has no funding escrow."""
    names = ["Proceeds", "Budget spent", "Budget remaining"]
    if data.get("funding_assets") is None:
        return [("—", name) for name in names]
    spent = int(data.get("spent") or 0)
    values = [(data.get("totals") or {}).get("yearn"), spent, max(CAP - spent, 0)]
    return [(whole(value), name) for value, name in zip(values, names)]


def coin(size):
    """The USDC icon the page embeds, so the image needs no file of its own."""
    page = (ROOT / "dashboard/template.html").read_text()
    encoded = re.search(r"\.usdc\{background-image:url\(data:image/png;base64,([^)]+)\)", page)[1]
    icon = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGBA")
    return icon.resize((size, size), Image.LANCZOS)


def image(data):
    font = lambda size: ImageFont.load_default(size=size)
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # The page's top bar: brand at the left, the report's day at the right.
    x, line = 72, 104
    for text, color in [("Vyper", INK), ("  /  ", FAINT), ("funding", MUTED)]:
        draw.text((x, line), text, font=font(36), fill=color, anchor="ls")
        x += draw.textlength(text, font=font(36))
    if data.get("as_of"):
        day = datetime.fromtimestamp(int(data["as_of"]), timezone.utc)
        draw.text((W - 72, line), f"Updated {day:%b} {day.day}, {day.year}", font=font(28), fill=MUTED, anchor="rs")

    # One card of three figures, each a number with its coin above a muted label. The numbers
    # share one size, the largest at which the widest still fits its column.
    left, top, right, bottom = 48, 160, W - 48, H - 72
    draw.rounded_rectangle((left, top, right, bottom), radius=22, fill=SURFACE, outline=BORDER, width=2)
    shown = figures(data)
    column, pad = (right - left) / len(shown), 44
    size = 84
    while size > 40 and max(
        draw.textlength(value, font=font(size)) + (size * 0.7 if value != "—" else 0) for value, _ in shown
    ) > column - 2 * pad:
        size -= 2
    icon = coin(round(size * 0.56))
    baseline = (top + bottom) // 2 + 4
    for i, (value, name) in enumerate(shown):
        x = left + column * i + pad
        if i:
            draw.line((left + column * i, top + 88, left + column * i, bottom - 88), fill=BORDER, width=2)
        if value == "—":
            # The bundled font has no dash, so an empty figure is drawn as a short bar.
            draw.rectangle((x, baseline - size * 0.34, x + size * 0.42, baseline - size * 0.28), fill=FAINT)
        else:
            draw.text((x, baseline), value, font=font(size), fill=INK, anchor="ls")
            end = x + draw.textlength(value, font=font(size)) + size * 0.14
            img.paste(icon, (round(end), round(baseline - size * 0.36 - icon.height / 2)), icon)
        draw.text((x, baseline + 66), name, font=font(32), fill=MUTED, anchor="ls")

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
