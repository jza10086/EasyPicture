from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from easypicture.domain import (
    Size,
    SvgCircleGeometry,
    SvgEllipseGeometry,
    SvgFillRule,
    SvgLineCap,
    SvgLineGeometry,
    SvgLineJoin,
    SvgPolygonGeometry,
    SvgPolylineGeometry,
    SvgPrimitiveKind,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    create_svg_primitive,
    parse_editable_svg,
    update_editable_svg,
)


@pytest.mark.parametrize(
    ("kind", "geometry"),
    (
        (SvgPrimitiveKind.RECT, SvgRectGeometry(1.0, 2.0, 30.0, 20.0, 4.0, 5.0)),
        (SvgPrimitiveKind.CIRCLE, SvgCircleGeometry(20.0, 21.0, 10.0)),
        (SvgPrimitiveKind.ELLIPSE, SvgEllipseGeometry(20.0, 21.0, 10.0, 8.0)),
        (SvgPrimitiveKind.LINE, SvgLineGeometry(1.0, 2.0, 30.0, 20.0)),
        (
            SvgPrimitiveKind.POLYLINE,
            SvgPolylineGeometry(((1.0, 2.0), (10.0, 4.0), (20.0, 12.0))),
        ),
        (
            SvgPrimitiveKind.POLYGON,
            SvgPolygonGeometry(((1.0, 2.0), (20.0, 2.0), (10.0, 12.0))),
        ),
    ),
)
def test_create_parse_and_update_basic_svg_primitive(kind, geometry) -> None:
    style = SvgPrimitiveStyle(
        fill=(12, 34, 56, 128),
        stroke=(200, 180, 160, 255),
        stroke_width=3.5,
        line_cap=SvgLineCap.ROUND,
        line_join=SvgLineJoin.BEVEL,
        miter_limit=6.0,
        dash_array=(4.0, 2.0),
        dash_offset=1.0,
        fill_rule=SvgFillRule.EVENODD,
        opacity=0.75,
    )
    vector = create_svg_primitive(kind, geometry, style, Size(128, 96))
    parsed = parse_editable_svg(vector.svg)
    assert parsed is not None
    assert parsed.kind is kind
    assert parsed.geometry == geometry
    assert parsed.style == style
    assert vector.intrinsic_bounds.width > 0.0
    assert vector.intrinsic_bounds.height > 0.0

    updated = update_editable_svg(vector.svg, geometry, style)
    assert parse_editable_svg(updated) == parsed


def test_update_preserves_defs_and_resource_fill() -> None:
    source = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
    <defs><linearGradient id="g"><stop stop-color="#f00"/></linearGradient></defs>
    <rect id="box" x="4" y="5" width="20" height="10" fill="url(#g)"/>
    </svg>"""
    parsed = parse_editable_svg(source)
    assert parsed is not None
    assert parsed.fill_resource == "url(#g)"
    updated = update_editable_svg(
        source,
        SvgRectGeometry(8.0, 9.0, 30.0, 20.0),
        parsed.style,
    )
    assert "linearGradient" in updated
    assert "url(#g)" in updated
    assert parse_editable_svg(updated).geometry == SvgRectGeometry(8.0, 9.0, 30.0, 20.0)  # type: ignore[union-attr]


def test_parser_requires_exactly_one_editable_visible_leaf() -> None:
    assert parse_editable_svg("<svg><rect width='2' height='2'/><circle r='1'/></svg>") is None
    assert parse_editable_svg("<svg><path d='M0 0L1 1'/></svg>") is None


def test_polygon_and_dash_validation() -> None:
    with pytest.raises(ValueError, match="area"):
        SvgPolygonGeometry(((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)))
    with pytest.raises(ValueError, match="positive"):
        SvgPrimitiveStyle(dash_array=(0.0, 0.0))


def test_standard_output_has_no_private_metadata() -> None:
    vector = create_svg_primitive(
        SvgPrimitiveKind.LINE,
        SvgLineGeometry(0.0, 0.0, 10.0, 0.0),
        SvgPrimitiveStyle(fill=None, stroke=(0, 0, 0, 255), stroke_width=2.0),
        Size(16, 16),
    )
    root = ET.fromstring(vector.svg)
    assert root.tag.endswith("svg")
    assert "EasyPicture" not in vector.svg
    assert "easypicture" not in vector.svg.lower()


def test_parser_resolves_simple_css_inheritance_and_current_color() -> None:
    source = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
    <style>rect.hot { color:#123456; fill:currentColor; stroke:#abcdef; }</style>
    <g opacity="0.5"><rect class="hot" x="2" y="3" width="20" height="10"
    style="stroke-width:3;fill-opacity:0.5"/></g></svg>"""
    parsed = parse_editable_svg(source)
    assert parsed is not None
    assert parsed.style.fill == (0x12, 0x34, 0x56, 128)
    assert parsed.style.stroke == (0xAB, 0xCD, 0xEF, 255)
    assert parsed.style.stroke_width == 3.0
    assert parsed.style.opacity == 0.5


def test_resource_paint_can_be_preserved_or_replaced_with_none() -> None:
    source = """<svg xmlns="http://www.w3.org/2000/svg"><defs>
    <linearGradient id="g"><stop stop-color="#fff"/></linearGradient></defs>
    <rect x="0" y="0" width="10" height="10" fill="url(#g)"/></svg>"""
    parsed = parse_editable_svg(source)
    assert parsed is not None
    preserved = update_editable_svg(source, parsed.geometry, parsed.style)
    assert 'fill="url(#g)"' in preserved
    cleared = update_editable_svg(
        source,
        parsed.geometry,
        parsed.style,
        preserve_fill_resource=False,
    )
    assert 'fill="none"' in cleared
