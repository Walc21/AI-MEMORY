import json
import struct
import tempfile
import unittest
import wave
import zlib
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import av
import numpy as np

from BN1_1.Pacote.cache import Pacote, CacheError
from Transformer_Core.Fields.calculus import difference, rate, spatial_gradient, trajectory_velocity
from Transformer_Core.Fields.store import FieldStore, Limits, FieldError, write_json
from Transformer_Core.Hot_Hub.hub import _digest


def png(values):
    """Small unfiltered, non-interlaced PNG fixtures, no imaging dependency."""
    depth = values.dtype.itemsize * 8
    height, width = values.shape[:2]
    color = 0 if values.ndim == 2 else 2
    raw = values.astype(">u2" if depth == 16 else "u1")
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    rows = b"".join(b"\0" + row.tobytes() for row in raw)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class FieldTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()

    def build(self, path, limits=None):
        manifest = FieldStore(self.root, limits).build({path.name: _digest(path)})
        record_path = self.root / "representations" / manifest["generation"] / manifest["records"][path.name]
        return json.loads(record_path.read_text()), record_path.parent

    def test_static_image_has_zero_time_change_but_nonzero_spatial_gradient(self):
        values = np.array([[[0, 10, 20], [255, 30, 10]], [[40, 90, 20], [10, 40, 30]]], dtype=np.uint8)
        path = self.data / "1_aB7.mp3"  # Deliberately incorrect extension.
        path.write_bytes(png(values))
        record, folder = self.build(path)
        self.assertEqual(record["status"], "decoded")
        field = record["fields"][0]
        self.assertEqual(field["time"]["kind"], "static")
        chunk = field["chunks"][0]
        self.assertEqual(chunk["temporal"], {"kind": "constant", "rate": 0})
        samples = np.load(folder / chunk["samples"]["file"], allow_pickle=False)
        np.testing.assert_array_equal(samples, values)
        gradients = spatial_gradient(samples, axes=(0, 1))
        np.testing.assert_array_equal(gradients[1][0, 0], [255, 20, -10])
        self.assertEqual(path.read_bytes(), png(values))

    def test_16bit_rgb_and_gray_are_not_downconverted(self):
        for index, values in enumerate((np.array([[0, 65535, 1025]], dtype=np.uint16),
                np.array([[[65535, 30000, 1025], [0, 4, 257]]], dtype=np.uint16))):
            path = self.data / f"{index + 1}_aB7"
            path.write_bytes(png(values))
            record, folder = self.build(path)
            self.assertEqual(record["status"], "decoded", record)
            chunk = record["fields"][0]["chunks"][0]
            decoded = np.load(folder / chunk["samples"]["file"], allow_pickle=False)
            np.testing.assert_array_equal(decoded, values)
            self.assertEqual(decoded.dtype.itemsize, 2)

    def test_pcm_sample_clock_stereo_and_signed_differences_across_chunks(self):
        path = self.data / "1_aB7.pdf"
        values = np.tile(np.array([[-32768, 32767], [32767, -32768]], dtype="<i2"), (6000, 1))
        with wave.open(str(path), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(values.tobytes())
        record, folder = self.build(path)
        field = record["fields"][0]
        chunks = field["chunks"]
        self.assertGreater(len(chunks), 1)
        joined, changes = [], []
        for chunk in chunks:
            self.assertEqual(chunk["step"], [1, 8000])
            joined.append(np.load(folder / chunk["samples"]["file"], allow_pickle=False))
            if "from_previous" in chunk:
                self.assertEqual(chunk["from_previous"]["dt"], [1, 8000])
                changes.append(np.load(folder / chunk["from_previous"]["delta"]["file"], allow_pickle=False)[None])
            changes.append(np.load(folder / chunk["temporal"]["delta"]["file"], allow_pickle=False))
        np.testing.assert_array_equal(np.concatenate(joined), values)
        np.testing.assert_array_equal(np.concatenate(changes), np.diff(values.astype(np.int64), axis=0))

    def test_video_uses_real_variable_timestamps_and_preserves_native_samples(self):
        path = self.data / "1_aB7.txt"
        originals = [np.full((4, 4), value, dtype=np.uint16) for value in (100, 200, 500)]
        with av.open(str(path), "w", format="matroska") as out:
            stream = out.add_stream("ffv1", rate=10)
            stream.width = stream.height = 4
            stream.pix_fmt = "gray16le"
            stream.time_base = Fraction(1, 10)
            stream.codec_context.time_base = Fraction(1, 10)
            for pts, values in zip((0, 1, 3), originals):
                frame = av.VideoFrame.from_ndarray(values, format="gray16le")
                frame.pts, frame.time_base = pts, Fraction(1, 10)
                out.mux(stream.encode(frame))
            out.mux(stream.encode())
        record, folder = self.build(path)
        self.assertEqual(record["status"], "decoded", record)
        chunks = record["fields"][0]["chunks"]
        self.assertEqual([c["start"] for c in chunks], [[0, 1], [1, 10], [3, 10]])
        self.assertEqual([c["from_previous"]["dt"] for c in chunks[1:]], [[1, 10], [1, 5]])
        for chunk, original in zip(chunks, originals):
            np.testing.assert_array_equal(np.load(folder / chunk["samples"]["file"], allow_pickle=False), original)
        delta = np.load(folder / chunks[2]["from_previous"]["delta"]["file"], allow_pickle=False)
        np.testing.assert_array_equal(rate(delta, Fraction(1, 5)), np.full((4, 4), 1500))

    def test_arbitrary_tensor_has_no_invented_time_or_three_dimensional_projection(self):
        path = self.data / "1_aB7.zip"
        values = np.arange(120, dtype=np.int64).reshape(2, 3, 4, 5)
        with path.open("wb") as out:
            np.save(out, values, allow_pickle=False)
        record, folder = self.build(path)
        field = record["fields"][0]
        self.assertEqual(field["axes"], ["index_0", "index_1", "index_2", "index_3"])
        self.assertEqual(field["time"]["kind"], "unmodeled")
        np.testing.assert_array_equal(np.load(folder / field["chunks"][0]["samples"]["file"], allow_pickle=False), values)

    def test_one_container_keeps_both_video_and_audio_tracks(self):
        path = self.data / "1_aB7"
        sound = np.arange(800, dtype=np.int16).reshape(1, -1)
        with av.open(str(path), "w", format="matroska") as out:
            video = out.add_stream("ffv1", rate=10)
            video.width = video.height = 4
            video.pix_fmt = "gray"
            audio = out.add_stream("pcm_s16le", rate=8000)
            audio.layout = "mono"
            frame = av.VideoFrame.from_ndarray(np.zeros((4, 4), dtype=np.uint8), format="gray")
            frame.pts, frame.time_base = 0, Fraction(1, 10)
            out.mux(video.encode(frame))
            af = av.AudioFrame.from_ndarray(sound, format="s16", layout="mono")
            af.sample_rate, af.pts, af.time_base = 8000, 0, Fraction(1, 8000)
            out.mux(audio.encode(af))
            out.mux(video.encode())
            out.mux(audio.encode())
        record, folder = self.build(path)
        self.assertEqual(record["status"], "decoded", record)
        self.assertEqual(len(record["fields"]), 2)
        field = next(f for f in record["fields"] if f["axes"] == ["sample", "channel"])
        actual = np.concatenate([np.load(folder / c["samples"]["file"], allow_pickle=False) for c in field["chunks"]])
        np.testing.assert_array_equal(actual[:, 0], sound[0])

    def test_native_10bit_chroma_planes_keep_their_own_grids(self):
        path = self.data / "1_aB7.bin"
        frame = av.VideoFrame(4, 4, "yuv420p10le")
        originals = []
        for index, plane in enumerate(frame.planes):
            padded = np.zeros((plane.height, plane.line_size // 2), dtype="<u2")
            padded[:, :plane.width] = 1023 - index * 200
            plane.update(padded.tobytes())
            originals.append(padded[:, :plane.width].copy())
        frame.pts, frame.time_base = 0, Fraction(1, 10)
        with av.open(str(path), "w", format="matroska") as out:
            stream = out.add_stream("ffv1", rate=10)
            stream.width = stream.height = 4
            stream.pix_fmt = "yuv420p10le"
            out.mux(stream.encode(frame))
            out.mux(stream.encode())
        record, folder = self.build(path)
        self.assertEqual(record["status"], "decoded", record)
        self.assertEqual(len(record["fields"]), 3)
        for field, original in zip(record["fields"], originals):
            np.testing.assert_array_equal(np.load(folder / field["chunks"][0]["samples"]["file"], allow_pickle=False), original)
            self.assertEqual(field["native"]["component_bits"], [10, 10, 10])

    def test_unknown_and_corrupt_data_remain_exact_opaque_byte_fields(self):
        for data in (b"", b"arbitrary text", b"\0\xff\0", b"\x89PNG\r\n\x1a\nBAD"):
            path = self.data / "1_aB7.png"
            path.write_bytes(data)
            record, _ = self.build(path)
            self.assertEqual(record["status"], "opaque")
            self.assertEqual(record["fields"], [])
            self.assertEqual(record["byte_field"]["time"], "unmodeled")
            self.assertEqual(path.read_bytes(), data)

    def test_limit_reports_opaque_without_publishing_a_truncated_field(self):
        path = self.data / "1_aB7.png"
        original = png(np.zeros((2, 2, 3), dtype=np.uint8))
        path.write_bytes(original)
        record, folder = self.build(path, Limits(max_derived_bytes=1))
        self.assertEqual(record["status"], "opaque")
        self.assertEqual(record["reason"], "derived_byte_limit")
        self.assertEqual([p.name for p in folder.iterdir()], ["record.json"])
        self.assertEqual(path.read_bytes(), original)

    def test_gradients_require_anchors_and_trajectories_require_supplied_positions(self):
        first = np.array([[1, 3], [4, 10]], dtype=np.int32)
        second = first + 100
        np.testing.assert_array_equal(spatial_gradient(first, (0,))[0], spatial_gradient(second, (0,))[0])
        velocity = trajectory_velocity(np.array([[0., 0., 0.], [2., 4., 0.], [8., 4., 6.]]), [0, 2, 5])
        np.testing.assert_array_equal(velocity, [[1, 2, 0], [2, 0, 2]])
        with self.assertRaises(ValueError):
            trajectory_velocity(np.array([[0., 0.], [1., 1.]]), [0, 0])
        with self.assertRaises(ValueError):
            rate([1], 0)

    def test_failed_publication_retains_bbn_and_retry_repairs_derived_corruption(self):
        source = self.root / "image.png"
        source.write_bytes(png(np.zeros((2, 2, 3), dtype=np.uint8)))
        runtime = self.root / "runtime"
        pacote = Pacote(runtime)
        pacote.open()
        pacote.add([source])
        def fail_manifest(path, payload):
            if path.name.startswith(".fields-"):
                raise OSError("simulated disk interruption")
            write_json(path, payload)
        with patch("Transformer_Core.Fields.store.write_json", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                pacote.close()
        self.assertTrue((runtime / "BN1_1/BBN1_1").exists())
        self.assertEqual(len(list((runtime / "BN1_1/Pacote/files").glob("*/*"))), 1)
        self.assertEqual(pacote.close(), 1)
        hub = runtime / "Transformer_Core/Hot_Hub"
        manifest = json.loads((hub / "fields.json").read_text())
        path = hub / "representations" / manifest["generation"] / next(iter(manifest["records"].values()))
        record = json.loads(path.read_text())
        array = path.parent / record["fields"][0]["chunks"][0]["samples"]["file"]
        array.write_bytes(b"damaged")
        with self.assertRaises(FieldError):
            FieldStore(hub).verify(manifest["sources"])
        self.assertEqual(pacote.close(), 1)
        fresh = FieldStore(hub).verify(manifest["sources"])
        self.assertNotEqual(fresh["generation"], manifest["generation"])
        self.assertEqual((hub / "data" / next(iter(manifest["sources"]))).read_bytes(), source.read_bytes())
        self.assertFalse((runtime / "BN1_1/BBN1_1").exists())

    def test_legacy_cycle_refused_without_rewriting_state_or_deleting_data(self):
        runtime = self.root / "legacy"
        runtime.mkdir()
        state = runtime / "cycle.json"
        original = b'{"status":"HUB_READY","items":[]}'
        state.write_bytes(original)
        with self.assertRaisesRegex(CacheError, "layout antigo"):
            Pacote(runtime).close()
        self.assertEqual(state.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
