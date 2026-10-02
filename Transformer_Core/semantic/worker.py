"""Bounded POSIX workers. Document strings never become executable commands."""

import base64
import io
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile

from .model import SemanticError


def execute(job: dict, limits) -> dict:
    payload = json.dumps(job, ensure_ascii=True, allow_nan=False).encode()
    maximum_input = 128 * 1024 * 1024
    if len(payload) > maximum_input:
        raise SemanticError("Entrada do worker excede o limite.")
    with tempfile.TemporaryDirectory(prefix="mimir-worker-") as temporary:
        folder = Path(temporary)
        stdout, stderr = folder / "output.json", folder / "error.log"
        env = {**os.environ, "MIMIR_WORKER_MEMORY_MB": str(limits.worker_memory_mb),
               "MIMIR_WORKER_CPU": str(limits.worker_timeout), "MIMIR_WORKER_OUTPUT": str(limits.max_output_bytes),
               "MIMIR_WORKER_TMP": temporary, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
        command = [sys.executable, *(["-S"] if sys.flags.no_site else []), "-m", "Transformer_Core.semantic.worker"]
        with stdout.open("wb") as out, stderr.open("wb") as err:
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                           env=env, start_new_session=True)
                process.communicate(payload, timeout=limits.worker_timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise SemanticError("Worker excedeu o timeout.") from exc
        if process.returncode or stdout.stat().st_size > limits.max_output_bytes:
            raise SemanticError("Worker falhou ou excedeu os limites de recursos.")
        try:
            response = json.loads(stdout.read_bytes())
            if not isinstance(response, dict) or set(response) != {"ok", "result", "error"}:
                raise SemanticError("Worker retornou IPC inválido.")
            if response["ok"] is not True:
                raise SemanticError(response.get("error", "Worker recusou a entrada."))
            return response["result"]
        except (ValueError, TypeError) as exc:
            raise SemanticError("Worker retornou IPC inválido.") from exc


def _guard(job):
    memory = int(os.environ["MIMIR_WORKER_MEMORY_MB"]) * 1024 * 1024
    cpu, output = int(os.environ["MIMIR_WORKER_CPU"]), int(os.environ["MIMIR_WORKER_OUTPUT"])
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    temp_root = Path(os.environ["MIMIR_WORKER_TMP"]).resolve()
    trusted_commands = {path for name in ("tesseract", "ffmpeg") if (path := shutil.which(name))}
    network = job.get("mode") in {"extract", "vision"} and job.get("profile", {}).get("name") == "ollama"
    def audit(event, args):
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.rename", "os.link", "os.symlink"}:
            paths = args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]
            if any(not isinstance(path, (str, bytes)) or not Path(os.fsdecode(path)).resolve().is_relative_to(temp_root) for path in paths):
                raise PermissionError("Mutação fora do temporário bloqueada no worker.")
        if event in {"socket.connect", "socket.sendto"}:
            address = args[1]
            if not network or not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise PermissionError("Rede bloqueada no worker.")
        if event == "subprocess.Popen" and args[0] not in trusted_commands:
            raise PermissionError("Executável não autorizado no worker.")
        if event in {"os.system", "os.exec", "os.posix_spawn", "os.posix_spawnp"}:
            raise PermissionError("Execução arbitrária bloqueada.")
        if event == "open" and isinstance(args[0], (str, bytes)):
            mode = args[1]
            flags = args[2]
            writing = isinstance(mode, str) and any(char in mode for char in "wax+")
            writing = writing or isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing:
                path = Path(os.fsdecode(args[0])).resolve()
                if not path.is_relative_to(temp_root):
                    raise PermissionError("Filesystem do worker aceita escrita somente no diretório temporário.")
    sys.addaudithook(audit)


def _picture(job):
    from PIL import Image
    import zipfile
    data = base64.b64decode(job["data"], validate=True)
    locator, kind = job["locator"], job["kind"]
    if locator["type"] == "ooxml" or locator["type"] == "archive_member":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            data = archive.read(locator["member"])
    if locator["type"] in {"pdf_page", "pdf_xobject"}:
        if locator["type"] == "pdf_xobject":
            from pypdf import PdfReader
            page = PdfReader(io.BytesIO(data)).pages[locator["page"] - 1]
            image = next(item for item in page.images if item.name.split(".")[0] == locator["name"].lstrip("/"))
            picture = Image.open(io.BytesIO(image.data))
        else:
            import fitz
            document = fitz.open(stream=data, filetype="pdf")
            pixmap = document[locator["page"] - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            picture = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    elif kind == "video_frame":
        import av
        with av.open(io.BytesIO(data)) as container:
            stream = container.streams[locator["stream"]]
            frame = next(frame for index, frame in enumerate(container.decode(stream)) if index == locator["frame"])
            picture = frame.to_image()
    else:
        picture = Image.open(io.BytesIO(data))
        picture.seek(locator.get("frame", 0))
    if picture.width * picture.height > 25000000:
        raise SemanticError("Imagem excede o limite de pixels.")
    return picture.convert("RGB")


def _ocr(job):
    import csv
    picture = _picture(job)
    image_path = Path(os.environ["MIMIR_WORKER_TMP"]) / "ocr.png"
    picture.convert("RGB").save(image_path)
    binary = shutil.which("tesseract")
    if not binary:
        raise SemanticError("Tesseract não instalado.")
    result = subprocess.run([binary, str(image_path), "stdout", "-l", job.get("language", "eng"), "tsv"],
                            capture_output=True, text=True, timeout=40, check=True)
    lines = {}
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        word = row["text"].strip()
        confidence = float(row["conf"])
        if word and confidence >= 0:
            key = tuple(row[field] for field in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append((word, int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]), confidence))
    observations = []
    for words in lines.values():
        left, top = min(w[1] for w in words), min(w[2] for w in words)
        right, bottom = max(w[1] + w[3] for w in words), max(w[2] + w[4] for w in words)
        observations.append({"text": " ".join(w[0] for w in words), "locator": {"type": "ocr_box", "left": left,
            "top": top, "width": right - left, "height": bottom - top,
            "image_width": picture.width, "image_height": picture.height},
            "confidence": min(1, sum(w[5] for w in words) / len(words) / 100), "method": "ocr"})
    return observations


