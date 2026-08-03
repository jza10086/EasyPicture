"""Qt-free editable SVG primitive parsing and standard serialization.

The project deliberately keeps the SVG XML document as the persistent source
of truth.  The value objects in this module are transient editing views used by
the shape tool and property panel.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

from easypicture.domain.geometry import FloatRect, Size
from easypicture.domain.vector import RGBAColor, SvgElementObject

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_EDITABLE_TAGS = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon"})
_RESOURCE_CONTAINERS = frozenset(
    {"defs", "clipPath", "mask", "linearGradient", "radialGradient", "pattern"}
)
_STYLE_KEYS = frozenset(
    {
        "color",
        "fill",
        "fill-opacity",
        "fill-rule",
        "stroke",
        "stroke-opacity",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-dasharray",
        "stroke-dashoffset",
        "opacity",
    }
)
_INHERITED_STYLE_KEYS = _STYLE_KEYS - {"opacity"}
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_EPSILON = 1e-9
MAX_SVG_POINTS = 4096


class SvgPrimitiveKind(StrEnum):
    RECT = "rect"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    LINE = "line"
    POLYLINE = "polyline"
    POLYGON = "polygon"


class SvgLineCap(StrEnum):
    BUTT = "butt"
    ROUND = "round"
    SQUARE = "square"


class SvgLineJoin(StrEnum):
    MITER = "miter"
    ROUND = "round"
    BEVEL = "bevel"


class SvgFillRule(StrEnum):
    NONZERO = "nonzero"
    EVENODD = "evenodd"


@dataclass(frozen=True, slots=True)
class SvgRectGeometry:
    x: float
    y: float
    width: float
    height: float
    rx: float = 0.0
    ry: float = 0.0

    def __post_init__(self) -> None:
        _finite(self.x, self.y, self.width, self.height, self.rx, self.ry)
        if self.width <= 0.0 or self.height <= 0.0:
            raise ValueError("SVG rectangle dimensions must be positive")
        if self.rx < 0.0 or self.ry < 0.0:
            raise ValueError("SVG rectangle radii must be non-negative")
        if self.rx > self.width / 2.0 + _EPSILON or self.ry > self.height / 2.0 + _EPSILON:
            raise ValueError("SVG rectangle radii exceed half the rectangle size")


@dataclass(frozen=True, slots=True)
class SvgCircleGeometry:
    cx: float
    cy: float
    r: float

    def __post_init__(self) -> None:
        _finite(self.cx, self.cy, self.r)
        if self.r <= 0.0:
            raise ValueError("SVG circle radius must be positive")


@dataclass(frozen=True, slots=True)
class SvgEllipseGeometry:
    cx: float
    cy: float
    rx: float
    ry: float

    def __post_init__(self) -> None:
        _finite(self.cx, self.cy, self.rx, self.ry)
        if self.rx <= 0.0 or self.ry <= 0.0:
            raise ValueError("SVG ellipse radii must be positive")


@dataclass(frozen=True, slots=True)
class SvgLineGeometry:
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        _finite(self.x1, self.y1, self.x2, self.y2)
        if math.hypot(self.x2 - self.x1, self.y2 - self.y1) <= _EPSILON:
            raise ValueError("SVG line endpoints must be distinct")


@dataclass(frozen=True, slots=True)
class SvgPolylineGeometry:
    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        _validate_points(self.points, minimum=2, polygon=False)


@dataclass(frozen=True, slots=True)
class SvgPolygonGeometry:
    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        _validate_points(self.points, minimum=3, polygon=True)


type SvgPrimitiveGeometry = (
    SvgRectGeometry
    | SvgCircleGeometry
    | SvgEllipseGeometry
    | SvgLineGeometry
    | SvgPolylineGeometry
    | SvgPolygonGeometry
)


@dataclass(frozen=True, slots=True)
class SvgPrimitiveStyle:
    fill: RGBAColor | None = None
    stroke: RGBAColor | None = (0, 0, 0, 255)
    stroke_width: float = 1.0
    line_cap: SvgLineCap = SvgLineCap.BUTT
    line_join: SvgLineJoin = SvgLineJoin.MITER
    miter_limit: float = 4.0
    dash_array: tuple[float, ...] = ()
    dash_offset: float = 0.0
    fill_rule: SvgFillRule = SvgFillRule.NONZERO
    opacity: float = 1.0

    def __post_init__(self) -> None:
        for color in (self.fill, self.stroke):
            if color is not None and (
                len(color) != 4
                or any(not isinstance(channel, int) or not 0 <= channel <= 255 for channel in color)
            ):
                raise ValueError("SVG colors must contain four byte channels")
        _finite(
            self.stroke_width, self.miter_limit, self.dash_offset, self.opacity, *self.dash_array
        )
        if not 0.0 <= self.stroke_width <= 8192.0:
            raise ValueError("SVG stroke width is out of range")
        if self.miter_limit < 1.0:
            raise ValueError("SVG miter limit must be at least one")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("SVG opacity must be between zero and one")
        if any(value < 0.0 for value in self.dash_array):
            raise ValueError("SVG dash lengths must be non-negative")
        if self.dash_array and not any(value > 0.0 for value in self.dash_array):
            raise ValueError("SVG dash array must contain a positive length")


@dataclass(frozen=True, slots=True)
class EditableSvgPrimitive:
    kind: SvgPrimitiveKind
    geometry: SvgPrimitiveGeometry
    style: SvgPrimitiveStyle
    fill_resource: str | None = None
    stroke_resource: str | None = None


def parse_editable_svg(svg: str) -> EditableSvgPrimitive | None:
    """Return a transient editable view for a standalone basic SVG leaf."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return None
    target = _editable_leaf(root)
    if target is None:
        return None
    kind = SvgPrimitiveKind(_local_name(target.tag))
    try:
        geometry = _parse_geometry(kind, target)
        style_map = _computed_style(root, target)
        fill_text = style_map.get(
            "fill",
            "none" if kind in {SvgPrimitiveKind.LINE, SvgPrimitiveKind.POLYLINE} else "black",
        )
        stroke_text = style_map.get(
            "stroke",
            "black" if kind in {SvgPrimitiveKind.LINE, SvgPrimitiveKind.POLYLINE} else "none",
        )
        fill_resource = fill_text if fill_text.strip().lower().startswith("url(") else None
        stroke_resource = stroke_text if stroke_text.strip().lower().startswith("url(") else None
        opacity = _number(style_map.get("opacity"), 1.0)
        fill_opacity = _number(style_map.get("fill-opacity"), 1.0)
        stroke_opacity = _number(style_map.get("stroke-opacity"), 1.0)
        style = SvgPrimitiveStyle(
            fill=None if fill_resource else _parse_color(fill_text, fill_opacity),
            stroke=None if stroke_resource else _parse_color(stroke_text, stroke_opacity),
            stroke_width=max(0.0, _number(style_map.get("stroke-width"), 1.0)),
            line_cap=_enum_value(SvgLineCap, style_map.get("stroke-linecap"), SvgLineCap.BUTT),
            line_join=_enum_value(SvgLineJoin, style_map.get("stroke-linejoin"), SvgLineJoin.MITER),
            miter_limit=max(1.0, _number(style_map.get("stroke-miterlimit"), 4.0)),
            dash_array=_parse_dash_array(style_map.get("stroke-dasharray")),
            dash_offset=_number(style_map.get("stroke-dashoffset"), 0.0),
            fill_rule=_enum_value(SvgFillRule, style_map.get("fill-rule"), SvgFillRule.NONZERO),
            opacity=min(1.0, max(0.0, opacity)),
        )
    except TypeError, ValueError:
        return None
    return EditableSvgPrimitive(kind, geometry, style, fill_resource, stroke_resource)


