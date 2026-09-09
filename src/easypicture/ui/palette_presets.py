"""Local named palettes: QSettings only, never document metadata."""

from __future__ import annotations

import json
from typing import cast

from easypicture.imaging.color_adjustments import PaletteMapAdjustment, RGBColor
from easypicture.ui.settings import AppSettings


class PalettePresetStore:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def palettes(self) -> dict[str, tuple[RGBColor, ...]]:
        result: dict[str, tuple[RGBColor, ...]] = {}
        try:
            data = json.loads(self.settings.color_adjustment_state("palette_presets") or "{}")
            if not isinstance(data, dict):
                return result
            for name, raw in list(data.items())[:256]:
                try:
                    if not isinstance(name, str) or not name.strip() or len(name) > 256:
                        continue
                    colors = tuple(cast(RGBColor, tuple(color)) for color in raw)
                    PaletteMapAdjustment(colors)
                    result[name] = colors
                except TypeError, ValueError:
                    continue
        except ValueError, TypeError:
            pass
        return result

    def save(self, name: str, colors: tuple[RGBColor, ...], *, replacing: str | None = None) -> str:
        PaletteMapAdjustment(colors)
        name = name.strip()
        if not name or len(name) > 256:
            raise ValueError("调色板名称必须为 1～256 个字符")
        palettes = self.palettes()
        if replacing is not None:
            palettes.pop(replacing, None)
        if len(palettes) >= 256:
            raise ValueError("本机最多保存 256 个调色板预设")
        base, suffix = name, 1
        while name in palettes:
            name = f"{base}.{suffix}"
            suffix += 1
        palettes[name] = colors
        self._write(palettes)
        return name

    def delete(self, name: str) -> None:
        palettes = self.palettes()
        palettes.pop(name, None)
        self._write(palettes)

    def _write(self, palettes: dict[str, tuple[RGBColor, ...]]) -> None:
        self.settings.set_color_adjustment_state("palette_presets", json.dumps(palettes))
        self.settings.sync()
