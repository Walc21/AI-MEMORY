# Contrato do Hot Hub: vetor de bytes e chunks de 1024

## Escopo e substituição

Esta fase substitui integralmente o espelho de originais e os campos dependentes de decodificadores. O Hot Hub não extrai texto, células, frames, áudio ou sinais físicos. Recebe os bytes completos fornecidos pelo BBN1_1 e publica um único formato, JSON Lines UTF-8. Desconhecimento da extensão ou impossibilidade de decodificar o conteúdo não alteram o procedimento.

Runtime: biblioteca padrão Python 3.10+. Layout do ciclo: 3. Esquema de saída: `mimir.byte-chunks.v1`.

## Modelo matemático

Para cada arquivo `f`, seja `v_f = (b_0, ..., b_(L-1))`, com `b_i ∈ {0,...,255}`. `bytes` é a representação concreta deste vetor. O algoritmo lê um arquivo inteiro, processa-o e libera o vetor antes de ler o seguinte.

Defina `B = 1024`, `m = ceil(L/B)`, `j ∈ {0,...,m-1}`, `r_j = min(B,L-jB)`. Cada `c_j` tem `B` componentes; os primeiros `r_j` são `v_f[jB:jB+r_j]` e os demais são zeros de preenchimento. Nenhuma soma, normalização, divisão dos valores, conversão de sinal ou extração de conteúdo é aplicada aos bytes.

O conjunto por arquivo é uma família indexada:

`C_f = {(source_id, marker, j, r_j, c_j) : 0 ≤ j < m}`.

O índice distingue chunks idênticos e define a ordem. Não se usa um `set` dos valores, pois isso eliminaria repetições. As fatias válidas particionam `[0,L)` sem sobreposição nem lacunas. Logo:

`concat(c_0[:r_0], ..., c_(m-1)[:r_(m-1)]) = v_f`.

Para `L=0`, `m=0`: nenhum chunk artificial é criado; o cabeçalho registra o arquivo vazio. Para múltiplos de 1024, o último chunk tem 1024 bytes válidos, sem bloco adicional.

## Identidade e orientação

- `source_name`: nome recebido do contrato Namer/Pacote, por exemplo `1_aB7.PDF`.
- `marker`: `1_aB7/PDF`, preservando maiúsculas. O sufixo conservado pelo Pacote é a extensão após o último ponto do nome original; `arquivo.tar.gz` chega como `X_ID.gz` e gera `X_ID/gz`.
- Sem extensão: `source_name = 1_aB7`, `marker = 1_aB7/`.
- `generation`: UUID aleatório de 128 bits da publicação, independente do ID temporário de três caracteres do Namer.
- `source_id`: `<generation>:<source_name>`. Evita usar o rótulo temporário como identidade global entre lotes. Cada geração é criada em diretório exclusivo; colisão de UUID faz a criação falhar, sem sobrescrever.
- `index`: inteiro começando em zero e aumentando de um em um dentro de cada arquivo.

O marcador é orientação de formato, não chave de roteamento, diretório ou instrução de decodificação. O manifesto aponta explicitamente qual registro pertence a qual arquivo. Cada chunk repete `marker` e `source_id`; a validação rejeita mistura, troca, duplicação e reordenação.

## Formato de saída

Todos os arquivos publicados usam a mesma codificação JSON Lines (`.jsonl`); cada linha é um objeto JSON independente. Os números de `values` são bytes em representação decimal, sem Base64 e sem arrays de outros dtypes. Não há arquivos binários de conteúdo paralelo no Hot Hub.

Um registro de arquivo contém:

1. Cabeçalho com `kind: file`, `schema`, `source_name`, `source_id`, `marker`, `byte_length`, `sha256` do arquivo original, `chunk_size: 1024` e `chunk_count`.
2. Exatamente `chunk_count` linhas com `kind: chunk`, `marker`, `source_id`, `index`, `valid_length` e `values`.

`values` sempre contém 1024 inteiros de 0 a 255; booleanos não são aceitos como inteiros. `valid_length` está entre 1 e 1024; só o último chunk pode ter menos de 1024 bytes válidos. Todos os valores após esse limite devem ser zero.