def update_editable_svg(
    source_svg: str,
    geometry: SvgPrimitiveGeometry,
    style: SvgPrimitiveStyle,
    *,
    preserve_fill_resource: bool = True,
    preserve_stroke_resource: bool = True,
) -> str:
    """Update one editable leaf while retaining resources and safe siblings."""
    try:
        root = ET.fromstring(source_svg)
    except ET.ParseError as error:
        raise ValueError("SVG XML is invalid") from error
    target = _editable_leaf(root)
    if target is None:
        raise ValueError("SVG document does not contain one editable primitive")
    current = parse_editable_svg(source_svg)
    if current is None or _kind_for_geometry(geometry) is not current.kind:
        raise ValueError("SVG geometry kind does not match the source primitive")
    _write_geometry(target, geometry)
    _write_style(
        target,
        style,
        current,
        preserve_fill_resource=preserve_fill_resource,
        preserve_stroke_resource=preserve_stroke_resource,
    )
    return ET.tostring(root, encoding="unicode")


def create_svg_primitive(
    kind: SvgPrimitiveKind,
    geometry: SvgPrimitiveGeometry,
    style: SvgPrimitiveStyle,
    canvas_size: Size,
    *,
    name: str | None = None,
) -> SvgElementObject:
    if _kind_for_geometry(geometry) is not kind:
        raise ValueError("SVG geometry kind does not match the requested primitive")
    ET.register_namespace("", SVG_NAMESPACE)
    root = ET.Element(
        f"{{{SVG_NAMESPACE}}}svg",
        {
            "width": str(canvas_size.width),
            "height": str(canvas_size.height),
            "viewBox": f"0 0 {canvas_size.width} {canvas_size.height}",
            "version": "1.1",
        },
    )
    leaf = ET.SubElement(root, f"{{{SVG_NAMESPACE}}}{kind.value}", {"id": "primitive"})
    _write_geometry(leaf, geometry)
    _write_style(leaf, style, None)
    svg = ET.tostring(root, encoding="unicode")
    bounds = primitive_bounds(geometry, style)
    return SvgElementObject(
        id=uuid4(),
        name=(name or _default_name(kind)),
        element_kind=kind.value,
        svg=svg,
        intrinsic_bounds=bounds,
    )


