"""OpenGL texture-page backend for full-frame canvas painting."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from PySide6.QtCore import QRectF
from PySide6.QtGui import (
    QImage,
    QOpenGLContext,
    QPainter,
    QTransform,
    QVector3D,
    QVector4D,
)
from PySide6.QtOpenGL import (
    QOpenGLFunctions_3_3_Core,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)

from easypicture.domain import (
    ProjectiveTransform,
    RasterGroupLayer,
    RasterLayer,
    TransformMatrix,
    VectorGroupLayer,
    as_projective,
)
from easypicture.imaging import TransformInterpolation
from easypicture.pixels import DEFAULT_TILE_SIZE, TileCoord
from easypicture.render.canvas_frame import (
    CanvasRenderCoordinator,
    RenderBackendKind,
    TransformPreviewState,
)

GL_BLEND = 0x0BE2
GL_COLOR_BUFFER_BIT = 0x00004000
GL_CULL_FACE = 0x0B44
GL_DEPTH_TEST = 0x0B71
GL_RENDERER = 0x1F01
GL_ONE = 1
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_RGBA = 0x1908
GL_SCISSOR_TEST = 0x0C11
GL_STENCIL_TEST = 0x0B90
GL_TEXTURE0 = 0x84C0
GL_UNSIGNED_BYTE = 0x1401
GL_VENDOR = 0x1F00
GL_VERSION = 0x1F02
GL_LINE_LOOP = 0x0002
GL_TRIANGLE_FAN = 0x0006

PAGE_SIZE = 2048
TILES_PER_PAGE = PAGE_SIZE // DEFAULT_TILE_SIZE
DEFAULT_GPU_CACHE_BYTES = 512 * 1024 * 1024

_VERTEX_SHADER = """
#version 330 core
uniform vec3 u_transform_row0;
uniform vec3 u_transform_row1;
uniform vec3 u_transform_row2;
uniform vec3 u_view_row0;
uniform vec3 u_view_row1;
uniform vec3 u_view_row2;
uniform vec4 u_document_rect;
uniform vec4 u_uv_rect;

out vec2 v_uv;
out vec2 v_document_position;

void main() {
    const vec2 corners[4] = vec2[4](
        vec2(0.0, 0.0),
        vec2(1.0, 0.0),
        vec2(1.0, 1.0),
        vec2(0.0, 1.0)
    );
    vec2 corner = corners[gl_VertexID];
    vec2 position = u_document_rect.xy + corner * u_document_rect.zw;
    vec3 source = vec3(position, 1.0);
    vec3 mapped = vec3(
        dot(u_transform_row0, source),
        dot(u_transform_row1, source),
        dot(u_transform_row2, source)
    );
    vec3 clip = vec3(
        dot(u_view_row0, mapped),
        dot(u_view_row1, mapped),
        dot(u_view_row2, mapped)
    );
    gl_Position = vec4(clip.x, clip.y, 0.0, clip.z);
    v_uv = u_uv_rect.xy + corner * u_uv_rect.zw;
    v_document_position = mapped.xy / mapped.z;
}
"""

_FRAGMENT_SHADER = """
#version 330 core
in vec2 v_uv;
in vec2 v_document_position;
uniform sampler2D u_texture;
uniform vec4 u_layer_modulation;
uniform int u_mode;
uniform vec4 u_solid_color;
out vec4 frag_color;

