"""Utilities for creating request labels."""

import typing
from dataclasses import dataclass

import barcode  # type: ignore
from barcode.writer import ImageWriter  # type: ignore
from PIL import Image, ImageDraw, ImageFont  # type: ignore

from .models import Request


def code39(text: typing.Any, size: tuple[int, int], dpi: int = 300) -> Image.Image:
    """Create a barcode image for given text within the provided size."""
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

    code = barcode.Code39(str(text), writer=writer, add_checksum=False)

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
        size_h, size_w = size

        min_, max_ = min_font, max_font
        while abs(max_ - min_) > 1:
            font_size = int(round((max_ - min_) / 2)) + min_

            font = fonts[font_size]

            left, top, right, bottom = font.getbbox(text)
            text_w = right - left
            text_h = bottom - top

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

    x0 = box.x0 + (box_w - text_w + 1) // 2
    y0 = box.y0 + (box_h - text_h + 1) // 2

    draw.text((x0, y0), text, font=font)


def render_request_label(
    request: Request, size: tuple[int, int] = (1300, 500)
) -> Image.Image:
    """Render a request label image."""
    width, height = size

    image = Image.new("L", size, color=(255,))
    draw = ImageDraw.Draw(image)

    id_ = f"{request.inmate_jurisdiction}-{request.inmate_id}-{request.index}"

    def build_box_from_percentages(x0: int, y0: int, x1: int, y1: int):
        return Box(
            (x0 * width + 50) // 100,
            (y0 * height + 50) // 100,
            (x1 * width + 50) // 100,
            (y1 * height + 50) // 100,
        )

    # package ID barcode
    box = build_box_from_percentages(1, 1, 99, 50)
    image.paste(code39(id_, box.size), (box.x0, box.y0))

    box = build_box_from_percentages(1, 50, 99, 60)
    add_text(draw, box, id_)

    # inmate name
    def get_inmate_name(inmate: typing.Any) -> str:
        if inmate.first_name is None or inmate.last_name is None:
            return "Name: N/A"
        return " ".join([inmate.first_name, inmate.last_name])

    box = build_box_from_percentages(1, 60, 99, 90)
    add_text(draw, box, get_inmate_name(request.inmate))

    # other info at bottom
    box = build_box_from_percentages(1, 90, 33, 98)
    add_text(draw, box, request.inmate.jurisdiction)

    def get_unit_name(unit: typing.Any) -> str:
        return unit.name if unit is not None else "Unit: N/A"

    box = build_box_from_percentages(33, 90, 67, 99)
    add_text(draw, box, get_unit_name(request.inmate.unit))

    def get_shipping_method(unit: typing.Any) -> str:
        return unit.shipping_method if unit is not None else "Shipping: N/A"

    box = build_box_from_percentages(67, 90, 99, 99)
    add_text(draw, box, get_shipping_method(request.inmate.unit))

    return image