def primitive_bounds(geometry: SvgPrimitiveGeometry, style: SvgPrimitiveStyle) -> FloatRect:
    left, top, right, bottom = _geometry_bounds(geometry)
    padding = style.stroke_width / 2.0 if style.stroke is not None else 0.0
    width = max(_EPSILON, right - left + padding * 2.0)
    height = max(_EPSILON, bottom - top + padding * 2.0)
    return FloatRect(left - padding, top - padding, width, height)


def with_geometry(
    primitive: EditableSvgPrimitive, geometry: SvgPrimitiveGeometry
) -> EditableSvgPrimitive:
    if _kind_for_geometry(geometry) is not primitive.kind:
        raise ValueError("SVG geometry kind mismatch")
    return replace(primitive, geometry=geometry)


def _editable_leaf(root: ET.Element) -> ET.Element | None:
    parent_map = {child: parent for parent in root.iter() for child in parent}
    candidates: list[ET.Element] = []
    for element in root.iter():
        if _local_name(element.tag) not in _EDITABLE_TAGS:
            continue
        ancestor = parent_map.get(element)
        blocked = False
        while ancestor is not None:
            if _local_name(ancestor.tag) in _RESOURCE_CONTAINERS:
                blocked = True
                break
            ancestor = parent_map.get(ancestor)
        if not blocked:
            candidates.append(element)
    return candidates[0] if len(candidates) == 1 else None


