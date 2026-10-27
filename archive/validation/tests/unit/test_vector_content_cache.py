from dataclasses import replace
from uuid import uuid4

from easypicture.domain import AffineTransform, FloatRect, SvgObject, TextObject
from easypicture.render.content_cache import ContentCache
from easypicture.render.svg_renderer import cached_svg_renderer, svg_content_cache
from easypicture.render.text_renderer import cached_text_layout, text_content_cache


def test_content_budget_evicts_and_does_not_retain_oversized_values():
    cache = ContentCache(byte_budget=10, entry_budget=2)
    ids = [uuid4() for _ in range(4)]
    first = cache.get(ids[0], ["content"], 4, object)
    assert cache.get(ids[0], ["content"], 4, object) is first
    cache.get(ids[1], "second", 4, object)
    cache.get(ids[2], "third", 4, object)
    assert cache.statistics.entries == 2
    assert cache.statistics.evictions == 1
    cache.get(ids[3], "too large", 11, object)
    assert cache.statistics.entries == 2
    assert cache.statistics.estimated_bytes == 8
    cache.discard(ids)
    assert cache.statistics.estimated_bytes == 0


def test_svg_transform_and_metadata_reuse_parse_but_preview_style_does_not(qapp):
    element = SvgObject(
        uuid4(),
        "Rectangle",
        "rect",
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
        '<rect width="16" height="16" fill="red"/></svg>',
        FloatRect(0, 0, 16, 16),
    )
    cache = svg_content_cache()
    cache.clear()
    parsed = cached_svg_renderer(element)
    assert parsed.isValid()
    moved = replace(
        element, name="renamed", transform=AffineTransform.translation(3, 4), opacity=0.5
    )
    assert cached_svg_renderer(moved) is parsed
    styled = replace(moved, fill_override=(0, 255, 0, 255))
    assert cached_svg_renderer(styled) is not parsed
    assert cache.statistics.entries == 1


def test_text_transform_color_and_frame_position_do_not_relayout(qapp):
    text = TextObject.create(FloatRect(0, 0, 100, 60), text="first\nsecond")
    cache = text_content_cache()
    cache.clear()
    layout = cached_text_layout(text)
    changed = replace(
        text,
        name="renamed",
        color=(255, 0, 0, 255),
        opacity=0.3,
        frame=FloatRect(50, 60, 100, 80),
        transform=AffineTransform.rotation(20),
    )
    assert cached_text_layout(changed) is layout
    assert cached_text_layout(replace(changed, text="different")) is not layout
    assert cache.statistics.entries == 1
