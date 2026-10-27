"""Static SVG export with embedded raster groups and optional vector flattening."""

from __future__ import annotations

import base64
import copy
import html
import os
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from easypicture.domain import AffineTransform
from easypicture.domain.effective import iter_effective_leaves
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.image_buffers import flush_file, temporary_sibling
from easypicture.persistence.image_export import (
    ExportFormat,
    ImageExportReceipt,
    ImageExportRequest,
    SvgExportOptions,
    SvgVectorExportMode,
    normalize_export_path,
)
from easypicture.persistence.snapshots import (
    LayerSnapshot,
    SvgElementSnapshot,
    TextObjectSnapshot,
)
from easypicture.pixels import Rect
from easypicture.render.raster_renderer import paint_raster_object
from easypicture.render.svg_renderer import create_static_svg_renderer, svg_with_overrides
from easypicture.render.text_renderer import paint_vector_objects

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_URL_REFERENCE = re.compile(r"url\(\s*#([^\s)]+)\s*\)")


def export_svg(
    request: ImageExportRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> ImageExportReceipt:
    if not isinstance(request.options, SvgExportOptions):
        raise TypeError("SVG export requires SvgExportOptions")
    destination = normalize_export_path(request.path, ExportFormat.SVG)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    snapshot = request.snapshot
    width = snapshot.size.width
    height = snapshot.size.height
    nodes = iter_effective_leaves(snapshot)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="{SVG_NAMESPACE}" '
            f'version="1.1" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
        ),
    ]
    parts.append(
        f'<defs><clipPath id="canvas-output"><rect width="{width}" height="{height}"/>'
        '</clipPath></defs><g clip-path="url(#canvas-output)">'
    )
    try:
        total = max(1, len(nodes))
        vector_serial = 0
        for index, item in enumerate(nodes):
            token.raise_if_cancelled()
            node = item.leaf
            if not item.visible or item.opacity <= 0.0:
                continue
            if not isinstance(node, (LayerSnapshot, TextObjectSnapshot, SvgElementSnapshot)):
                continue
            if isinstance(node, LayerSnapshot):
                pixels = node.pixels.read_region(Rect(0, 0, node.size.width, node.size.height))
                encoded = BytesIO()
                Image.fromarray(pixels).save(encoded, format="PNG")
                image = _embedded_png(encoded.getvalue(), node.size.width, node.size.height)
                parts.append(
                    _wrap_group(
                        image,
                        opacity=item.opacity,
                        transform=_matrix_transform(node.world_transform),
                    )
                )
            elif request.options.vector_mode is SvgVectorExportMode.RASTERIZE_VECTOR:
                png = _render_object_png(node, width, height, item.opacity)
                if png is not None:
                    parts.append(_embedded_png(png, width, height))
            else:
                vector_serial += 1
                if isinstance(node, TextObjectSnapshot):
                    parts.append(_svg_text(node, item.opacity, f"clip{vector_serial}"))
                else:
                    parts.append(
                        _svg_element(node, item.opacity, width, height, f"v{vector_serial}-")
                    )
            progress(index + 1, total + 1, "生成 SVG")
        parts.append("</g></svg>")
        token.raise_if_cancelled()
        data = "".join(parts).encode("utf-8")
        temporary.write_bytes(data)
        flush_file(temporary)
        progress(total, total + 1, "验证 SVG")
        _validate_svg(temporary, width, height)
        token.raise_if_cancelled()
        os.replace(temporary, destination)
        progress(total + 1, total + 1, "SVG 导出完成")
    finally:
        temporary.unlink(missing_ok=True)
    return ImageExportReceipt(
        destination,
        snapshot.document_id,
        snapshot.revision,
        ExportFormat.SVG,
        width,
        height,
        destination.stat().st_size,
    )


def _render_object_png(
    node: LayerSnapshot | TextObjectSnapshot | SvgElementSnapshot,
    width: int,
    height: int,
    opacity: float,
) -> bytes | None:
    image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    if isinstance(node, LayerSnapshot):
        paint_raster_object(painter, node, opacity=opacity)
    else:
        paint_vector_objects(painter, (node,), group_opacity=opacity / node.opacity)
    painter.end()
    return _qimage_png(image) if _has_visible_alpha(image) else None


def _embedded_png(data: bytes, width: int, height: int) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return (
        f'<image x="0" y="0" width="{width}" height="{height}" '
        f'href="data:image/png;base64,{encoded}"/>'
    )


