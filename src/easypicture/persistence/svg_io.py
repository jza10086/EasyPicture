"""Safe static-SVG import into independent vector child objects."""

from __future__ import annotations

import base64
import binascii
import copy
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from PySide6.QtCore import QByteArray
from PySide6.QtSvg import QSvgRenderer, QtSvg

from easypicture.domain import (
    Document,
    FloatRect,
    RasterGroupLayer,
    SvgElementObject,
    TextObject,
    VectorGroupLayer,
)
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SUPPORTED_LEAVES = frozenset(
    {
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "use",
        "image",
    }
)
UNSUPPORTED_ELEMENTS = frozenset(
    {
        "animate", "animateMotion", "animateTransform", "set", "script", "foreignObject",
        "audio", "video", "filter", "mask", "feBlend", "feColorMatrix",
        "feComponentTransfer", "feComposite", "feConvolveMatrix", "feDiffuseLighting",
        "feDisplacementMap", "feDropShadow", "feFlood", "feGaussianBlur", "feImage",
        "feMerge", "feMorphology", "feOffset", "feSpecularLighting", "feTile", "feTurbulence",
    }
)
MAX_SVG_SOURCE_BYTES = 32 * 1024 * 1024
MAX_EMBEDDED_PNG_BYTES = 64 * 1024 * 1024
MAX_VECTOR_CHILDREN = 256
_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_LENGTH_PATTERN = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(px|pt|pc|mm|cm|in)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SvgImportWarning:
    category: str
    element_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class SvgImportResult:
    document: Document
    imported_count: int
    skipped_count: int
    warnings: tuple[SvgImportWarning, ...]

    def warning_summary(self) -> str:
        if not self.warnings:
            return ""
        counts = Counter(item.category for item in self.warnings)
        details = "、".join(f"{name} {count} 项" for name, count in counts.items())
        return f"SVG 导入完成，但已跳过或调整：{details}。详细信息已写入日志。"


