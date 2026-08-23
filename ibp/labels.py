"""Utilities for creating request labels.

The visual design mirrors the historical print-server label so that
volunteers see a familiar label regardless of print path: a full-width
Code128 barcode up top, a "PACKAGE ID" caption beneath it, the inmate
name and ID in the middle, and jurisdiction / unit / shipping method
along the bottom.
"""

import typing
from dataclasses import dataclass

import barcode  # type: ignore
from barcode.writer import ImageWriter  # type: ignore
from PIL import Image, ImageDraw, ImageFont  # type: ignore

from .models import Request


def code128(text: typing.Any, size: tuple[int, int], dpi: int = 300) -> Image.Image:
    """Create a Code128 barcode image for given text within the provided size."""
    writer = ImageWriter()
    options: dict[str, typing.Any] = {
        "write_text": False,
        "writer": writer,
        "dpi": int(dpi),
        "quiet_zone": 0,
    }

    def px2mm(px: int) -> float:  # pylint: disable=invalid-name
        """Convert pixels to millimeters for the given DPI."""
        return 25.4 * px / options["dpi"]

    code = barcode.Code128(str(text), writer=writer)

    raw = code.build()
    modules_per_line = len(raw[0])
    module_width = px2mm(size[0]) / modules_per_line
    options["module_width"] = module_width

    module_height = px2mm(size[1]) - 2  # barcode adds this for some reason
    options["module_height"] = module_height

    return code.render(options)


def build_font_fitter(min_font: int = 1, max_font: int = 100):
    """Build a function that returns a font to best fit text to a box."""
    fonts = {
        font_size: ImageFont.truetype("DejaVuSansMono.ttf", font_size)
        for font_size in range(min_font, max_font)
    }

    def wrapped(size: tuple[int, int], text: str) -> ImageFont.FreeTypeFont:
        size_w, size_h = size

        min_, max_ = min_font, max_font
        while abs(max_ - min_) > 1:
            font_size = (max_ - min_ + 1) // 2 + min_

            font = fonts[font_size]

            text_x0, text_y0, text_x1, text_y1 = font.getbbox(text)
            text_w = text_x1 - text_x0
            text_h = text_y1 - text_y0

            if text_h < size_h and text_w < size_w:
                min_ = font_size
            else:
                max_ = font_size

        font = fonts[min_]
        return font

    return wrapped


fit_font = build_font_fitter()  # pylint: disable=invalid-name
"""Returns a font that best fits text to a box."""


@dataclass
class Box:
    """Utility class for modeling a textbox."""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self):
        self.x0, self.x1 = sorted((self.x0, self.x1))
        self.y0, self.y1 = sorted((self.y0, self.y1))

    @property
    def width(self) -> int:
        """Width of the textbox."""
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        """Height of the textbox."""
        return self.y1 - self.y0

    @property
    def size(self) -> tuple[int, int]:
        """Size of the text box as (width, height)."""
        return self.width, self.height


def add_text(draw: ImageDraw.ImageDraw, box: Box, text: typing.Any) -> None:
    """Add text to a box with a fitted font."""
    text = str(text)

    box_w, box_h = box.size

    font = fit_font(box.size, text)
    text_x0, text_y0, text_x1, text_y1 = font.getbbox(text)
    text_w = text_x1 - text_x0
    text_h = text_y1 - text_y0

    x0 = box.x0 + (box_w - text_w + 1) // 2 - text_x0
    y0 = box.y0 + (box_h - text_h + 1) // 2 - text_y0

    draw.text((x0, y0), text, font=font)


def get_inmate_name(inmate: typing.Any) -> str:
    """Return the inmate's full name, or a placeholder if unavailable."""
    if inmate.first_name is None or inmate.last_name is None:
        return "N/A"
    return " ".join([inmate.first_name, inmate.last_name])


def get_unit_name(unit: typing.Any) -> str:
    """Return the unit's name, or a placeholder if unavailable."""
    return unit.name if unit is not None else "N/A"


def get_shipping_method(unit: typing.Any) -> str:
    """Return the unit's shipping method, or a placeholder if unavailable."""
    if unit is None or unit.shipping_method is None:
        return "N/A"
    return unit.shipping_method


def render_request_label(
    request: Request, size: tuple[int, int] = (1004, 378)
) -> Image.Image:
    """Render a request label image.

    Default size is 1004x378 pixels for 85mm x 32mm labels at 300 DPI.
    """
    width, height = size

    image = Image.new("L", size, color=(255,))
    draw = ImageDraw.Draw(image)

    jurisdiction_code = "TEX" if request.inmate_jurisdiction == "Texas" else "FED"
    id_ = f"{jurisdiction_code}-{request.inmate_id}-{request.index}"

    def build_box_from_percentages(x0: int, y0: int, x1: int, y1: int):
        return Box(
            (x0 * width + 50) // 100,
            (y0 * height + 50) // 100,
            (x1 * width + 50) // 100,
            (y1 * height + 50) // 100,
        )

    # package ID barcode (full width)
    box = build_box_from_percentages(5, 5, 95, 38)
    image.paste(code128(id_, box.size), (box.x0, box.y0))

    box = build_box_from_percentages(5, 38, 95, 48)
    add_text(draw, box, f"PACKAGE ID: {id_}".upper())

    # inmate name and ID
    box = build_box_from_percentages(5, 50, 95, 75)
    name_line = f"{get_inmate_name(request.inmate)} #{request.inmate_id}"
    add_text(draw, box, name_line.upper())

    # jurisdiction, unit, shipping
    unit = request.inmate.unit
    details = (
        f"{request.inmate.jurisdiction} — "
        f"{get_unit_name(unit)} — "
        f"{get_shipping_method(unit)}"
    ).upper()

    box = build_box_from_percentages(5, 77, 95, 95)
    add_text(draw, box, details)

    return image