def _svg_element(
    element: SvgElementSnapshot,
    opacity: float,
    width: int,
    height: int,
    resource_prefix: str,
) -> str:
    try:
        root = ET.fromstring(svg_with_overrides(element))
    except ET.ParseError as error:
        raise OSError("项目中的 SVG 元素无效") from error
    root = copy.deepcopy(root)
    _make_fragment_ids_unique(root, resource_prefix)
    viewport_transform = _viewbox_transform(root, width, height)
    children = list(root)
    for child in children:
        _strip_namespaces(child)
    fragment = "".join(ET.tostring(child, encoding="unicode") for child in children)
    if not fragment:
        return ""
    matrix = element.transform
    transform = _matrix_transform(matrix)
    inherited = _root_presentation_attributes(root)
    inner_attributes = "".join(
        f' {html.escape(name)}="{html.escape(value, quote=True)}"'
        for name, value in inherited.items()
    )
    if viewport_transform or inner_attributes:
        inner_transform = f' transform="{viewport_transform}"' if viewport_transform else ""
        fragment = f"<g{inner_transform}{inner_attributes}>{fragment}</g>"
    return _wrap_group(fragment, opacity=opacity, transform=transform)


def _make_fragment_ids_unique(root: ET.Element, prefix: str) -> None:
    replacements: dict[str, str] = {}
    for node in root.iter():
        old_id = node.get("id")
        if old_id:
            new_id = f"{prefix}{old_id}"
            replacements[old_id] = new_id
            node.set("id", new_id)
    if not replacements:
        return
    for node in root.iter():
        for name, value in tuple(node.attrib.items()):
            if value.startswith("#") and value[1:] in replacements:
                node.set(name, f"#{replacements[value[1:]]}")
                continue
            node.set(
                name,
                _URL_REFERENCE.sub(
                    lambda match: f"url(#{replacements.get(match.group(1), match.group(1))})",
                    value,
                ),
            )
        if node.tag.rsplit("}", 1)[-1] == "style" and node.text:
            for old_id, new_id in replacements.items():
                node.text = node.text.replace(f"url(#{old_id})", f"url(#{new_id})")


def _strip_namespaces(node: ET.Element) -> None:
    node.tag = node.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
    attributes: dict[str, str] = {}
    for name, value in node.attrib.items():
        local = name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        attributes["href" if local == "href" else local] = value
    node.attrib.clear()
    node.attrib.update(attributes)
    defaults = {
        "stroke-linecap": "butt",
        "stroke-linejoin": "miter",
        "stroke-miterlimit": "4",
        "fill-rule": "nonzero",
        "stroke-dashoffset": "0",
        "opacity": "1",
    }
    for name, default in defaults.items():
        if node.get(name) == default:
            node.attrib.pop(name, None)
    if node.get("transform", "").replace(" ", "") in {
        "matrix(1,0,0,1,0,0)",
        "matrix(100100)",
    }:
        node.attrib.pop("transform", None)
    for child in node:
        _strip_namespaces(child)


def _root_presentation_attributes(root: ET.Element) -> dict[str, str]:
    ignored = {
        "width",
        "height",
        "viewBox",
        "preserveAspectRatio",
        "x",
        "y",
        "version",
        "baseProfile",
        "id",
    }
    return {
        name.rsplit("}", 1)[-1]: value
        for name, value in root.attrib.items()
        if name.rsplit("}", 1)[-1] not in ignored
    }


def _viewbox_transform(root: ET.Element, width: int, height: int) -> str:
    values = root.get("viewBox", "").replace(",", " ").split()
    if len(values) != 4:
        return ""
    try:
        minimum_x, minimum_y, view_width, view_height = map(float, values)
    except ValueError:
        return ""
    if view_width <= 0.0 or view_height <= 0.0:
        return ""
    preserve = root.get("preserveAspectRatio", "xMidYMid meet").strip().split()
    if preserve and preserve[0] == "defer":
        preserve = preserve[1:]
    alignment = preserve[0] if preserve else "xMidYMid"
    mode = preserve[1] if len(preserve) > 1 else "meet"
    scale_x = width / view_width
    scale_y = height / view_height
    if alignment == "none":
        return (
            f"matrix({scale_x:.12g} 0 0 {scale_y:.12g} "
            f"{-minimum_x * scale_x:.12g} {-minimum_y * scale_y:.12g})"
        )
    scale = max(scale_x, scale_y) if mode == "slice" else min(scale_x, scale_y)
    remaining_x = width - view_width * scale
    remaining_y = height - view_height * scale
    offset_x = (
        0.0 if "xMin" in alignment else remaining_x if "xMax" in alignment else remaining_x / 2.0
    )
    offset_y = (
        0.0 if "YMin" in alignment else remaining_y if "YMax" in alignment else remaining_y / 2.0
    )
    translate_x = offset_x - minimum_x * scale
    translate_y = offset_y - minimum_y * scale
    if abs(scale - 1.0) < 1e-12 and abs(translate_x) < 1e-12 and abs(translate_y) < 1e-12:
        return ""
    return f"matrix({scale:.12g} 0 0 {scale:.12g} {translate_x:.12g} {translate_y:.12g})"


