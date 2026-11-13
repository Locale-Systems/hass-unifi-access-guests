"""Draw the brand icon: a door with a calendar badge.

Regenerate with: .venv/bin/python tools/draw_icon.py custom_components/unifi_access_guests/brand
(needs Pillow, which the test harness installs).
"""
import sys
from PIL import Image, ImageDraw

def draw(size: int) -> Image.Image:
    s = 4 * size  # supersample, then downscale for smooth edges
    u = s / 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    def r(x0, y0, x1, y1, rad, **kw):
        d.rounded_rectangle([x0 * u, y0 * u, x1 * u, y1 * u], radius=rad * u, **kw)
    # Door frame and door
    r(40, 16, 168, 240, 14, fill=(55, 71, 79, 255))
    r(56, 32, 152, 240, 8, fill=(30, 136, 229, 255))
    # Panels
    r(70, 52, 124, 108, 6, outline=(144, 202, 249, 255), width=round(6 * u))
    r(70, 140, 124, 220, 6, outline=(144, 202, 249, 255), width=round(6 * u))
    # Knob
    d.ellipse([130 * u, 98 * u, 146 * u, 114 * u], fill=(255, 202, 40, 255))
    # Calendar badge, with a ring so it reads on light and dark backgrounds
    r(124, 124, 244, 244, 22, fill=(255, 255, 255, 255))
    r(132, 132, 236, 236, 16, fill=(250, 250, 250, 255), outline=(55, 71, 79, 255), width=round(6 * u))
    d.rounded_rectangle([132 * u, 132 * u, 236 * u, 166 * u], radius=16 * u, fill=(229, 57, 53, 255),
                        corners=(True, True, False, False))
    for x in (160, 208):
        r(x - 5, 122, x + 5, 146, 5, fill=(55, 71, 79, 255))
    # Check mark
    d.line([(154 * u, 200 * u), (176 * u, 220 * u), (216 * u, 178 * u)],
           fill=(67, 160, 71, 255), width=round(14 * u), joint="curve")
    return img.resize((size, size), Image.LANCZOS)

out = sys.argv[1]
draw(256).save(f"{out}/icon.png", optimize=True)
draw(512).save(f"{out}/icon@2x.png", optimize=True)
