"""Small, explicit PT/EN vocabulary; no probabilistic entailment claims."""

import re

from .model import normalized

NOISE = set("a o as os de da do das dos e em um uma na no nas nos que qual quais quem quando onde como quanto quantos quanta quantas por para pelo pela ao aos foi era ser esta este essa esse seu sua the an of in at and to who what when where how was is are does do did has have can could would please tell me about s field campo".split())
ALIASES = {}
for group in [
    "work works worked working trabalha trabalhava trabalhou trabalho emprego employer empresa organization organizacao",
    "live lives lived mora morava morou reside residencia residence localizacao location",
    "born nasceu nascimento nascido birth",
    "responsible responsavel responsabilidade responsibility",
    "color colour cor cores", "favorite favourite favorita favorito",
    "budget orcamento", "deadline prazo", "code codigo", "number numero",
    "serial serie", "temperature temperatura", "project projeto",
    "salary salario", "cost custo", "price preco", "name nome",
    "weight peso", "mass massa", "speed velocidade", "capacity capacidade",
    "email correio", "phone telefone", "address endereco", "password senha",
]:
    for term in group.split():
        ALIASES[term] = group.split()[0]


def terms(text):
    return {ALIASES.get(t, t) for t in re.findall(r"\w+", normalized(text)) if t not in NOISE}


def spans(text):
    """Exact sentence/line spans, retaining decimals and punctuation."""
    for match in re.finditer(r".+?(?:[.!?](?=\s|$)|\n|$)", text, re.DOTALL):
        start = match.start() + len(match.group()) - len(match.group().lstrip())
        end = match.end() - len(match.group()) + len(match.group().rstrip())
        if end > start:
            yield start, end


def uncertain(text):
    return bool(re.search(r"\b(?:talvez|hip[oó]tese|hipoteticamente|possivelmente|might|maybe|hypothesis|possibly|imagine|suponha)\b|^(?:se|if)\b", text, re.IGNORECASE))