def _parse_geometry(kind: SvgPrimitiveKind, element: ET.Element) -> SvgPrimitiveGeometry:
    if kind is SvgPrimitiveKind.RECT:
        width = _required_number(element, "width")
        height = _required_number(element, "height")
        rx = _number(element.get("rx"), 0.0)
        ry = _number(element.get("ry"), rx)
        if element.get("rx") is None and element.get("ry") is not None:
            rx = ry
        return SvgRectGeometry(
            _number(element.get("x"), 0.0),
            _number(element.get("y"), 0.0),
            width,
            height,
            min(rx, width / 2.0),
            min(ry, height / 2.0),
        )
    if kind is SvgPrimitiveKind.CIRCLE:
        return SvgCircleGeometry(
            _number(element.get("cx"), 0.0),
            _number(element.get("cy"), 0.0),
            _required_number(element, "r"),
        )
    if kind is SvgPrimitiveKind.ELLIPSE:
        return SvgEllipseGeometry(
            _number(element.get("cx"), 0.0),
            _number(element.get("cy"), 0.0),
            _required_number(element, "rx"),
            _required_number(element, "ry"),
        )
    if kind is SvgPrimitiveKind.LINE:
        return SvgLineGeometry(
            _number(element.get("x1"), 0.0),
            _number(element.get("y1"), 0.0),
            _number(element.get("x2"), 0.0),
            _number(element.get("y2"), 0.0),
        )
    points = _parse_points(element.get("points", ""))
    return (
        SvgPolylineGeometry(points)
        if kind is SvgPrimitiveKind.POLYLINE
        else SvgPolygonGeometry(points)
    )


def _write_geometry(element: ET.Element, geometry: SvgPrimitiveGeometry) -> None:
    for name in (
        "x",
        "y",
        "width",
        "height",
        "rx",
        "ry",
        "cx",
        "cy",
        "r",
        "x1",
        "y1",
        "x2",
        "y2",
        "points",
    ):
        element.attrib.pop(name, None)
    if isinstance(geometry, SvgRectGeometry):
        values = {
            "x": geometry.x,
            "y": geometry.y,
            "width": geometry.width,
            "height": geometry.height,
        }
        if geometry.rx > 0.0 or geometry.ry > 0.0:
            values.update({"rx": geometry.rx, "ry": geometry.ry})
        _set_numbers(element, values)
    elif isinstance(geometry, SvgCircleGeometry):
        _set_numbers(element, {"cx": geometry.cx, "cy": geometry.cy, "r": geometry.r})
    elif isinstance(geometry, SvgEllipseGeometry):
        _set_numbers(
            element, {"cx": geometry.cx, "cy": geometry.cy, "rx": geometry.rx, "ry": geometry.ry}
        )
    elif isinstance(geometry, SvgLineGeometry):
        _set_numbers(
            element, {"x1": geometry.x1, "y1": geometry.y1, "x2": geometry.x2, "y2": geometry.y2}
        )
    elif isinstance(geometry, (SvgPolylineGeometry, SvgPolygonGeometry)):
        element.set(
            "points",
            " ".join(f"{_format_number(x)},{_format_number(y)}" for x, y in geometry.points),
        )
    else:  # pragma: no cover - exhaustive guard
        raise TypeError("unsupported SVG geometry")


def _write_style(
    element: ET.Element,
    style: SvgPrimitiveStyle,
    current: EditableSvgPrimitive | None,
    *,
    preserve_fill_resource: bool = True,
    preserve_stroke_resource: bool = True,
) -> None:
    element.attrib.pop("style", None)
    resource_fill = (
        current.fill_resource if current is not None and preserve_fill_resource else None
    )
    resource_stroke = (
        current.stroke_resource if current is not None and preserve_stroke_resource else None
    )
    _set_paint(element, "fill", style.fill, resource_fill)
    _set_paint(element, "stroke", style.stroke, resource_stroke)
    element.set("stroke-width", _format_number(style.stroke_width))
    element.set("stroke-linecap", style.line_cap.value)
    element.set("stroke-linejoin", style.line_join.value)
    element.set("stroke-miterlimit", _format_number(style.miter_limit))
    if style.dash_array:
        element.set(
            "stroke-dasharray", " ".join(_format_number(value) for value in style.dash_array)
        )
    else:
        element.attrib.pop("stroke-dasharray", None)
    if abs(style.dash_offset) > _EPSILON:
        element.set("stroke-dashoffset", _format_number(style.dash_offset))
    else:
        element.attrib.pop("stroke-dashoffset", None)
    element.set("fill-rule", style.fill_rule.value)
    if style.opacity < 1.0 - _EPSILON:
        element.set("opacity", _format_number(style.opacity))
    else:
        element.attrib.pop("opacity", None)


