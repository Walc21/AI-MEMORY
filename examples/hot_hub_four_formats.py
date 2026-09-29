"""Generate four synthetic, reproducible inputs for the README demonstration.

Requires project requirements and reportlab: pip install reportlab
"""

from fractions import Fraction
from pathlib import Path
import wave

import av
import numpy as np
from openpyxl import Workbook
from reportlab.pdfgen import canvas


def create_examples(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    pdf = folder / "nota.pdf"
    page = canvas.Canvas(str(pdf), pagesize=(595, 842), pageCompression=0)
    page.drawString(72, 760, "Relato: a rua tem carros e pedestres.")
    page.save()

    sound = folder / "rua.wav"
    rate = 8000
    t = np.arange(5 * rate) / rate
    rng = np.random.default_rng(21)
    # Synthetic traffic impression: engine hum + high-frequency hiss.
    samples = 0.16 * np.sin(2 * np.pi * (70 * t + 10 * t**2))
    samples += 0.07 * rng.standard_normal(len(t))
    pcm = np.round(np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(sound), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm.tobytes())

    road = folder / "estrada.mkv"
    with av.open(str(road), "w", format="matroska") as output:
        stream = output.add_stream("ffv1", rate=10)
        stream.width, stream.height, stream.pix_fmt = 32, 24, "gray"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 10)
        for index in range(50):
            picture = np.full((24, 32), 155, dtype=np.uint8)
            picture[8:, 7:25] = 75
            for y in range(9, 24):
                if (y + index * 2) % 8 < 4:
                    picture[y, 15:17] = 240
            frame = av.VideoFrame.from_ndarray(picture, format="gray")
            frame.pts, frame.time_base = index, Fraction(1, 10)
            output.mux(stream.encode(frame))
        output.mux(stream.encode())

    sheet = folder / "medidas.xlsx"
    book = Workbook()
    page = book.active
    page.title = "Medidas"
    for row in range(1, 6):
        for column in range(1, 6):
            page.cell(row, column, (row - 1) * 5 + column)
    book.save(sheet)
    return [pdf, sound, road, sheet]


if __name__ == "__main__":
    for path in create_examples(Path("examples/generated")):
        print(path)