def _asr(job):
    from faster_whisper import WhisperModel
    data = base64.b64decode(job["data"], validate=True)
    audio = io.BytesIO(data)
    if job["locator"]["type"] == "media_stream":
        import av
        # Select the structural stream explicitly; preserve its origin binding.
        audio = io.BytesIO()
        with av.open(io.BytesIO(data)) as source, av.open(audio, mode="w", format="wav") as target:
            stream = source.streams[job["locator"]["stream"]]
            output = target.add_stream("pcm_s16le", rate=16000)
            output.layout = "mono"
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in source.decode(stream):
                for resampled in resampler.resample(frame):
                    for packet in output.encode(resampled):
                        target.mux(packet)
            for resampled in resampler.resample(None):
                for packet in output.encode(resampled):
                    target.mux(packet)
            for packet in output.encode(None):
                target.mux(packet)
        audio.seek(0)
    model = WhisperModel(job["model"], device="cpu", compute_type="int8", local_files_only=True)
    segments, info = model.transcribe(audio, beam_size=1)
    return [{"text": segment.text.strip(), "locator": {"type": "audio_time", "start_seconds": segment.start,
             "end_seconds": segment.end, "sample_start": int(segment.start * 16000),
             "sample_end": int(segment.end * 16000), "sample_rate": 16000,
             "coordinate_space": "resampled_pcm"}, "confidence": max(0, min(1, 1 - segment.no_speech_prob)),
             "method": "asr"} for segment in segments if segment.text.strip()]


def _vision(job):
    import urllib.request
    from .extraction import extractor_profile, local_request
    extractor_profile("ollama", job["vision_model"], job["endpoint"])
    picture = _picture(job)
    encoded = io.BytesIO()
    picture.thumbnail((1536, 1536))
    picture.save(encoded, format="PNG")
    request = urllib.request.Request(job["endpoint"].rstrip("/") + "/api/generate", data=json.dumps({
        "model": job["vision_model"], "stream": False, "format": "json", "system": job["prompt"],
        "prompt": "Describe the attached image as evidence.", "images": [base64.b64encode(encoded.getvalue()).decode()],
        "options": {"temperature": 0}}).encode(), headers={"Content-Type": "application/json"})
    payload = local_request(request, 40, 4 * 1024 * 1024)
    value = json.loads(json.loads(payload)["response"])
    if not isinstance(value, dict) or set(value) != {"observations"} or not isinstance(value["observations"], list):
        raise SemanticError("Visão retornou schema inválido.")
    output = []
    for item in value["observations"]:
        if not isinstance(item, dict) or set(item) != {"text", "confidence"} or not isinstance(item["text"], str) or not item["text"].strip() or type(item["confidence"]) not in {int, float} or not 0 <= item["confidence"] <= 1:
            raise SemanticError("Observação de visão inválida.")
        output.append({**item, "method": "vision", "locator": {"type": "image_region", "scope": "whole_image"}})
    return output


def main():
    job = json.load(sys.stdin)
    _guard(job)
    try:
        if job["mode"] == "extract":
            from .extraction import extract
            if "texts" in job:
                result = [extract(text, job["profile"], job.get("timeout", 30)) for text in job["texts"]]
            else:
                result = extract(job["text"], job["profile"], job.get("timeout", 30))
        elif job["mode"] == "structural":
            from dataclasses import asdict
            from Transformer_Core.structural.model import Limits
            from Transformer_Core.structural.protocols import observe
            result = asdict(observe(base64.b64decode(job["data"], validate=True), job["filename"], Limits(**job["limits"]), job["strict"]))
        elif job["mode"] == "ocr":
            result = _ocr(job)
        elif job["mode"] == "asr":
            result = _asr(job)
        elif job["mode"] == "vision":
            result = _vision(job)
        else:
            raise SemanticError("Modo de worker desconhecido.")
        response = {"ok": True, "result": result, "error": None}
    except Exception as exc:
        response = {"ok": False, "result": None, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    print(json.dumps(response, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
