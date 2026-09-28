# AI MEMORY / Mimir

Implementação incremental da arquitetura Mimir. Esta etapa contém a fronteira **Input** e os componentes **Pacote**, **Namer**, **Sorter** e **BBN1_1**.

## Fluxo atual

```text
Input / Storage_I (interface CLI)
    -> BN1_1 / Pacote (arquivos reais em cache)
    -> n (somente número inteiro) -> BN1_1 / Namer
    -> lista de stems -> BN1_1 / Pacote (associação aleatória e renomeação)
    -> nomes renomeados -> BN1_1 / Sorter (extensão -> SHA-256 / IDD)
    -> pares nome, IDD -> BN1_1 / BBN1_1 (arquivos por extensão)
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

Após renomear todos os arquivos, o Pacote passa somente a lista de nomes `stem.ext` ao Sorter. Para cada nome, o Sorter extrai o token após o último ponto, respeita maiúsculas/minúsculas e calcula `SHA-256(extensão UTF-8)` como IDD hexadecimal de 64 caracteres. Arquivos sem extensão usam o token vazio e `SHA-256("")`. O Sorter cria uma partição por IDD em `BN1_1/Sorter/partitions/<IDD>/names.json` e mantém `BN1_1/Sorter/extension_registry.json` com a relação verificável `IDD → extensão`. SHA-256 não é reversível: o registro é o mecanismo de recuperação da extensão; inconsistências são recusadas.

Depois da classificação, o BBN1_1 solicita cada arquivo real ao Pacote e confere SHA-256 do nome completo e do conteúdo recebido. As cópias verificadas ficam em `BN1_1/BBN1_1/by_extension/<extensão>/` ou, para arquivos sem extensão, em `BN1_1/BBN1_1/no_extension/`. Assim, `pdf` e `PDF` ficam separados. O Sorter opera apenas com nomes e metadados, sem acessar bytes.

Submissões repetidas contam como entradas distintas, mesmo quando o conteúdo ou o nome coincide. Nenhum tipo ou conteúdo é interpretado. Um bloqueio de processo serializa `open`, `add` e `close`; depois de fechado, o ciclo não aceita arquivos novos. `close` pode ser repetido após uma interrupção: o Namer reutiliza a mesma lista e o Pacote retoma a atribuição já registrada.

O cache, o registro e o BBN1_1 ficam no diretório de execução, ignorado pelo Git. **Não há expiração automática nesta etapa.** A confirmação para liberar a limpeza do Pacote e o prazo de retenção ainda serão definidos; os originais no Pacote permanecem após a cópia verificada. Para testar outro ciclo, use outro diretório de execução temporário com `MIMIR_RUNTIME_DIR`.

## Limites desta etapa

- `Input` é a fronteira externa; `BN1_1/Pacote` guarda os originais. Namer recebe apenas `n`; Sorter recebe apenas nomes; BBN1_1 recebe bytes apenas sob demanda ao Pacote.
- O próximo passo arquitetural é a fronteira BBN1_1 → Transformer_Core; ela ainda não existe.
- O registro de IDDs é persistido no diretório de execução do ciclo. SHA-256 cobre extensões arbitrárias de forma determinística, mas nenhum hash finito pode oferecer reversibilidade ou ausência matemática de colisões; divergências detectadas interrompem a entrega.
- O cache é em disco para permitir arquivos arbitrários e sobreviver ao encerramento do comando. A política futura de TTL e a liberação após confirmação do BBN1_1 estão pendentes.

Teste: `python -m unittest discover -s tests -v`.
