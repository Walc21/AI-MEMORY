"""Flat object names; no extension classification or format identity."""

import re


def validate_names(names: list[str]) -> None:
    if any(not isinstance(name, str) or not re.fullmatch(
        r"[1-9][0-9]*_[A-Za-z0-9]{3}(?:\.[^/\x00]*)?", name, re.DOTALL
    ) for name in names):
        raise ValueError("Nome fora do contrato Namer → Pacote → BBN1_1.")
    if len(names) != len(set(names)):
        raise ValueError("Nomes repetidos no lote.")
