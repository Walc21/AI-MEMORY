"""Classify renamed filenames by their literal final extension, without bytes."""

import re
from pathlib import Path


RENAMED = re.compile(r"^[1-9][0-9]*_[A-Za-z0-9]{3}(?:\.(.+))?$")


class SorterError(Exception):
    pass


class Sorter:
    @staticmethod
    def extension(filename: str) -> str:
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise SorterError("O Sorter só aceita nomes renomeados, sem caminhos.")
        if not RENAMED.fullmatch(filename):
            raise SorterError("Nome fora do contrato Namer → Pacote → Sorter.")
        return filename.rsplit(".", 1)[1] if "." in filename else ""

    @classmethod
    def classify(cls, filenames: list[str]) -> dict[str, list[str]]:
        """Group metadata only; the empty key is the no-extension class."""
        if len(filenames) != len(set(filenames)):
            raise SorterError("O mesmo nome renomeado apareceu mais de uma vez.")
        groups: dict[str, list[str]] = {}
        for filename in filenames:
            groups.setdefault(cls.extension(filename), []).append(filename)
        return groups
