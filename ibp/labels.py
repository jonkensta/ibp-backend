"""Utilities for rendering request labels as images."""

from __future__ import annotations

import itertools
import typing

import barcode  # type: ignore
from barcode.writer import ImageWriter  # type: ignore
from PIL import Image, ImageDraw, ImageFont  # type: ignore

# pylint: disable=invalid-name


def get_next_available_index(indices: typing.Iterable[int]) -> int:
    """Get next available index from an iterable of indices."""
    used_indices = sorted(indices)
    enumerated = itertools.zip_longest(itertools.count(), used_indices)
    return next(
        index
        for index, used_index in enumerated
        if used_index is None or index != used_index
    )


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
            text_h, text_w = font.getsize(text)

            if text_h < size_h and text_w < size_w:
                min_ = font_size
            else:
                max_ = font_size

        font = fonts[min_]
        return font

    return wrapped


fit_font = build_font_fitter()  # pylint: disable=invalid-name
"""Returns a font that best fits text to a box."""


class Box:
    """Utility class for modelling a textbox."""

    def __init__(self, x0: float, y0: float, x1: float, y1: float):
        """Initialize our textbox."""
        x0 = int(round(x0))
        y0 = int(round(y0))
        x1 = int(round(x1))
        y1 = int(round(y1))

        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)

        self._x0 = x0
        self._y0 = y0
        self._x1 = x1
        self._y1 = y1

    @property
    def x0(self) -> int:
        """x-component of bottom-left point."""
        return self._x0

    @property
    def y0(self) -> int:
        """y-component of bottom-left point."""
        return self._y0

    @property
    def x1(self) -> int:
        """x-component of top-right point."""
        return self._x1

    @property
    def y1(self) -> int:
        """y-component of top-right point."""
        return self._y1

    @property
    def width(self) -> int:
        """Width of the textbox."""
        return self._x1 - self._x0

    @property
    def height(self) -> int:
        """Height of the textbox."""
        return self._y1 - self._y0

    @property
    def size(self) -> tuple[int, int]:
        """Size of the text box as (width, height)."""
        return self.width, self.height


def add_text(draw: ImageDraw.ImageDraw, box: Box, text: typing.Any) -> None:
    """Add text to a box with a fitted font."""
    text = str(text)

    font = fit_font(box.size, text)
    text_size = font.getsize(text)

    x0 = box.x0 + int((box.size[0] - text_size[0] + 1) / 2)
    y0 = box.y0 + int((box.size[1] - text_size[1] + 1) / 2)

    draw.text((x0, y0), text, font=font)


def render_request_label(
    request: typing.Any, size: tuple[int, int] = (1300, 500)
) -> Image.Image:
    """Render a request label image."""
    width, height = size

    image = Image.new("L", size, color=(255,))
    draw = ImageDraw.Draw(image)

    id_ = f"{request.inmate_jurisdiction}-{request.inmate_id}-{request.index}"

    # package ID barcode
    box = Box(0.01 * width, 0.01 * height, 0.99 * width, 0.50 * height)
    image.paste(code39(id_, box.size), (box.x0, box.y0))

    box = Box(0.01 * width, 0.50 * height, 0.99 * width, 0.60 * height)
    add_text(draw, box, id_)

    # inmate name
    def get_inmate_name(inmate: typing.Any) -> str:
        if inmate.first_name is None or inmate.last_name is None:
            return "Name: N/A"
        return " ".join([inmate.first_name, inmate.last_name])

    box = Box(0.01 * width, 0.60 * height, 0.99 * width, 0.90 * height)
    add_text(draw, box, get_inmate_name(request.inmate))

    # other info at bottom
    box = Box(0.01 * width, 0.90 * height, 0.33 * width, 0.98 * height)
    add_text(draw, box, request.inmate.jurisdiction)

    def get_unit_name(unit: typing.Any) -> str:
        return unit.name if unit is not None else "Unit: N/A"

    box = Box(0.33 * width, 0.90 * height, 0.67 * width, 0.99 * height)
    add_text(draw, box, get_unit_name(request.inmate.unit))

    def get_shipping_method(unit: typing.Any) -> str:
        return unit.shipping_method if unit is not None else "Shipping: N/A"

    box = Box(0.67 * width, 0.90 * height, 0.99 * width, 0.99 * height)
    add_text(draw, box, get_shipping_method(request.inmate.unit))

    return image
