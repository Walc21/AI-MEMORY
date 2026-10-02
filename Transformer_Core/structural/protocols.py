"""Deterministic structural protocols. No OCR, transcription or inference."""

import csv
from fractions import Fraction
import hashlib
from importlib import metadata
import io
import json
import math
from pathlib import Path, PurePosixPath
import threading
import wave
import xml.etree.ElementTree as ET
import zipfile

from .model import Limits, ProtocolError, ProtocolResult, StructuralError, Unit
from .router import route


# csv.field_size_limit is process-global. Serialize temporary adjustments for
# direct library callers as well as the isolated pipeline workers.
_CSV_LIMIT_LOCK = threading.RLock()


def dependency_versions() -> dict:
    versions = {}
    for name in ("pypdf", "Pillow", "av"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _text(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    text = data.decode("utf-8-sig", errors="strict")
    if len(text) > limits.max_text_chars:
        raise ProtocolError("text_limit")
    offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    for number, line in enumerate(data[offset:].splitlines(keepends=True), 1):
        result.add(Unit("text_line", {"type": "bytes", "start": offset,
                                      "end": offset + len(line), "line": number},
                        {"text": line.decode("utf-8")}), limits)
        offset += len(line)


def _json(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    text = data.decode("utf-8-sig")
    if len(text) > limits.max_text_chars:
        raise ProtocolError("text_limit")
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ProtocolError("duplicate_json_key")
            output[key] = value
        return output
    def invalid_constant(value):
        raise ProtocolError("non_finite_json")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ProtocolError("non_finite_json")
        return number
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float)
    stack = [(value, "", None)]
    while stack:
        item, pointer, parent = stack.pop()
        kind = "object" if isinstance(item, dict) else "array" if isinstance(item, list) else "value"
        props = {"length": len(item)} if isinstance(item, (dict, list)) else {"value": item}
        index = result.add(Unit("json_" + kind, {"type": "json_pointer", "pointer": pointer}, props, parent), limits)
        children = item.items() if isinstance(item, dict) else enumerate(item) if isinstance(item, list) else []
        for key, child in reversed(list(children)):
            token = str(key).replace("~", "~0").replace("/", "~1")
            stack.append((child, pointer + "/" + token, index))


def _csv(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    text = data.decode("utf-8-sig")
    if len(text) > limits.max_text_chars:
        raise ProtocolError("text_limit")
    with _CSV_LIMIT_LOCK:
        previous_limit = csv.field_size_limit()
        # The complete input already passed max_text_chars. A valid field may
        # exceed Python's unrelated default of 128 KiB without exceeding it.
        csv.field_size_limit(max(previous_limit, len(text)))
        try:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t" if Path(filename).suffix.lower() == ".tsv" else ",", strict=True)
            previous = 0
            for row_number, fields in enumerate(reader, 1):
                result.add(Unit("table_row", {"type": "csv_row", "row": row_number,
                                             "line_start": previous + 1, "line_end": reader.line_num},
                                {"fields": fields}), limits)
                previous = reader.line_num
        finally:
            csv.field_size_limit(previous_limit)


class OfficeArchive:
    """Bounded OOXML reader. No external links, macros, entities or extraction."""
    def __init__(self, data: bytes, limits: Limits):
        self.archive = zipfile.ZipFile(io.BytesIO(data))
        self.limits = limits
        entries = self.archive.infolist()
        if len(entries) > limits.max_nodes or sum(info.file_size for info in entries) > limits.max_expanded_bytes:
            self.archive.close()
            raise ProtocolError("archive_limit")
        names = [info.filename for info in entries]
        if len(names) != len(set(names)) or any(
            name.startswith("/") or ".." in PurePosixPath(name).parts or "\\" in name
            for name in names
        ):
            self.archive.close()
            raise ProtocolError("invalid_archive_paths")
        self.names = set(names)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.archive.close()

    def xml(self, member: str):
        data = self.archive.read(member)
        declarations = data.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
            raise ProtocolError("xml_entities_refused")
        return ET.fromstring(data)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _docx_paragraph_text(paragraph):
    """Preserve explicit Word text separators in document order."""
    controls = {W + "tab": "\t", W + "br": "\n", W + "cr": "\n",
                W + "noBreakHyphen": "\u2011", W + "softHyphen": "\u00ad"}
    return "".join(part.text or "" if part.tag == W + "t" else controls.get(part.tag, "")
                   for part in paragraph.iter())


def _docx(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    total = 0
    with OfficeArchive(data, limits) as archive:
        document = archive.xml("word/document.xml")
        body = document.find(W + "body")
        if body is None:
            raise ProtocolError("missing_document_body")
        for position, element in enumerate(body):
            location = {"type": "ooxml", "member": "word/document.xml", "body_index": position}
            if element.tag == W + "p":
                text = _docx_paragraph_text(element)
                total += len(text)
                result.add(Unit("paragraph", location, {"text": text}), limits)
            elif element.tag == W + "tbl":
                table = result.add(Unit("table", location), limits)
                for row_number, row in enumerate(element.findall(W + "tr"), 1):
                    cells = ["\n".join(_docx_paragraph_text(paragraph) for paragraph in cell.iter(W + "p"))
                             for cell in row.findall(W + "tc")]
                    total += sum(map(len, cells))
                    result.add(Unit("table_row", {**location, "row": row_number}, {"cells": cells}, table), limits)
            if total > limits.max_text_chars:
                raise ProtocolError("text_limit")
        # Embedded media retains a member locator, without assigning meaning.
        for member in sorted(name for name in archive.names if name.startswith("word/media/") and not name.endswith("/")):
            blob = archive.archive.read(member)
            result.add(Unit("embedded_media", {"type": "archive_member", "member": member},
                            {"byte_length": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}), limits)


def _xlsx(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    total = 0
    with OfficeArchive(data, limits) as archive:
        book = archive.xml("xl/workbook.xml")
        relationships = {item.attrib["Id"]: item for item in archive.xml("xl/_rels/workbook.xml.rels")}
        strings = []
        if "xl/sharedStrings.xml" in archive.names:
            strings = ["".join(part.text or "" for part in item.iter(S + "t")) for item in archive.xml("xl/sharedStrings.xml")]
            if sum(map(len, strings)) > limits.max_text_chars:
                raise ProtocolError("text_limit")
        sheets = book.find(S + "sheets")
        if sheets is None:
            raise ProtocolError("missing_worksheets")
        for sheet_number, sheet in enumerate(sheets, 1):
            rel = relationships[sheet.attrib[R + "id"]]
            if rel.attrib.get("TargetMode") == "External":
                raise ProtocolError("external_worksheet")
            target = rel.attrib["Target"]
            member = target.lstrip("/") if target.startswith("/xl/") else "xl/" + target
            if ".." in PurePosixPath(member).parts or member not in archive.names:
                raise ProtocolError("invalid_worksheet_path")
            location = {"type": "ooxml", "member": member, "sheet": sheet_number}
            parent = result.add(Unit("worksheet", location, {"name": sheet.attrib["name"]}), limits)
            for cell in archive.xml(member).iter(S + "c"):
                value_element, formula = cell.find(S + "v"), cell.find(S + "f")
                cell_type = cell.attrib.get("t", "n")
                value = value_element.text if value_element is not None else None
                if cell_type == "s":
                    string_index = int(value)
                    if not 0 <= string_index < len(strings):
                        raise ProtocolError("invalid_shared_string")
                    value = strings[string_index]
                elif cell_type == "inlineStr":
                    value = "".join(part.text or "" for part in cell.iter(S + "t"))
                props = {"cell_type": cell_type, "value": value, "formula": formula.text if formula is not None else None}
                total += len(value or "") + len(props["formula"] or "")
                if total > limits.max_text_chars:
                    raise ProtocolError("text_limit")
                result.add(Unit("cell", {**location, "coordinate": cell.attrib["r"]}, props, parent), limits)


def _pdf(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    from pypdf import PdfReader
    result.dependencies["pypdf"] = metadata.version("pypdf")
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise ProtocolError("encrypted_pdf")
    total = 0
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        total += len(text)
        if total > limits.max_text_chars:
            raise ProtocolError("text_limit")
        parent = result.add(Unit("page", {"type": "pdf_page", "page": number},
                                {"width_points": float(page.mediabox.width),
                                 "height_points": float(page.mediabox.height), "text": text}), limits)
        resources = page.get("/Resources", {})
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        xobjects = resources.get("/XObject", {})
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        for key in sorted(xobjects):
            image = xobjects[key].get_object()
            if image.get("/Subtype") == "/Image":
                result.add(Unit("image", {"type": "pdf_xobject", "page": number, "name": str(key)},
                                {"width": int(image["/Width"]), "height": int(image["/Height"])}, parent), limits)


def _image(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    from PIL import Image
    result.dependencies["Pillow"] = metadata.version("Pillow")
    with Image.open(io.BytesIO(data)) as image:
        for frame in range(getattr(image, "n_frames", 1)):
            image.seek(frame)
            if image.width * image.height > limits.max_pixels:
                raise ProtocolError("pixel_limit")
            image.load()  # Validate the data, rather than reporting only a header.
            result.add(Unit("image", {"type": "image_frame", "frame": frame},
                            {"width": image.width, "height": image.height,
                             "mode": image.mode, "format": image.format}), limits)


def _wav(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    with wave.open(io.BytesIO(data), "rb") as audio:
        rate, frames = audio.getframerate(), audio.getnframes()
        width, channels = audio.getsampwidth(), audio.getnchannels()
        # Read PCM in bounded blocks to detect truncated payloads.
        remaining = frames
        while remaining:
            block = audio.readframes(min(remaining, 65536))
            count = len(block) // (width * channels)
            if not count or len(block) % (width * channels):
                raise ProtocolError("truncated_audio")
            remaining -= count
        result.add(Unit("audio_stream", {"type": "sample_range", "start": 0, "end": frames},
                        {"sample_rate": rate, "sample_width": width, "channels": channels,
                         "sample_count": frames, "duration": str(Fraction(frames, rate))}), limits)


def _media(data: bytes, filename: str, limits: Limits, result: ProtocolResult):
    import av
    result.dependencies["av"] = metadata.version("av")
    with av.open(io.BytesIO(data), mode="r") as container:
        selected = [stream for stream in container.streams if stream.type in {"audio", "video"}]
        if not selected:
            raise ProtocolError("no_audio_video_stream")
        parents = {}
        for stream in selected:
            if stream.type == "video" and stream.codec_context.width * stream.codec_context.height > limits.max_pixels:
                raise ProtocolError("pixel_limit")
            parents[stream.index] = result.add(Unit(stream.type + "_stream",
                {"type": "media_stream", "stream": stream.index},
                {"codec": stream.codec_context.name,
                 "time_base": str(stream.time_base) if stream.time_base else None}), limits)
        indices = {stream.index: 0 for stream in selected}
        for packet in container.demux(selected):
            for frame in packet.decode():
                stream = packet.stream
                props = {"pts": frame.pts, "time_base": str(frame.time_base) if frame.time_base else None}
                if stream.type == "video":
                    if frame.width * frame.height > limits.max_pixels:
                        raise ProtocolError("pixel_limit")
                    props.update(width=frame.width, height=frame.height, pixel_format=frame.format.name)
                else:
                    props.update(sample_count=frame.samples, sample_rate=frame.sample_rate, layout=frame.layout.name)
                result.add(Unit(stream.type + "_frame", {"type": "media_frame", "stream": stream.index,
                    "frame": indices[stream.index]}, props, parents[stream.index]), limits)
                indices[stream.index] += 1


PROTOCOLS = {"text": _text, "json": _json, "csv": _csv, "docx": _docx, "xlsx": _xlsx,
             "pdf": _pdf, "image": _image, "wav": _wav, "media": _media}


def observe(data: bytes, filename: str, limits: Limits, strict: bool = False) -> ProtocolResult:
    selected = route(filename)
    result = ProtocolResult(selected)
    reason = None
    if len(data) > limits.max_file_bytes:
        raise StructuralError("Arquivo excede max_file_bytes.")
    if selected == "unknown":
        reason = "unsupported_extension"
    else:
        try:
            PROTOCOLS[selected](data, filename, limits, result)
        except ImportError:
            reason = "missing_dependency"
        except ProtocolError as exc:
            reason = str(exc)
        except MemoryError:
            raise
        except Exception:
            # Parser errors are isolated at the protocol boundary. Never publish
            # partial observations as a complete representation.
            reason = "invalid_format"
    if reason is not None:
        if strict:
            raise ProtocolError(f"{filename}: {selected}: {reason}")
        return ProtocolResult(selected, "opaque", reason, result.dependencies)
    return result