def _set_paint(
    element: ET.Element, name: str, color: RGBAColor | None, resource: str | None
) -> None:
    opacity_name = f"{name}-opacity"
    if color is None and resource is not None:
        element.set(name, resource)
        element.attrib.pop(opacity_name, None)
        return
    if color is None:
        element.set(name, "none")
        element.attrib.pop(opacity_name, None)
        return
    element.set(name, f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}")
    if color[3] < 255:
        element.set(opacity_name, _format_number(color[3] / 255.0))
    else:
        element.attrib.pop(opacity_name, None)


def _style_map(element: ET.Element) -> dict[str, str]:
    result = {key: value for key in _STYLE_KEYS if (value := element.get(key)) is not None}
    for declaration in element.get("style", "").split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        key = key.strip().lower()
        if key in _STYLE_KEYS:
            result[key] = value.strip()
    return result


def _computed_style(root: ET.Element, target: ET.Element) -> dict[str, str]:
    """Resolve the intentionally small, safe CSS subset supported by P9D."""
    parent_map = {child: parent for parent in root.iter() for child in parent}
    path: list[ET.Element] = []
    current: ET.Element | None = target
    while current is not None:
        path.append(current)
        current = parent_map.get(current)
    path.reverse()
    rules = _css_rules(root)
    inherited: dict[str, str] = {"color": "black"}
    opacity_product = 1.0
    for element in path:
        local = {key: value for key, value in inherited.items() if key in _INHERITED_STYLE_KEYS}
        local.update({key: value for key in _STYLE_KEYS if (value := element.get(key)) is not None})
        matches: list[tuple[tuple[int, int, int], int, dict[str, str]]] = []
        for selector, declarations, order in rules:
            specificity = _selector_specificity(selector, element)
            if specificity is not None:
                matches.append((specificity, order, declarations))
        for _specificity, _order, declarations in sorted(matches):
            local.update(declarations)
        local.update(_inline_style(element))
        for key, value in tuple(local.items()):
            if value.strip().lower() == "inherit":
                if key in inherited:
                    local[key] = inherited[key]
                else:
                    local.pop(key)
        color = local.get("color", inherited.get("color", "black"))
        for paint in ("fill", "stroke"):
            if local.get(paint, "").strip().lower() == "currentcolor":
                local[paint] = color
        opacity_product *= min(1.0, max(0.0, _number(local.get("opacity"), 1.0)))
        inherited = local
    inherited["opacity"] = _format_number(opacity_product)
    return inherited


