"""Pure-Python replacements for the IMG4 operations used by build.sh."""

from __future__ import annotations

from pathlib import Path

from pyimg4 import Compression, IMG4, IM4M, IM4P, PayloadProperty


class Img4ToolError(RuntimeError):
    pass


def parse_im4p(blob: bytes) -> IM4P:
    try:
        return IM4P(blob)
    except Exception as im4p_error:
        try:
            container = IMG4(blob)
            if container.im4p is None:
                raise Img4ToolError("IMG4 container has no IM4P payload")
            return container.im4p
        except Exception as img4_error:
            raise Img4ToolError(
                f"Input is neither a valid IM4P nor IMG4 ({im4p_error}; {img4_error})"
            ) from img4_error


def extract_im4p(blob: bytes) -> bytes:
    image = parse_im4p(blob)
    if image.payload is None:
        raise Img4ToolError("IM4P has no payload")
    if image.payload.encrypted:
        raise Img4ToolError("Encrypted IM4P payloads are not supported without a keybag key")
    try:
        if image.payload.compression != Compression.NONE:
            image.payload.decompress()
    except Exception as exc:
        raise Img4ToolError(f"Could not decompress {image.fourcc}: {exc}") from exc
    return image.payload.data + (image.payload.extra or b"")


def extract_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(extract_im4p(source.read_bytes()))


def _ticket(path: Path) -> IM4M:
    try:
        return IM4M(path.read_bytes())
    except Exception as exc:
        raise Img4ToolError(f"Invalid IM4M ticket {path}: {exc}") from exc


def wrap_existing_im4p(source: Path, destination: Path, ticket_path: Path, fourcc: str | None = None) -> None:
    try:
        image = parse_im4p(source.read_bytes())
        if fourcc:
            image.fourcc = fourcc
        output = IMG4(im4p=image, im4m=_ticket(ticket_path)).output()
    except Exception as exc:
        if isinstance(exc, Img4ToolError):
            raise
        raise Img4ToolError(f"Could not wrap {source.name}: {exc}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(output)


def wrap_raw(
    data: bytes,
    destination: Path,
    ticket_path: Path,
    *,
    fourcc: str,
    description: str = "",
    compress: bool = False,
    properties: tuple[PayloadProperty, ...] = (),
) -> None:
    try:
        image = IM4P(fourcc=fourcc, description=description, payload=data)
        for prop in properties:
            image.add_property(PayloadProperty(fourcc=prop.fourcc, value=prop.value))
        if compress:
            image.payload.compress(Compression.LZFSE)
        output = IMG4(im4p=image, im4m=_ticket(ticket_path)).output()
    except Exception as exc:
        raise Img4ToolError(f"Could not create {fourcc} IMG4: {exc}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(output)


def wrap_kernel(raw_path: Path, original_im4p: Path, destination: Path, ticket_path: Path) -> None:
    original = parse_im4p(original_im4p.read_bytes())
    wrap_raw(
        raw_path.read_bytes(),
        destination,
        ticket_path,
        fourcc="rkrn",
        description=original.description,
        compress=True,
        properties=original.properties,
    )


def extract_raw_or_img4(source: Path) -> bytes:
    """Accept either a raw filesystem image or an IM4P/IMG4-wrapped one."""

    data = source.read_bytes()
    if len(data) >= 36 and data[32:36] == b"NXSB":
        return data
    try:
        extracted = extract_im4p(data)
    except Img4ToolError:
        return data
    return extracted