def _svg_text(text: TextObjectSnapshot, opacity: float, clip_id: str) -> str:
    frame = text.frame
    matrix = text.transform
    transform = _matrix_transform(matrix)
    fill = _color(text.color)
    stroke = _color(text.stroke_color) if text.stroke_width > 0.0 else "none"
    anchor = {
        "left": "start",
        "center": "middle",
        "right": "end",
    }[text.horizontal_alignment.value]
    x = {
        "left": frame.x,
        "center": frame.x + frame.width / 2.0,
        "right": frame.right,
    }[text.horizontal_alignment.value]
    line_height = text.font_size * text.line_spacing
    lines = text.text.split("\n") or [""]
    tspans = "".join(
        f'<tspan x="{x:.12g}" y="{frame.y + text.font_size + index * line_height:.12g}">'
        f"{html.escape(line)}</tspan>"
        for index, line in enumerate(lines)
    )
    content = (
        f'<defs><clipPath id="{clip_id}"><rect x="{frame.x:.12g}" y="{frame.y:.12g}" '
        f'width="{frame.width:.12g}" height="{frame.height:.12g}"/></clipPath></defs>'
        f'<text clip-path="url(#{clip_id})" font-family="{html.escape(text.font_family)}" '
        f'font-size="{text.font_size:.12g}" font-weight="{text.font_weight}" '
        f'font-style="{"italic" if text.italic else "normal"}" text-anchor="{anchor}" '
        f'fill="{fill}" fill-opacity="{text.color[3] / 255.0:.12g}" stroke="{stroke}" '
        f'stroke-width="{text.stroke_width * 2.0:.12g}">{tspans}</text>'
    )
    return _wrap_group(content, opacity=opacity, transform=transform)


def _matrix_transform(matrix: AffineTransform) -> str:
    a = matrix.a
    b = matrix.b
    c = matrix.c
    d = matrix.d
    tx = matrix.tx
    ty = matrix.ty
    if (
        abs(a - 1.0) < 1e-12
        and abs(b) < 1e-12
        and abs(c) < 1e-12
        and abs(d - 1.0) < 1e-12
        and abs(tx) < 1e-12
        and abs(ty) < 1e-12
    ):
        return ""
    return f"matrix({a:.12g} {b:.12g} {c:.12g} {d:.12g} {tx:.12g} {ty:.12g})"


def _wrap_group(content: str, *, opacity: float, transform: str) -> str:
    attributes: list[str] = []
    if opacity < 1.0 - 1e-12:
        attributes.append(f'opacity="{opacity:.12g}"')
    if transform:
        attributes.append(f'transform="{transform}"')
    if not attributes:
        return content
    return f"<g {' '.join(attributes)}>{content}</g>"


def _color(value: tuple[int, int, int, int]) -> str:
    return f"#{value[0]:02x}{value[1]:02x}{value[2]:02x}"


def _validate_svg(path: Path, width: int, height: int) -> None:
    data = path.read_bytes()
    lowered = data.lower()
    if any(marker in lowered for marker in (b"<script", b"javascript:", b"<!doctype")):
        raise OSError("导出的 SVG 包含不安全内容")
    renderer = create_static_svg_renderer(data)
    if not renderer.isValid():
        raise OSError("导出的 SVG 无法重新打开")
    size = renderer.defaultSize()
    if size.width() != width or size.height() != height:
        raise OSError("导出的 SVG 尺寸验证失败")
    with Image.open(BytesIO(_render_svg_png(renderer, width, height))) as check:
        if check.size != (width, height):
            raise OSError("导出的 SVG 渲染验证失败")


def _render_svg_png(renderer: QSvgRenderer, width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return _qimage_png(image)


def _has_visible_alpha(image: QImage) -> bool:
    straight = image.convertToFormat(QImage.Format.Format_RGBA8888)
    rows = np.frombuffer(
        straight.constBits(),
        dtype=np.uint8,
        count=straight.height() * straight.bytesPerLine(),
    ).reshape(straight.height(), straight.bytesPerLine())
    alpha = rows[:, : straight.width() * 4].reshape(
        straight.height(),
        straight.width(),
        4,
    )[..., 3]
    return bool(np.any(alpha))


def _qimage_png(image: QImage) -> bytes:
    straight = image.convertToFormat(QImage.Format.Format_RGBA8888)
    rows = np.frombuffer(
        straight.constBits(),
        dtype=np.uint8,
        count=straight.height() * straight.bytesPerLine(),
    ).reshape(straight.height(), straight.bytesPerLine())
    pixels = rows[:, : straight.width() * 4].reshape(
        straight.height(),
        straight.width(),
        4,
    )
    output = BytesIO()
    Image.fromarray(pixels, "RGBA").save(output, format="PNG")
    return output.getvalue()


__all__ = ["export_svg"]