def _inline_style(element: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in element.get("style", "").split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        key = key.strip().lower()
        if key in _STYLE_KEYS:
            result[key] = value.strip()
    return result


def _css_rules(root: ET.Element) -> list[tuple[str, dict[str, str], int]]:
    result: list[tuple[str, dict[str, str], int]] = []
    order = 0
    for node in root.iter():
        if _local_name(node.tag) != "style" or not node.text:
            continue
        css = re.sub(r"/\*.*?\*/", "", node.text, flags=re.DOTALL)
        for selector_list, body in _CSS_RULE.findall(css):
            declarations: dict[str, str] = {}
            for declaration in body.split(";"):
                if ":" not in declaration:
                    continue
                key, value = declaration.split(":", 1)
                key = key.strip().lower()
                if key in _STYLE_KEYS:
                    declarations[key] = value.strip()
            for selector in selector_list.split(","):
                normalized = selector.strip()
                if normalized and _simple_selector(normalized):
                    result.append((normalized, declarations, order))
                    order += 1
    return result


def _simple_selector(selector: str) -> bool:
    return (
        re.fullmatch(
            r"(?:[A-Za-z_][\w-]*)?(?:#[A-Za-z_][\w-]*)?(?:\.[A-Za-z_][\w-]*)?",
            selector,
        )
        is not None
    )


def _selector_specificity(
    selector: str,
    element: ET.Element,
) -> tuple[int, int, int] | None:
    if not _simple_selector(selector):
        return None
    tag_match = re.match(r"^[A-Za-z_][\w-]*", selector)
    tag = tag_match.group() if tag_match else None
    id_match = re.search(r"#([A-Za-z_][\w-]*)", selector)
    class_match = re.search(r"\.([A-Za-z_][\w-]*)", selector)
    if tag is not None and _local_name(element.tag) != tag:
        return None
    if id_match is not None and element.get("id") != id_match.group(1):
        return None
    classes = element.get("class", "").split()
    if class_match is not None and class_match.group(1) not in classes:
        return None
    return (1 if id_match else 0, 1 if class_match else 0, 1 if tag else 0)


def _parse_color(value: str, opacity: float) -> RGBAColor | None:
    text = value.strip().lower()
    if text in {"", "none"}:
        return None
    rgb: tuple[int, int, int] | None = None
    if text.startswith("#"):
        payload = text[1:]
        if len(payload) == 3:
            rgb = tuple(int(char * 2, 16) for char in payload)  # type: ignore[assignment]
        elif len(payload) in {6, 8}:
            rgb = (int(payload[0:2], 16), int(payload[2:4], 16), int(payload[4:6], 16))
            if len(payload) == 8:
                opacity *= int(payload[6:8], 16) / 255.0
    elif match := re.fullmatch(r"rgba?\(([^)]*)\)", text):
        components = [part.strip() for part in match.group(1).split(",")]
        if len(components) in {3, 4}:
            rgb = tuple(_color_component(part) for part in components[:3])  # type: ignore[assignment]
            if len(components) == 4:
                opacity *= _alpha_component(components[3])
    else:
        named = {
            "black": (0, 0, 0),
            "white": (255, 255, 255),
            "red": (255, 0, 0),
            "green": (0, 128, 0),
            "blue": (0, 0, 255),
            "transparent": (0, 0, 0),
        }
        rgb = named.get(text)
        if text == "transparent":
            opacity = 0.0
    if rgb is None:
        return None
    return (*rgb, round(min(1.0, max(0.0, opacity)) * 255))


def _parse_dash_array(value: str | None) -> tuple[float, ...]:
    if value is None or value.strip().lower() in {"", "none"}:
        return ()
    values = tuple(float(item) for item in _NUMBER.findall(value))
    if any(number < 0.0 for number in values) or not any(number > 0.0 for number in values):
        raise ValueError("invalid SVG dash array")
    return values


def _parse_points(value: str) -> tuple[tuple[float, float], ...]:
    numbers = tuple(float(item) for item in _NUMBER.findall(value))
    if len(numbers) % 2:
        raise ValueError("SVG point list must contain coordinate pairs")
    return tuple(zip(numbers[::2], numbers[1::2], strict=True))


def _geometry_bounds(geometry: SvgPrimitiveGeometry) -> tuple[float, float, float, float]:
    if isinstance(geometry, SvgRectGeometry):
        return geometry.x, geometry.y, geometry.x + geometry.width, geometry.y + geometry.height
    if isinstance(geometry, SvgCircleGeometry):
        return (
            geometry.cx - geometry.r,
            geometry.cy - geometry.r,
            geometry.cx + geometry.r,
            geometry.cy + geometry.r,
        )
    if isinstance(geometry, SvgEllipseGeometry):
        return (
            geometry.cx - geometry.rx,
            geometry.cy - geometry.ry,
            geometry.cx + geometry.rx,
            geometry.cy + geometry.ry,
        )
    if isinstance(geometry, SvgLineGeometry):
        return (
            min(geometry.x1, geometry.x2),
            min(geometry.y1, geometry.y2),
            max(geometry.x1, geometry.x2),
            max(geometry.y1, geometry.y2),
        )
    xs = tuple(point[0] for point in geometry.points)
    ys = tuple(point[1] for point in geometry.points)
    return min(xs), min(ys), max(xs), max(ys)


def _kind_for_geometry(geometry: SvgPrimitiveGeometry) -> SvgPrimitiveKind:
    mapping = {
        SvgRectGeometry: SvgPrimitiveKind.RECT,
        SvgCircleGeometry: SvgPrimitiveKind.CIRCLE,
        SvgEllipseGeometry: SvgPrimitiveKind.ELLIPSE,
        SvgLineGeometry: SvgPrimitiveKind.LINE,
        SvgPolylineGeometry: SvgPrimitiveKind.POLYLINE,
        SvgPolygonGeometry: SvgPrimitiveKind.POLYGON,
    }
    return mapping[type(geometry)]


def _validate_points(
    points: tuple[tuple[float, float], ...], *, minimum: int, polygon: bool
) -> None:
    if not minimum <= len(points) <= MAX_SVG_POINTS:
        raise ValueError(f"SVG point count must be between {minimum} and {MAX_SVG_POINTS}")
    _finite(*(value for point in points for value in point))
    if len(set(points)) < minimum:
        raise ValueError("SVG points must contain enough distinct coordinates")
    if polygon:
        area = (
            sum(
                left[0] * right[1] - right[0] * left[1]
                for left, right in zip(points, points[1:] + points[:1], strict=True)
            )
            / 2.0
        )
        if abs(area) <= _EPSILON:
            raise ValueError("SVG polygon area must be non-zero")


def _required_number(element: ET.Element, name: str) -> float:
    value = element.get(name)
    if value is None:
        raise ValueError(f"SVG primitive is missing {name}")
    return _number(value, math.nan)


def _number(value: str | None, default: float) -> float:
    if value is None:
        return default
    match = _NUMBER.match(value.strip())
    if match is None:
        raise ValueError("invalid SVG number")
    result = float(match.group())
    if not math.isfinite(result):
        raise ValueError("SVG number must be finite")
    return result


def _set_numbers(element: ET.Element, values: dict[str, float]) -> None:
    for name, value in values.items():
        element.set(name, _format_number(value))


def _format_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("SVG number must be finite")
    if abs(value) < 5e-13:
        value = 0.0
    return f"{value:.12g}"


def _color_component(value: str) -> int:
    if value.endswith("%"):
        return round(min(100.0, max(0.0, float(value[:-1]))) * 2.55)
    return round(min(255.0, max(0.0, float(value))))


def _alpha_component(value: str) -> float:
    if value.endswith("%"):
        return min(100.0, max(0.0, float(value[:-1]))) / 100.0
    return min(1.0, max(0.0, float(value)))


def _enum_value(enum_type: type[Any], value: str | None, default: Any) -> Any:
    try:
        return enum_type(value) if value is not None else default
    except ValueError:
        return default


def _finite(*values: float) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("SVG geometry and style values must be finite")


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _default_name(kind: SvgPrimitiveKind) -> str:
    return {
        SvgPrimitiveKind.RECT: "矩形",
        SvgPrimitiveKind.CIRCLE: "圆",
        SvgPrimitiveKind.ELLIPSE: "椭圆",
        SvgPrimitiveKind.LINE: "直线",
        SvgPrimitiveKind.POLYLINE: "折线",
        SvgPrimitiveKind.POLYGON: "多边形",
    }[kind]


__all__ = [
    "EditableSvgPrimitive",
    "MAX_SVG_POINTS",
    "SvgCircleGeometry",
    "SvgEllipseGeometry",
    "SvgFillRule",
    "SvgLineCap",
    "SvgLineGeometry",
    "SvgLineJoin",
    "SvgPolygonGeometry",
    "SvgPolylineGeometry",
    "SvgPrimitiveGeometry",
    "SvgPrimitiveKind",
    "SvgPrimitiveStyle",
    "SvgRectGeometry",
    "create_svg_primitive",
    "parse_editable_svg",
    "primitive_bounds",
    "update_editable_svg",
    "with_geometry",
]