Exemplo conceitual (abreviado, os `...` não são JSON real):

```text
Entrada 1_aB7.bin: bytes [65, 0, 255]
Cabeçalho: byte_length=3, chunk_count=1, marker="1_aB7/bin"
Chunk 0: valid_length=3, values=[65,0,255,0,...,0] (1024 valores)
Reconstrução: values[:3] = [65,0,255]
```

O manifesto `manifest.jsonl` contém uma única linha com:

- `schema` e `generation`;
- `sources`: mapa nome → SHA-256 esperado, recebido do Pacote;
- `records`: mapa nome → `{file, sha256}`, onde o hash verifica o registro `.jsonl` completo;
- `summary`: totais `files`, `bytes` válidos e `chunks`.

O manifesto é o ponto de publicação. Consumidores não devem descobrir lotes listando diretórios nem concatenar chunks globalmente. Devem selecionar a geração ativa, resolver o arquivo pelo manifesto e validar seu conjunto. `HotHub.verify(expected)` faz a validação completa e `HotHub.reconstruct(name, expected)` oferece um consumidor de referência.

## Integração, falhas e integridade

O Pacote mantém o bloqueio do ciclo durante o handoff. Os estados são `OPEN → CLOSED → NAMING → NAMED → STAGED → CHUNKING → HUB_VERIFIED → HUB_READY`. O BBN1_1 requisita bytes do Pacote como antes. Sua limpeza requer agora reconstrução verificável do conteúdo do Hot Hub e confirmação do Pacote.

`HotHub.build(bbn_root, expected)`:

1. Confere a lista exata de arquivos regulares de entrada; rejeita symlinks e entradas especiais.
2. Para cada arquivo, lê um vetor de bytes e compara seu SHA-256 ao esperado.
3. Grava cabeçalho e chunks em um registro separado de uma nova geração.
4. Verifica nomes, identidade, índices, dimensões, padding, quantidade, hash dos registros, reconstrução dos hashes de origem e resumo do lote.
5. Sincroniza dados e diretórios da geração. Publica o manifesto via substituição atômica e sincroniza o diretório do Hot Hub.
6. Verifica novamente o lote publicado antes de devolver o resultado.

Uma falha antes da substituição do manifesto mantém a geração anterior ativa, se existente. Falhas de escrita ou de verificação impedem liberar BBN1_1 e Pacote. `close` pode retomar usando o mesmo ID do Namer; se a saída estiver corrompida, reconstrói-a a partir do Pacote íntegro. Dados órfãos não se tornam ativos por existir em disco.

Os hashes verificam integridade relativamente ao inventário confiável do ciclo; não são assinaturas contra alguém capaz de modificar simultaneamente todos os arquivos, hashes e código. As garantias de sincronização dependem do sistema de arquivos POSIX e do dispositivo. Este componente não é um sandbox.

## Linguagem, custo e migração

Python usa os mesmos objetos, imports e chamadas do restante do sistema. Um vetor `bytes` possui um byte por componente, sem precisão de ponto flutuante. JSON Lines evita dependência de formatos internos de uma linguagem para a próxima etapa. Rust ou C poderiam ser considerados após medições de desempenho, mas adicionariam uma fronteira de integração desnecessária nesta fase.

Construção: tempo `O(L)` e memória `O(L+B)` por arquivo, além do inventário do lote. A gravação produz um chunk por vez. A verificação mantém um chunk por vez (`O(B)` além do inventário). O consumidor de referência que reconstrói o arquivo inteiro usa `O(L)`; o futuro consumidor pode percorrer as linhas sequencialmente. A expansão decimal e os metadados fazem a saída ocupar mais espaço que a origem.

APIs antigas `mirror`, `normalize` e `FieldStore`, além de `fields.json`, `.npy` e contagens `decoded/partial/opaque`, foram removidas. Usar `build`, `verify`, `reconstruct`, `manifest.jsonl` e `files/bytes/chunks`. Ciclos do layout 2 são recusados sem alteração. Preserve o runtime antigo e reenvie os originais para um runtime novo. Não há migração, coleta de gerações ou apagamento físico seguro automático.
