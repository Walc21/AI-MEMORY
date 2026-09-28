"""Namer: receives only n and returns one immutable stem list per cycle."""

import json
import os
import secrets
import string
import tempfile
from pathlib import Path


ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits


class NamerError(Exception):
    pass


class Namer:
    def __init__(self, runtime: Path):
        self.inbox = runtime / "BN1_1" / "Namer" / "inbox" / "n.json"
        self.outbox = runtime / "BN1_1" / "Namer" / "outbox" / "stems.json"

    def respond(self) -> list[str]:
        # This component reads only the numeric message; it has no path to the
        # Pacote's stored files and never sees filenames or file contents.
        with self.inbox.open(encoding="utf-8") as stream:
            message = json.load(stream)
        if set(message) != {"n"} or type(message["n"]) is not int or message["n"] < 0:
            raise NamerError("O Namer espera apenas um inteiro n não negativo.")
        n = message["n"]

        if self.outbox.exists():
            with self.outbox.open(encoding="utf-8") as stream:
                existing = json.load(stream)
            stems = existing.get("stems")
            if not isinstance(stems, list) or len(stems) != n:
                raise NamerError("A resposta já criada pelo Namer não corresponde a n.")
            return stems  # The ID never changes on retry.

        batch_id = "".join(secrets.choice(ALPHABET) for _ in range(3))
        stems = [f"{index}_{batch_id}" for index in range(1, n + 1)]
        self.outbox.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=self.outbox.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"stems": stems}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.outbox)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return stems