def import_svg(
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
) -> SvgImportResult:
    source_path = Path(path)
    token.raise_if_cancelled()
    raw = source_path.read_bytes()
    if len(raw) > MAX_SVG_SOURCE_BYTES:
        raise ValueError("SVG 文件超过 32 MiB 安全限制")
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("SVG 不允许包含 DTD 或实体声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError("SVG XML 无效") from error
    if _local_name(root.tag) != "svg":
        raise ValueError("文件根节点不是 SVG")

    width, height = _canvas_size(root)
    warnings: list[SvgImportWarning] = []
    _flatten_nested_svg(root, width, height, warnings)
    _sanitize_tree(root, warnings)
    leaves = _drawable_leaves(root)
    if len(leaves) > MAX_VECTOR_CHILDREN:
        warnings.append(
            SvgImportWarning(
                "数量限制",
                None,
                f"只导入前 {MAX_VECTOR_CHILDREN} 个矢量元素",
            )
        )
        leaves = leaves[:MAX_VECTOR_CHILDREN]

    objects: list[TextObject | SvgElementObject] = []
    total = max(1, len(leaves))
    for index, leaf in enumerate(leaves):
        token.raise_if_cancelled()
        element_id = leaf.get("id") or f"element-{index + 1}"
        kind = _local_name(leaf.tag)
        try:
            standalone, bounds = _standalone_leaf(root, leaf, width, height, element_id)
            if kind == "text":
                objects.append(_text_object_from_element(leaf, bounds, element_id))
            else:
                objects.append(
                    SvgElementObject(
                        id=uuid4(),
                        name=element_id,
                        element_kind=kind,
                        svg=standalone,
                        intrinsic_bounds=bounds,
                    )
                )
        except (TypeError, ValueError) as error:
            warnings.append(SvgImportWarning("不支持元素", element_id, str(error)))
        progress(index + 1, total, "导入 SVG 元素")

    if not objects:
        raise ValueError("SVG 中没有可导入的受支持静态元素")

    document = Document.create(width, height)
    raster_group = next(
        node for node in document.nodes if isinstance(node, RasterGroupLayer)
    )
    used_names = {
        item.name
        for node in document.nodes
        for item in (node, *node.children)
    }
    group_name = _unique_name(source_path.stem.strip() or "SVG", used_names)
    used_names.add(group_name)
    for vector_object in objects:
        vector_object.name = _unique_name(vector_object.name, used_names)
        used_names.add(vector_object.name)
    vector_group = VectorGroupLayer(
        id=uuid4(),
        name=group_name,
        children=objects,
    )
    imported = Document.restore(
        document_id=uuid4(),
        size=document.size,
        active_layer_id=document.active_layer_id,
        selection=document.selection,
        nodes=[raster_group, vector_group],
        source_name=source_path.name,
        modified=False,
    )
    return SvgImportResult(imported, len(objects), len(warnings), tuple(warnings))


def _canvas_size(root: ET.Element) -> tuple[int, int]:
    view_box = root.get("viewBox", "").replace(",", " ").split()
    view_width = view_height = None
    if len(view_box) == 4:
        try:
            view_width = float(view_box[2])
            view_height = float(view_box[3])
        except ValueError:
            pass
    width = _length_px(root.get("width"), view_width)
    height = _length_px(root.get("height"), view_height)
    if width is None or height is None:
        raise ValueError("SVG 缺少可用的 width/height 或 viewBox")
    rounded_width = math.ceil(width)
    rounded_height = math.ceil(height)
    if (
        not 1 <= rounded_width <= MAX_DOCUMENT_DIMENSION
        or not 1 <= rounded_height <= MAX_DOCUMENT_DIMENSION
    ):
        raise ValueError("SVG 画布尺寸超出 1～8192 像素限制")
    return rounded_width, rounded_height


def _length_px(value: str | None, fallback: float | None) -> float | None:
    if value is None or value.strip().endswith("%"):
        return fallback
    match = _LENGTH_PATTERN.match(value)
    if match is None:
        return fallback
    number = float(match.group(1))
    factor = {
        None: 1.0,
        "px": 1.0,
        "pt": 96.0 / 72.0,
        "pc": 16.0,
        "mm": 96.0 / 25.4,
        "cm": 96.0 / 2.54,
        "in": 96.0,
    }[match.group(2).lower() if match.group(2) else None]
    result = number * factor
    return result if math.isfinite(result) and result > 0.0 else fallback


def _sanitize_tree(root: ET.Element, warnings: list[SvgImportWarning]) -> None:
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for element in tuple(root.iter()):
        kind = _local_name(element.tag)
        element_id = element.get("id")
        if kind in UNSUPPORTED_ELEMENTS:
            parent = parent_map.get(element)
            if parent is not None:
                parent.remove(element)
            warnings.append(SvgImportWarning("不支持元素", element_id, f"跳过 <{kind}>") )
            continue
        skip_reason: str | None = None
        for attribute in tuple(element.attrib):
            local = _local_name(attribute).lower()
            value = element.attrib[attribute]
            if local.startswith("on"):
                del element.attrib[attribute]
                warnings.append(SvgImportWarning("脚本", element_id, f"移除事件属性 {local}"))
                continue
            if local in {"href", "xlink:href"} and not value.strip().startswith("#"):
                if kind == "image" and _is_safe_embedded_png(value):
                    if not _embedded_png_has_visible_alpha(value):
                        skip_reason = "跳过完全透明的内嵌 PNG"
                        break
                    continue
                if kind == "use":
                    skip_reason = "跳过引用外部资源的 <use>"
                    break
                skip_reason = f"跳过引用外部资源的 <{kind}>"
                break
            urls = [match.group(2).strip() for match in _URL_PATTERN.finditer(value)]
            if any(not item.startswith("#") for item in urls):
                skip_reason = f"跳过使用 {local} 外部资源的元素"
                break
            if local in {"filter", "mask"}:
                skip_reason = f"跳过使用 {local} 的元素"
                break
        if skip_reason is not None:
            parent = parent_map.get(element)
            if parent is not None:
                parent.remove(element)
            warnings.append(SvgImportWarning("不支持效果", element_id, skip_reason))
            continue
        if kind == "style" and element.text:
            css = element.text.lower()
            urls = [match.group(2).strip() for match in _URL_PATTERN.finditer(css)]
            if "@import" in css or "@keyframes" in css or any(
                not item.startswith("#") for item in urls
            ):
                parent = parent_map.get(element)
                if parent is not None:
                    parent.remove(element)
                warnings.append(
                    SvgImportWarning("不支持 CSS", element_id, "移除外部或动画样式表")
                )


def _is_safe_embedded_png(value: str) -> bool:
    marker = "data:image/png;base64,"
    stripped = value.strip()
    if not stripped.lower().startswith(marker):
        return False
    encoded = stripped[len(marker) :]
    if len(encoded) > (MAX_EMBEDDED_PNG_BYTES * 4 // 3) + 8:
        return False
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    if not payload or len(payload) > MAX_EMBEDDED_PNG_BYTES:
        return False
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG":
                return False
            width, height = image.size
            if (
                not 1 <= width <= MAX_DOCUMENT_DIMENSION
                or not 1 <= height <= MAX_DOCUMENT_DIMENSION
            ):
                return False
            image.verify()
    except (OSError, UnidentifiedImageError):
        return False
    return True


def _embedded_png_has_visible_alpha(value: str) -> bool:
    marker = "data:image/png;base64,"
    try:
        payload = base64.b64decode(value.strip()[len(marker) :], validate=True)
        with Image.open(BytesIO(payload)) as image:
            alpha = image.convert("RGBA").getchannel("A")
            maximum = alpha.getextrema()[1]
            return isinstance(maximum, (int, float)) and maximum > 0
    except (OSError, UnidentifiedImageError, binascii.Error, ValueError):
        return False


def _flatten_nested_svg(
    root: ET.Element,
    width: int,
    height: int,
    warnings: list[SvgImportWarning],
) -> None:
    converted = 0
    while True:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        nested = next(
            (
                element
                for element in root.iter()
                if element is not root and _local_name(element.tag) == "svg"
            ),
            None,
        )
        if nested is None:
            break
        parent = parent_map.get(nested)
        if parent is None:
            break
        namespace = nested.tag.split("}", 1)[0] + "}" if "}" in nested.tag else ""
        replacement = ET.Element(f"{namespace}g")
        ignored = {
            "width",
            "height",
            "viewBox",
            "preserveAspectRatio",
            "x",
            "y",
            "version",
            "baseProfile",
        }
        for name, value in nested.attrib.items():
            if _local_name(name) not in ignored:
                replacement.set(name, value)
        viewport_transform = _nested_viewport_transform(nested, width, height)
        existing_transform = replacement.get("transform", "").strip()
        combined = " ".join(
            item for item in (existing_transform, viewport_transform) if item
        )
        if combined:
            replacement.set("transform", combined)
        replacement.text = nested.text
        replacement.tail = nested.tail
        for child in list(nested):
            replacement.append(child)
        index = list(parent).index(nested)
        parent.remove(nested)
        parent.insert(index, replacement)
        converted += 1
    if converted:
        warnings.append(
            SvgImportWarning(
                "嵌套视口转换",
                None,
                f"已展开 {converted} 个嵌套 SVG 视口",
            )
        )


def _nested_viewport_transform(
    element: ET.Element,
    fallback_width: int,
    fallback_height: int,
) -> str:
    values = element.get("viewBox", "").replace(",", " ").split()
    if len(values) != 4:
        x = _coordinate(element.get("x"), 0.0)
        y = _coordinate(element.get("y"), 0.0)
        return "" if x == 0.0 and y == 0.0 else f"translate({x:.12g} {y:.12g})"
    try:
        minimum_x, minimum_y, view_width, view_height = map(float, values)
    except ValueError:
        return ""
    if view_width <= 0.0 or view_height <= 0.0:
        return ""
    viewport_width = _length_px(element.get("width"), float(fallback_width))
    viewport_height = _length_px(element.get("height"), float(fallback_height))
    if viewport_width is None or viewport_height is None:
        return ""
    x = _coordinate(element.get("x"), 0.0)
    y = _coordinate(element.get("y"), 0.0)
    preserve = element.get("preserveAspectRatio", "xMidYMid meet").strip().split()
    if preserve and preserve[0] == "defer":
        preserve = preserve[1:]
    alignment = preserve[0] if preserve else "xMidYMid"
    mode = preserve[1] if len(preserve) > 1 else "meet"
    scale_x = viewport_width / view_width
    scale_y = viewport_height / view_height
    if alignment == "none":
        return (
            f"matrix({scale_x:.12g} 0 0 {scale_y:.12g} "
            f"{x - minimum_x * scale_x:.12g} {y - minimum_y * scale_y:.12g})"
        )
    scale = max(scale_x, scale_y) if mode == "slice" else min(scale_x, scale_y)
    remaining_x = viewport_width - view_width * scale
    remaining_y = viewport_height - view_height * scale
    offset_x = (
        0.0
        if "xMin" in alignment
        else remaining_x
        if "xMax" in alignment
        else remaining_x / 2.0
    )
    offset_y = (
        0.0
        if "YMin" in alignment
        else remaining_y
        if "YMax" in alignment
        else remaining_y / 2.0
    )
    translate_x = x + offset_x - minimum_x * scale
    translate_y = y + offset_y - minimum_y * scale
    if (
        abs(scale - 1.0) < 1e-12
        and abs(translate_x) < 1e-12
        and abs(translate_y) < 1e-12
    ):
        return ""
    return (
        f"matrix({scale:.12g} 0 0 {scale:.12g} "
        f"{translate_x:.12g} {translate_y:.12g})"
    )


def _coordinate(value: str | None, fallback: float) -> float:
    if value is None:
        return fallback
    match = _LENGTH_PATTERN.match(value)
    if match is None:
        return fallback
    number = float(match.group(1))
    suffix = match.group(2).lower() if match.group(2) else None
    factor = {
        None: 1.0,
        "px": 1.0,
        "pt": 96.0 / 72.0,
        "pc": 16.0,
        "mm": 96.0 / 25.4,
        "cm": 96.0 / 2.54,
        "in": 96.0,
    }[suffix]
    result = number * factor
    return result if math.isfinite(result) else fallback


def _standalone_leaf(
    root: ET.Element,
    selected: ET.Element,
    width: int,
    height: int,
    element_id: str,
) -> tuple[str, FloatRect]:
    clone = copy.deepcopy(root)
    source_leaves = _drawable_leaves(root)
    clone_leaves = _drawable_leaves(clone)
    selected_index = source_leaves.index(selected)
    target = clone_leaves[selected_index]
    target.set("id", element_id)
    parent_map = {child: parent for parent in clone.iter() for child in parent}
    for index, leaf in enumerate(clone_leaves):
        if index == selected_index:
            continue
        parent = parent_map.get(leaf)
        if parent is not None:
            parent.remove(leaf)
    clone.set("width", str(width))
    clone.set("height", str(height))
    if not clone.get("viewBox"):
        clone.set("viewBox", f"0 0 {width} {height}")
    _normalize_clip_paths(clone, width, height)
    document = ET.tostring(clone, encoding="unicode")
    renderer = QSvgRenderer()
    renderer.setOptions(QtSvg.Option.DisableAnimations)
    renderer.load(QByteArray(document.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError("Qt 无法渲染该 SVG 元素")
    bounds = renderer.boundsOnElement(element_id)
    if bounds.isEmpty():
        raise ValueError("SVG 元素没有可见边界")
    mapped = renderer.transformForElement(element_id).mapRect(bounds)
    if mapped.isEmpty():
        mapped = bounds
    return document, FloatRect(mapped.x(), mapped.y(), mapped.width(), mapped.height())


def _text_object_from_element(
    element: ET.Element,
    bounds: FloatRect,
    name: str,
) -> TextObject:
    text = "".join(element.itertext()).strip() or "文本"
    style = _style_map(element)
    family = style.get("font-family", element.get("font-family", "Segoe UI")).strip(" '\"")
    size_value = style.get("font-size", element.get("font-size", "32"))
    size = _length_px(size_value, 32.0) or 32.0
    fill = _parse_color(style.get("fill", element.get("fill", "#ffffff")))
    return TextObject(
        id=uuid4(),
        name=name,
        text=text,
        frame=bounds,
        font_family=family or "Segoe UI",
        font_size=min(1024.0, max(1.0, size)),
        color=fill,
    )


def _style_map(element: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in element.get("style", "").split(";"):
        if ":" in declaration:
            key, value = declaration.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def _parse_color(value: str) -> tuple[int, int, int, int]:
    text = value.strip().lower()
    if text.startswith("#") and len(text) in {4, 7}:
        if len(text) == 4:
            return (
                int(text[1] * 2, 16),
                int(text[2] * 2, 16),
                int(text[3] * 2, 16),
                255,
            )
        return (
            int(text[1:3], 16),
            int(text[3:5], 16),
            int(text[5:7], 16),
            255,
        )
    basic = {
        "black": (0, 0, 0, 255),
        "white": (255, 255, 255, 255),
        "red": (255, 0, 0, 255),
        "green": (0, 128, 0, 255),
        "blue": (0, 0, 255, 255),
        "transparent": (0, 0, 0, 0),
    }
    return basic.get(text, (255, 255, 255, 255))


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _drawable_leaves(root: ET.Element) -> list[ET.Element]:
    parent_map = {child: parent for parent in root.iter() for child in parent}
    result: list[ET.Element] = []
    for element in root.iter():
        if _local_name(element.tag) not in SUPPORTED_LEAVES:
            continue
        ancestor = parent_map.get(element)
        excluded = False
        while ancestor is not None:
            if _local_name(ancestor.tag) in {
                "defs",
                "clipPath",
                "linearGradient",
                "radialGradient",
            }:
                excluded = True
                break
            ancestor = parent_map.get(ancestor)
        if not excluded:
            result.append(element)
    return result


def _normalize_clip_paths(root: ET.Element, width: int, height: int) -> None:
    """Translate basic clipPath resources to masks supported by QSvgRenderer."""
    replacements: dict[str, str] = {}
    for node in root.iter():
        if _local_name(node.tag) != "clipPath":
            continue
        clip_id = node.get("id")
        if not clip_id:
            continue
        mask_id = f"easypicture-mask-{clip_id}"
        replacements[clip_id] = mask_id
        namespace = node.tag.split("}", 1)[0] + "}" if "}" in node.tag else ""
        node.tag = f"{namespace}mask"
        node.set("id", mask_id)
        node.set("maskUnits", "userSpaceOnUse")
        node.set("x", "0")
        node.set("y", "0")
        node.set("width", str(width))
        node.set("height", str(height))
        for shape in node.iter():
            if shape is node:
                continue
            shape.set("fill", "white")
            shape.set("stroke", "white")
            shape.set("fill-opacity", "1")
            shape.set("stroke-opacity", "1")
    for node in root.iter():
        clip_value = node.attrib.pop("clip-path", None)
        if clip_value is None:
            continue
        match = re.fullmatch(r"url\(\s*#([^\s)]+)\s*\)", clip_value)
        if match and match.group(1) in replacements:
            node.set("mask", f"url(#{replacements[match.group(1)]})")


def _unique_name(requested: str, existing: set[str]) -> str:
    if requested not in existing:
        return requested
    suffix = 1
    while f"{requested}.{suffix}" in existing:
        suffix += 1
    return f"{requested}.{suffix}"


__all__ = ["SvgImportResult", "SvgImportWarning", "import_svg"]
