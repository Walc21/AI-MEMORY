# AI MEMORY / Mimir

Implementação incremental da arquitetura Mimir. Esta etapa contém a fronteira **Input**, o **Pacote** e o **Namer** de `BN1_1`.

## Fluxo atual

```text
Input / Storage_I (interface CLI)
    -> BN1_1 / Pacote (arquivos reais em cache)
    -> n (somente número inteiro) -> BN1_1 / Namer
    -> lista de stems -> BN1_1 / Pacote (associação aleatória e renomeação)
```

Requer Python 3.10+ em sistema POSIX (o bloqueio de processos usa `fcntl`). Na raiz do repositório:

```bash
python -m input open
python -m input add /caminho/arquivo.pdf /caminho/imagem.png
python -m input add /caminho/outro-arquivo
python -m input status
python -m input close
```

`open` ativa a janela; cada `add` copia arquivos regulares de qualquer formato para o Pacote. `close` a desativa, confere as entradas guardadas e entrega exclusivamente `{"n": N}` ao Namer em `.mimir-runtime/BN1_1/Namer/inbox/n.json`. O Namer gera uma vez um ID de três caracteres (`A–Z`, `a–z`, `0–9`), constrói `["1_ID", "2_ID", ..., "n_ID"]` e devolve a lista em `BN1_1/Namer/outbox/stems.json`. O Pacote sorteia uma bijeção entre arquivos e stems, renomeia fisicamente cada arquivo e conserva sua extensão literal após o último ponto. Nenhum byte ou nome original chega ao Namer. A única entrada pública é `python -m input`; o caminho de execução pode ser alterado por `MIMIR_RUNTIME_DIR`.

Submissões repetidas contam como entradas distintas, mesmo quando o conteúdo ou o nome coincide. Nenhum tipo ou conteúdo é interpretado. Um bloqueio de processo serializa `open`, `add` e `close`; depois de fechado, o ciclo não aceita arquivos novos. `close` pode ser repetido após uma interrupção: o Namer reutiliza a mesma lista e o Pacote retoma a atribuição já registrada.

O cache e o índice ficam no diretório de execução, ignorado pelo Git. **Não há expiração automática nesta etapa.** O prazo de retenção e a confirmação de ingestão pelo BBN1_1 ainda serão definidos; por isso o Pacote não apaga os arquivos nem permite iniciar outro ciclo após `close`. Para testar outro ciclo, use outro diretório de execução temporário com `MIMIR_RUNTIME_DIR`. Não coloque dados sensíveis no clone sem escolher um armazenamento local adequado.

## Limites desta etapa

- `Input` é a fronteira externa; `BN1_1/Pacote` guarda os bytes. O Namer recebe apenas `n`.
- O próximo passo arquitetural será o envio da lista de nomes renomeados ao Sorter; Sorter e BBN1_1 ainda não existem.
- O cache é em disco para permitir arquivos arbitrários e sobreviver ao encerramento do comando. A política futura de TTL e a liberação após confirmação do BBN1_1 estão pendentes.

Teste: `python -m unittest discover -s tests -v`.