void main() {
    if (u_mode == 1) {
        float parity = mod(
            floor(v_document_position.x / 16.0)
            + floor(v_document_position.y / 16.0),
            2.0
        );
        vec3 checker = mix(vec3(0.8118), vec3(0.9412), parity);
        frag_color = vec4(checker, 1.0);
    } else if (u_mode == 2) {
        frag_color = u_solid_color;
    } else {
        frag_color = texture(u_texture, v_uv) * u_layer_modulation;
    }
}
"""


type TileSignature = tuple[int, int, int, str]
type PageKey = tuple[UUID, int, int, str]


@dataclass(slots=True)
class _TexturePage:
    texture: QOpenGLTexture
    signatures: dict[TileCoord, TileSignature] = field(default_factory=dict)
    last_used_generation: int = 0


class OpenGLCanvasBackend:
    """Draw document texture pages with one complete framebuffer frame."""

    def __init__(self, *, cache_budget_bytes: int = DEFAULT_GPU_CACHE_BYTES) -> None:
        self.cache_budget_bytes = cache_budget_bytes
        self._functions: QOpenGLFunctions_3_3_Core | None = None
        self._program: QOpenGLShaderProgram | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._pages: OrderedDict[PageKey, _TexturePage] = OrderedDict()
        self._initialized_context: QOpenGLContext | None = None
        self._document_id: UUID | None = None
        self._capture_requested = False
        self._captured_frame: QImage | None = None
        self._transparent_tile = QImage(
            DEFAULT_TILE_SIZE,
            DEFAULT_TILE_SIZE,
            QImage.Format.Format_RGBA8888_Premultiplied,
        )
        self._transparent_tile.fill(0)

    @property
    def texture_bytes(self) -> int:
        return len(self._pages) * PAGE_SIZE * PAGE_SIZE * 4

    @property
    def is_initialized(self) -> bool:
        return self._program is not None

    def request_capture(self) -> None:
        self._capture_requested = True
        self._captured_frame = None

    def take_capture(self) -> QImage | None:
        image = self._captured_frame
        self._captured_frame = None
        return image

    def initialize(self, coordinator: CanvasRenderCoordinator) -> bool:
        context = QOpenGLContext.currentContext()
        if context is None or not context.isValid():
            return False
        if context is self._initialized_context and self._program is not None:
            return True
        self.release()
        functions = QOpenGLFunctions_3_3_Core()
        if not functions.initializeOpenGLFunctions():
            return False
        program = QOpenGLShaderProgram()
        if not program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Vertex,
            _VERTEX_SHADER,
        ):
            return False
        if not program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment,
            _FRAGMENT_SHADER,
        ):
            return False
        if not program.link():
            return False
        vao = QOpenGLVertexArrayObject()
        if not vao.create():
            return False
        vao.bind()
        program.bind()
        program.setUniformValue(program.uniformLocation(b"u_texture"), 0)
        program.release()
        vao.release()
        self._functions = functions
        self._program = program
        self._vao = vao
        self._initialized_context = context
        context.aboutToBeDestroyed.connect(self.release)
        coordinator.set_backend_diagnostics(
            RenderBackendKind.OPENGL,
            vendor=_gl_string(functions, GL_VENDOR),
            renderer=_gl_string(functions, GL_RENDERER),
            version=_gl_string(functions, GL_VERSION),
            texture_bytes=self.texture_bytes,
        )
        return True

    def paint_layers(
        self,
        painter: QPainter,
        coordinator: CanvasRenderCoordinator,
        viewport_width: int,
        viewport_height: int,
        device_pixel_ratio: float,
    ) -> bool:
        document = coordinator.document
        if not self.initialize(coordinator):
            return False
        device_transform = painter.deviceTransform()
        width_pixels = max(1.0, viewport_width * device_pixel_ratio)
        height_pixels = max(1.0, viewport_height * device_pixel_ratio)
        view_rows = self._view_rows(device_transform, width_pixels, height_pixels)
        native_active = False
        painter.beginNativePainting()
        native_active = True
        try:
            document_id = None if document is None else document.id
            if self._document_id != document_id:
                self._destroy_pages()
                self._document_id = document_id
            elif document is not None:
                self._purge_removed_layers({layer.id for layer in document.layers})
            functions = self._require_functions()
            program = self._require_program()
            vao = self._require_vao()
            functions.glDisable(GL_SCISSOR_TEST)
            functions.glDisable(GL_STENCIL_TEST)
            functions.glViewport(
                0,
                0,
                int(width_pixels),
                int(height_pixels),
            )
            functions.glColorMask(True, True, True, True)
            functions.glClearColor(0.2118, 0.2118, 0.2118, 1.0)
            functions.glClear(GL_COLOR_BUFFER_BIT)
            functions.glEnable(GL_BLEND)
            functions.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
            functions.glDisable(GL_DEPTH_TEST)
            functions.glDisable(GL_CULL_FACE)
            functions.glActiveTexture(GL_TEXTURE0)
            vao.bind()
            program.bind()

            def bind_frame_state() -> None:
                """Rebind state after a QPainter vector pass."""
                functions.glDisable(GL_SCISSOR_TEST)
                functions.glDisable(GL_STENCIL_TEST)
                functions.glViewport(0, 0, int(width_pixels), int(height_pixels))
                functions.glEnable(GL_BLEND)
                functions.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
                functions.glDisable(GL_DEPTH_TEST)
                functions.glDisable(GL_CULL_FACE)
                functions.glActiveTexture(GL_TEXTURE0)
                vao.bind()
                program.bind()
                program.setUniformValue(
                    program.uniformLocation(b"u_view_row0"), view_rows[0]
                )
                program.setUniformValue(
                    program.uniformLocation(b"u_view_row1"), view_rows[1]
                )
                program.setUniformValue(
                    program.uniformLocation(b"u_view_row2"), view_rows[2]
                )

            bind_frame_state()
            if document is not None:
                self._draw_checker_and_border(document.width, document.height, border=False)

                preview = coordinator.transform_preview
                if preview is not None:
                    try:
                        document.layer_by_id(preview.layer_id)
                    except ValueError:
                        coordinator.clear_transform_preview()
                        preview = None
                active_layer_id = None if preview is None else preview.layer_id
                for group in document.nodes:
                    if isinstance(group, RasterGroupLayer):
                        if not group.visible or group.opacity <= 0.0:
                            continue
                        for layer in group.children:
                            if layer.id == active_layer_id and preview is not None:
                                if document.selection.is_active:
                                    self._draw_layer(
                                        coordinator,
                                        layer,
                                        kind="base",
                                        transform=None,
                                        clip_to_document=True,
                                        opacity_multiplier=group.opacity,
                                    )
                                self._draw_layer(
                                    coordinator,
                                    layer,
                                    kind=(
                                        "floating"
                                        if document.selection.is_active
                                        else "source"
                                    ),
                                    transform=preview,
                                    clip_to_document=False,
                                    opacity_multiplier=group.opacity,
                                )
                            else:
                                self._draw_layer(
                                    coordinator,
                                    layer,
                                    kind="source",
                                    transform=None,
                                    clip_to_document=True,
                                    opacity_multiplier=group.opacity,
                                )
                    elif isinstance(group, VectorGroupLayer):
                        # QPainter owns vector text.  End the native pass for
                        # this group, then restore all GL state before the
                        # next raster group so root order remains authoritative.
                        program.release()
                        vao.release()
                        painter.endNativePainting()
                        native_active = False
                        coordinator.paint_vector_group(
                            painter,
                            group,
                            coordinator.document_rect(),
                        )
                        painter.beginNativePainting()
                        native_active = True
                        bind_frame_state()
                self._draw_checker_and_border(
                    document.width,
                    document.height,
                    border=True,
                )
            if self._capture_requested:
                self._captured_frame = self._read_framebuffer(
                    int(width_pixels),
                    int(height_pixels),
                )
                self._capture_requested = False
            program.release()
            vao.release()
            functions.glDisable(GL_BLEND)
        finally:
            if native_active:
                painter.endNativePainting()
        self._enforce_budget(coordinator.generation)
        coordinator.set_backend_diagnostics(
            RenderBackendKind.OPENGL,
            vendor=coordinator.diagnostics.vendor,
            renderer=coordinator.diagnostics.renderer,
            version=coordinator.diagnostics.version,
            texture_bytes=self.texture_bytes,
        )
        return True

    def _read_framebuffer(self, width: int, height: int) -> QImage:
        byte_count = width * height * 4
        storage = bytearray(byte_count)
        self._require_functions().glReadPixels(
            0,
            0,
            width,
            height,
            GL_RGBA,
            GL_UNSIGNED_BYTE,
            cast(int, storage),
        )
        image = QImage(
            bytes(storage),
            width,
            height,
            width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
        return image.mirrored(False, True)

    def release(self) -> None:
        context = QOpenGLContext.currentContext()
        if self._initialized_context is not None and context is not self._initialized_context:
            self._pages.clear()
            self._program = None
            self._vao = None
            self._functions = None
            self._initialized_context = None
            return
        self._destroy_pages()
        if self._vao is not None and self._vao.isCreated():
            self._vao.destroy()
        self._program = None
        self._vao = None
        self._functions = None
        self._initialized_context = None

    def _draw_layer(
        self,
        coordinator: CanvasRenderCoordinator,
        layer: RasterLayer,
        *,
        kind: str,
        transform: TransformPreviewState | None,
        clip_to_document: bool,
        opacity_multiplier: float = 1.0,
    ) -> None:
        if not layer.visible or layer.opacity <= 0.0:
            return
        document = coordinator.document
        if document is None:
            return
        coords = coordinator.display_tile_coords(layer)
        pages = sorted({(coord.x // TILES_PER_PAGE, coord.y // TILES_PER_PAGE) for coord in coords})
        matrix: TransformMatrix | None = None if transform is None else transform.matrix
        transform_rows = self._transform_rows(matrix)
        program = self._require_program()
        for page_x, page_y in pages:
            key = (layer.id, page_x, page_y, kind)
            page = self._sync_page(coordinator, layer, key, coords)
            page.last_used_generation = coordinator.generation
            self._pages.move_to_end(key)
            program.bind()
            program.setUniformValue(program.uniformLocation(b"u_mode"), 0)
            program.setUniformValue(
                program.uniformLocation(b"u_transform_row0"),
                transform_rows[0],
            )
            program.setUniformValue(
                program.uniformLocation(b"u_transform_row1"),
                transform_rows[1],
            )
            program.setUniformValue(
                program.uniformLocation(b"u_transform_row2"),
                transform_rows[2],
            )
            program.setUniformValue(
                program.uniformLocation(b"u_layer_modulation"),
                QVector4D(
                    float(layer.opacity * opacity_multiplier),
                    float(layer.opacity * opacity_multiplier),
                    float(layer.opacity * opacity_multiplier),
                    float(layer.opacity * opacity_multiplier),
                ),
            )
            rect = self._page_document_rect(layer, page_x, page_y)
            source_rect = QRectF(0.0, 0.0, rect.width(), rect.height())
            if clip_to_document:
                clipped = rect.intersected(
                    QRectF(0.0, 0.0, float(document.width), float(document.height))
                )
                if clipped.isEmpty():
                    continue
                source_rect = QRectF(
                    clipped.left() - rect.left(),
                    clipped.top() - rect.top(),
                    clipped.width(),
                    clipped.height(),
                )
                rect = clipped
            nearest = transform is None or (
                transform.interpolation is TransformInterpolation.NEAREST
            )
            page.texture.setMinificationFilter(
                QOpenGLTexture.Filter.Nearest
                if nearest
                else QOpenGLTexture.Filter.Linear
            )
            page.texture.setMagnificationFilter(
                QOpenGLTexture.Filter.Nearest
                if nearest
                else QOpenGLTexture.Filter.Linear
            )
            page.texture.bind(0)
            self._draw_quad(rect, source_rect)
            page.texture.release(0)

    def _draw_checker_and_border(
        self,
        width: int,
        height: int,
        *,
        border: bool,
    ) -> None:
        program = self._require_program()
        identity_rows = self._transform_rows(None)
        program.setUniformValue(
            program.uniformLocation(b"u_transform_row0"),
            identity_rows[0],
        )
        program.setUniformValue(
            program.uniformLocation(b"u_transform_row1"),
            identity_rows[1],
        )
        program.setUniformValue(
            program.uniformLocation(b"u_transform_row2"),
            identity_rows[2],
        )
        rect = QRectF(0.0, 0.0, float(width), float(height))
        if not border:
            program.setUniformValue(program.uniformLocation(b"u_mode"), 1)
            self._draw_quad(rect, rect)
            return
        program.setUniformValue(program.uniformLocation(b"u_mode"), 2)
        program.setUniformValue(
            program.uniformLocation(b"u_solid_color"),
            QVector4D(0.4392, 0.4392, 0.4392, 1.0),
        )
        self._set_quad_uniforms(rect, rect)
        self._require_functions().glDrawArrays(GL_LINE_LOOP, 0, 4)

    def _sync_page(
        self,
        coordinator: CanvasRenderCoordinator,
        layer: RasterLayer,
        key: PageKey,
        layer_coords: frozenset[TileCoord],
    ) -> _TexturePage:
        page = self._pages.get(key)
        if page is None:
            texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            texture.setSize(PAGE_SIZE, PAGE_SIZE)
            texture.setMipLevels(1)
            texture.setFormat(QOpenGLTexture.TextureFormat.RGBA8_UNorm)
            texture.allocateStorage(
                QOpenGLTexture.PixelFormat.RGBA,
                QOpenGLTexture.PixelType.UInt8,
            )
            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            empty = QImage(
                PAGE_SIZE,
                PAGE_SIZE,
                QImage.Format.Format_RGBA8888_Premultiplied,
            )
            empty.fill(0)
            texture.setData(
                QOpenGLTexture.PixelFormat.RGBA,
                QOpenGLTexture.PixelType.UInt8,
                cast(int, empty.bits()),
            )
            page = _TexturePage(texture)
            self._pages[key] = page
        _layer_id, page_x, page_y, kind = key
        current_coords = {
            coord
            for coord in layer_coords
            if coord.x // TILES_PER_PAGE == page_x
            and coord.y // TILES_PER_PAGE == page_y
        }
        for coord in set(page.signatures) - current_coords:
            self._upload_tile(page.texture, coord, self._transparent_tile)
            del page.signatures[coord]
        for coord in current_coords:
            signature = coordinator.display_tile_signature(layer, coord, kind)
            if page.signatures.get(coord) == signature:
                continue
            self._upload_tile(
                page.texture,
                coord,
                coordinator.display_tile_image(layer, coord, kind),
            )
            page.signatures[coord] = signature
        return page

    @staticmethod
    def _upload_tile(
        texture: QOpenGLTexture,
        coord: TileCoord,
        image: QImage,
    ) -> None:
        x_offset = (coord.x % TILES_PER_PAGE) * DEFAULT_TILE_SIZE
        y_offset = (coord.y % TILES_PER_PAGE) * DEFAULT_TILE_SIZE
        texture.setData(
            x_offset,
            y_offset,
            0,
            DEFAULT_TILE_SIZE,
            DEFAULT_TILE_SIZE,
            1,
            QOpenGLTexture.PixelFormat.RGBA,
            QOpenGLTexture.PixelType.UInt8,
            cast(int, image.bits()),
        )

    def _draw_quad(self, document_rect: QRectF, source_rect: QRectF) -> None:
        self._set_quad_uniforms(document_rect, source_rect)
        self._require_functions().glDrawArrays(GL_TRIANGLE_FAN, 0, 4)

    def _set_quad_uniforms(
        self,
        document_rect: QRectF,
        source_rect: QRectF,
    ) -> None:
        program = self._require_program()
        program.setUniformValue(
            program.uniformLocation(b"u_document_rect"),
            QVector4D(
                float(document_rect.left()),
                float(document_rect.top()),
                float(document_rect.width()),
                float(document_rect.height()),
            ),
        )
        program.setUniformValue(
            program.uniformLocation(b"u_uv_rect"),
            QVector4D(
                float(source_rect.left() / PAGE_SIZE),
                float(source_rect.top() / PAGE_SIZE),
                float(source_rect.width() / PAGE_SIZE),
                float(source_rect.height() / PAGE_SIZE),
            ),
        )

    @staticmethod
    def _page_document_rect(
        layer: RasterLayer,
        page_x: int,
        page_y: int,
    ) -> QRectF:
        local_left = page_x * PAGE_SIZE
        local_top = page_y * PAGE_SIZE
        width = min(PAGE_SIZE, layer.size.width - local_left)
        height = min(PAGE_SIZE, layer.size.height - local_top)
        return QRectF(
            float(layer.origin.x + local_left),
            float(layer.origin.y + local_top),
            float(width),
            float(height),
        )

    @staticmethod
    def _view_rows(
        transform: QTransform,
        width_pixels: float,
        height_pixels: float,
    ) -> tuple[QVector3D, QVector3D, QVector3D]:
        return (
            QVector3D(
                2.0 * transform.m11() / width_pixels,
                2.0 * transform.m21() / width_pixels,
                2.0 * transform.dx() / width_pixels - 1.0,
            ),
            QVector3D(
                -2.0 * transform.m12() / height_pixels,
                -2.0 * transform.m22() / height_pixels,
                1.0 - 2.0 * transform.dy() / height_pixels,
            ),
            QVector3D(0.0, 0.0, 1.0),
        )

    @staticmethod
    def _transform_rows(
        transform: TransformMatrix | None,
    ) -> tuple[QVector3D, QVector3D, QVector3D]:
        matrix = as_projective(transform) if transform is not None else ProjectiveTransform()
        return (
            QVector3D(matrix.m11, matrix.m12, matrix.m13),
            QVector3D(matrix.m21, matrix.m22, matrix.m23),
            QVector3D(matrix.m31, matrix.m32, matrix.m33),
        )

    def _enforce_budget(self, current_generation: int) -> None:
        while self.texture_bytes > self.cache_budget_bytes and self._pages:
            evict_key = next(
                (
                    key
                    for key, page in self._pages.items()
                    if page.last_used_generation != current_generation
                ),
                None,
            )
            if evict_key is None:
                break
            page = self._pages.pop(evict_key)
            page.texture.destroy()

    def _destroy_pages(self) -> None:
        for page in self._pages.values():
            page.texture.destroy()
        self._pages.clear()

    def _purge_removed_layers(self, current_layer_ids: set[UUID]) -> None:
        for key in tuple(self._pages):
            if key[0] in current_layer_ids:
                continue
            page = self._pages.pop(key)
            page.texture.destroy()

    def _require_functions(self) -> QOpenGLFunctions_3_3_Core:
        if self._functions is None:
            raise RuntimeError("OpenGL functions are not initialized")
        return self._functions

    def _require_program(self) -> QOpenGLShaderProgram:
        if self._program is None:
            raise RuntimeError("OpenGL program is not initialized")
        return self._program

    def _require_vao(self) -> QOpenGLVertexArrayObject:
        if self._vao is None:
            raise RuntimeError("OpenGL vertex array is not initialized")
        return self._vao


def _gl_string(functions: QOpenGLFunctions_3_3_Core, name: int) -> str:
    return functions.glGetString(name)


__all__ = [
    "DEFAULT_GPU_CACHE_BYTES",
    "OpenGLCanvasBackend",
    "PAGE_SIZE",
    "TILES_PER_PAGE",
]
