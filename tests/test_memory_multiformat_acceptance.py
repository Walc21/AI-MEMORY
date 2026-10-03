"""Real files: independent answers in nine formats, absent facts, restart/export.

All answers are judged against a hand-written oracle; another format never
duplicates the requested fact. OCR runs with Tesseract, not a fixture provider.
"""

import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from BN1_1.Pacote.cache import Pacote
from Transformer_Core.semantic.pipeline import Memory


def word_document():
    body = '<w:p><w:r><w:t>Nina lives in Recife.</w:t></w:r></w:p>'
    body += '<w:tbl>'
    for values in [("sensor", "serial"), ("Delta", "D-481")]:
        body += '<w:tr>' + ''.join('<w:tc><w:p><w:r><w:t>' + v + '</w:t></w:r></w:p></w:tc>' for v in values) + '</w:tr>'
    body += '</w:tbl>'
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + '</w:body></w:document>')
    return data.getvalue()


def corpus(root):
    from openpyxl import Workbook
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen import canvas
    contents = {
        "notes.txt": "Alice works at Acme.\n",
        "decision.md": "A senha do satélite Boreal é K8-Q271.\n",
        "registry.json": json.dumps({"project": "Aurora", "budget": 1450000, "active": False}),
        "records.csv": "sensor,serial,temperature\nOrion,O-772,-12.5\nVega,V-932,8.2\n",
        "inventory.tsv": "product\tcapacity\nAtlas\t128 GB\nNova\t64 GB\n",
        "document.docx": word_document(),
    }
    book = Workbook()
    sheet = book.active
    sheet.title = 'Instruments'
    for cell, value in {'A1':'sensor', 'C1':'serial', 'E1':'temperature',
                        'A2':'Sigma', 'C2':'S-571', 'E2':-21.75,
                        'A3':'Tau', 'C3':'T-229', 'E3':9.5, 'G1':'result', 'G2':'=E2*2'}.items():
        sheet[cell] = value
    data = io.BytesIO()
    book.save(data)
    contents['instrument.xlsx'] = data.getvalue()
    data = io.BytesIO()
    pdf = canvas.Canvas(data)
    y = 780
    for i in range(65):
        if y < 40:
            pdf.showPage()
            y = 780
        pdf.drawString(40, y, f"Reference item {i}: unrelated archival context.")
        y -= 12
    pdf.drawString(40, y, 'The speed of vehicle Helios is 37.25 km/h.')
    pdf.drawString(40, y-12, 'Bruno works at Lumen.')
    pdf.save()
    contents['report.pdf'] = data.getvalue()
    picture = Image.new('RGB', (1100, 170), 'white')
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 48)
    ImageDraw.Draw(picture).text((25, 45), 'Lara works at Cedar.', font=font, fill='black')
    data = io.BytesIO()
    picture.save(data, format='PNG')
    contents['scan.png'] = data.getvalue()
    paths = []
    for name, value in contents.items():
        path = root / name
        path.write_bytes(value if isinstance(value, bytes) else value.encode())
        paths.append(path)
    return paths


ORACLE = [
    ("Where does Alice work?", "Acme"),
    ("Qual e a senha do satelite Boreal?", "K8-Q271"),
    ("What is the budget of project Aurora?", "1450000"),
    ("What is the serial of sensor Orion?", "O-772"),
    ("What is the capacity of product Atlas?", "128 GB"),
    ("Where does Nina live?", "Recife"),
    ("What is the serial of sensor Delta?", "D-481"),
    ("What is the temperature of sensor Sigma?", "-21.75"),
    ("Onde Bruno trabalha?", "Lumen"),
    ("What is the speed of vehicle Helios?", "37.25 km/h"),
    ("Where does Lara work?", "Cedar"),
]


class MultiFormatAcceptance(unittest.TestCase):
    def test_real_multiformat_batch_precise_answers_absences_and_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = corpus(root)
            memory = Memory(root / 'memory')
            pacote = Pacote(root / 'runtime')
            pacote.open()
            pacote.add(sources)
            memory.ingest(root / 'runtime', ocr=True, strict_multimodal=True)
            # A new process-equivalent object must answer from the canon alone.
            restarted = Memory(root / 'memory')
            for question, expected in ORACLE:
                with self.subTest(question=question):
                    result = restarted.query(question)
                    self.assertFalse(result['abstained'], result['sufficiency'])
                    self.assertIn(expected, result['answer'])
                    self.assertTrue(result['answer_evidence'])
                    for citation in result['answer_evidence']:
                        path = root / (citation['content_id'].removeprefix('content:') + '.export')
                        if not path.exists():
                            restarted.source(citation['content_id'], path)
                        self.assertIn(path.read_bytes(), [source.read_bytes() for source in sources])
            absent = ["What is Alice favorite color?", "Quando Nina nasceu?",
                      "What is the password of project Aurora?", "What is the serial of sensor Absent?",
                      "What is the result of sensor Sigma?", "Where does Alice live?",
                      "Where does Bruno live?", "What is Lara favorite color?"]
            for question in absent:
                with self.subTest(absent=question):
                    result = restarted.query(question)
                    self.assertTrue(result['abstained'], result['answer'])
                    self.assertEqual(result['answer_evidence'], [])
                    self.assertEqual(result['claims'], [])
                    self.assertEqual(result['context']['untrusted_evidence'], [])
            self.assertTrue(restarted.verify()['verified'])


if __name__ == '__main__':
    unittest.main()
