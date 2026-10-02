# Contrato estrutural: Hot Hub → BN1_2

Versão do pipeline: **0.2.0**. Esquemas: `mimir.structural.v1`, `mimir.provenance.v1` e `mimir.bn1_2.v1`.

Este contrato implementa a próxima etapa após o Hot Hub de bytes: Route Hub, protocolos observáveis, Frankenstein e Curadoria/G_P. O BN1_2 recebe uma representação uniforme antes da barreira semântica. A referência conceitual é o [diagrama fornecido de 28/09/2026](images/mimir-pipeline-2026-09-28.jpg).

## Integração com a ingestão existente

`Pacote.transform()` conclui `close()` quando necessário e depois adquire o mesmo bloqueio exclusivo do ciclo. Sob esse bloqueio, confere as cópias preservadas no Pacote, verifica integralmente o Hot Hub e transforma o snapshot publicado. O BBN1_1 já pode estar removido: a nova etapa não usa essa cópia intermediária.

O layout do ciclo permanece **3**, seu estado final permanece `HUB_READY`, e o Hot Hub continua usando `mimir.byte-chunks.v1`. A transformação persiste seu estado em `Transformer_Core/Structural/state.json`, com estados `TRANSFORMING`, `FAILED` e `BN1_2_READY`. O estado serve como relatório; o manifesto BN1_2 é o ponto de publicação e `verify` confirma sua integridade.

Chamadores diretos de `StructuralPipeline` devem adquirir o bloqueio do ciclo e impedir builds concorrentes. A entrada pública pela CLI e por `Pacote` já faz isso.

## Route Hub e protocolos

O roteador examina somente o sufixo normalizado para selecionar uma rota. A extensão original, inclusive sua capitalização, continua na identidade Hot Hub. Nomes sem extensão e extensões desconhecidas usam `unknown`. Um arquivo `.pdf` inválido continua preservado como bytes, mas seu protocolo não reporta sucesso.

Os protocolos retornam `ProtocolResult`: rota, estado, motivo, versões de dependências utilizadas e uma sequência de `Unit`. Cada unidade tem tipo estrutural, localização, propriedades e opcionalmente o índice de uma unidade anterior que constitui seu pai. Ausência de pai indica pertencimento à raiz do arquivo.

| Rota | Unidade e localização | Cobertura atual |
| --- | --- | --- |
| `text` | `text_line` / bytes e número da linha | UTF-8 estrito, BOM opcional, finais de linha preservados; não interpreta Markdown. |
| `json` | `json_object`, `json_array`, `json_value` / JSON Pointer | Percurso em pré-ordem, ordem de inserção das chaves, índices de arrays e valores escalares. |
| `csv` | `table_row` / linha lógica e linhas físicas | CSV com vírgula, TSV com tabulação; aspas e campos multilinha; sem detecção de cabeçalho ou tipos. |
| `docx` | `paragraph`, `table`, `table_row` / membro OOXML e índice do corpo | Corpo principal; mídia embutida como `embedded_media` com membro, tamanho e hash. Não associa toda a mídia ao ponto visual de exibição. |
| `xlsx` | `worksheet`, `cell` / membro, número de planilha e coordenada | Tipos, valores armazenados, strings compartilhadas e fórmulas textuais. Não avalia fórmulas nem interpreta estilos, gráficos ou datas. |
| `pdf` | `page`, `image` / página ou XObject direto | Texto extraído por página, dimensões e metadados das imagens XObject diretas. Não executa OCR ou reconstrói completamente o layout/Form XObjects. |
| `image` | `image` / índice do frame | Frames validados pelo Pillow, dimensões, modo e formato. Não atribui descrição ao conteúdo. |
| `wav` | `audio_stream` / intervalo de amostras | PCM validado, taxa, canais, largura, quantidade e duração racional. |
| `media` | streams e frames / stream e índice do frame | Áudio/vídeo decodificados por PyAV; codec, PTS, base temporal e dimensões ou quantidade de amostras. Não transcreve ou interpreta. |
| `unknown` | Somente a raiz `file` | Estado opaco com `unsupported_extension`. |

Localizações por página, membro ou frame são coordenadas do formato decodificado. Não são intervalos de bytes do contêiner. Só as linhas de texto e a raiz possuem intervalos exatos de bytes. Frames não incluem pixels ou amostras em novos blobs: a referência à origem permite recuperar o contêiner exato e aplicar novamente o protocolo.

A ordem dos frames de mídia segue a emissão do demuxer. O grafo `precedes` é formado por stream, sem converter essa ordem em causalidade ou relação semântica. PTS e base temporal podem ser `null` quando indisponíveis.

## Frankenstein: documento uniforme

Um documento por origem contém exatamente `schema`, `source`, `protocol`, `nodes` e `provenance`.

### Origem

`source` contém:

| Campo | Significado |
| --- | --- |
| `name` | Nome canônico atribuído pelo Pacote, por exemplo `1_aB7.pdf`. |
| `id` | `<geração Hot Hub>:<nome canônico>`. |
| `sha256` | Hash dos bytes originais verificados. |
| `byte_length` | Comprimento verificado no registro Hot Hub. |
| `hot_hub_generation` | UUID hexadecimal da geração de chunks. |
| `record_sha256` | Hash do registro JSONL do Hot Hub. |

A associação ao nome original enviado permanece no `cycle.json` do Pacote. A saída não confunde arquivos diferentes com conteúdo igual. Reparo do Hot Hub cria uma geração diferente e, consequentemente, novas identidades de origem.

### Protocolo

`protocol` contém `route`, `version` (inteiro 1), `status` (`complete` ou `opaque`), `reason` e `dependencies`.

`complete` significa que o protocolo concluiu sua cobertura declarada sem erro ou limite alcançado; não promete a extração de recursos que estão fora dessa cobertura. `opaque` contém um motivo explícito e somente a raiz do arquivo. Nenhuma lista de observações parcial é publicada como completa.

Motivos incluem `unsupported_extension`, `missing_dependency`, `invalid_format`, `node_limit`, `text_limit`, `archive_limit`, `pixel_limit`, `duplicate_json_key`, `non_finite_json`, `xml_entities_refused`, `encrypted_pdf` e `truncated_audio`. Motivos adicionais descrevem falhas específicas do leitor OOXML ou ausência de streams.

### Objetos

Cada objeto contém exatamente `id`, `source_id`, `sequence`, `kind`, `parent_id`, `locator` e `properties`.

- `sequence` é um inteiro contíguo a partir de zero; zero é a raiz `file`.
- `parent_id` é `null` apenas na raiz. Nos demais objetos, deve apontar para um objeto anterior do mesmo arquivo.
- `id` é `obj:` seguido do SHA-256 da representação JSON canônica dos outros campos do objeto. Essa identidade inclui posição, conteúdo observado, origem e pertencimento.
- `locator.type` identifica bytes, JSON Pointer, linha CSV, membro OOXML/ZIP, página/XObject PDF, frame de imagem, intervalo de amostras, stream ou frame de mídia.
- `properties` contém apenas os campos observáveis previstos para aquele tipo e protocolo.

A serialização canônica usa chaves ordenadas, separadores compactos, escapes ASCII e recusa números não finitos. Para a mesma origem e as mesmas observações, o documento possui os mesmos IDs e bytes canônicos, independentemente da geração BN1_2 criada por `--force`.

Exemplo **abreviado** de uma linha de texto:

```json
{
  "source_id": "<geração Hot Hub>:1_aB7.txt",
  "sequence": 1,
  "kind": "text_line",
  "parent_id": "obj:<hash da raiz>",
  "locator": {"type": "bytes", "start": 0, "end": 4, "line": 1},
  "properties": {"text": "one\n"}
}
```

O arquivo publicado inclui também `id` e o envelope completo.

## Curadoria e G_P

A Curadoria valida o esquema, vínculo da origem, rota, inventário, tipos, propriedades, localização, IDs, sequência e pais. Intervalos de bytes não podem sair do comprimento original. Pais precisam preceder filhos, impedindo ciclos e objetos órfãos. Campos inesperados são recusados.

O G_P contém `schema: mimir.provenance.v1` e uma lista ordenada `edges`. Cada aresta contém exatamente `subject`, `predicate` e `object`.

| Predicado | Relação observável |
| --- | --- |
| `derived_from` | Objeto → identidade da origem Hot Hub. |
| `contains` | Pai → filho estrutural. |
| `precedes` | Irmão anterior → próximo irmão, em ordem de observação. |

O grafo esperado é reconstruído dos objetos durante a verificação e deve coincidir exatamente com o grafo armazenado. Relações ausentes, extras, entre origens ou semânticas são recusadas. Palavras como “explica” ou “contradiz” presentes no texto do usuário continuam sendo conteúdo textual; não se tornam predicados G_P.

## BN1_2: envelope e publicação

`BN1_2/manifest.json` contém exatamente:

- `schema: mimir.bn1_2.v1` e `representation_schema: mimir.structural.v1`.
- `generation`: UUID hexadecimal da geração estrutural.
- `upstream`: geração e fingerprint SHA-256 do manifesto Hot Hub canônico verificado.
- `profile`: versão do pipeline, limites, modo estrito e versões/disponibilidade dos leitores opcionais.
- `records`: inventário nome canônico → arquivo sequencial `<posição>.json` e seu SHA-256.
- `summary`: contagens de arquivos, objetos, arestas, protocolos completos e opacos.

Os nomes de registro são derivados da posição dos nomes canônicos ordenados. Não são usados caminhos fornecidos pelo conteúdo do arquivo. A geração deve conter exatamente o inventário esperado; arquivos extras, ausentes e links simbólicos são recusados.

Publicação:

1. Verifica o Hot Hub e obtém um snapshot estável.
2. Cria uma geração nova, observa e escreve cada documento, sincronizando os arquivos.
3. Verifica todos os documentos, hashes, resumos, limites e grafo.
4. Confirma que o snapshot Hot Hub ainda é o mesmo.
5. Sincroniza os diretórios e substitui atomicamente `manifest.json`.
6. Persiste `BN1_2_READY`.

Uma falha anterior à substituição conserva o manifesto anterior. Uma falha depois da publicação, ao registrar o estado, pode deixar um manifesto válido com estado desatualizado: repetir `transform` verifica a saída e corrige o estado. Pastas órfãs não são saída publicada.

`transform` reutiliza a geração apenas se os dados e o vínculo estiverem íntegros e o perfil coincidir. Mudanças de limites, modo estrito, versão do pipeline ou disponibilidade/versão dos leitores causam uma nova execução. `--force` também gera outra publicação. Não há coleta automática das gerações antigas.

## Limites e uso pela API

```python
from pathlib import Path
from BN1_1.Pacote.cache import Pacote
from Transformer_Core.structural import Limits

pacote = Pacote(Path("/tmp/meu-ciclo"))
manifest = pacote.transform(
    Limits(max_file_bytes=32 * 1024 * 1024, max_nodes=5000),
    strict=True,
)
verified = pacote.verify_transform()
origins = pacote.inspect_transform()
document = pacote.inspect_transform(origins[0]["source"])
```

O exemplo pressupõe um ciclo criado com arquivos enviados. Para criar um ciclo, use `pacote.open()` e `pacote.add([Path(...)])` antes de transformar.

Os valores padrão de `Limits` são 64 MiB por arquivo, 10.000 objetos, 2.000.000 caracteres, 128 MiB expandidos declarados em ZIP e 25.000.000 pixels por frame. Todos devem ser inteiros positivos.

O limite de arquivo é conferido antes da reconstrução, após a verificação do Hot Hub. Excedê-lo interrompe o lote e conserva os bytes. Limites durante a observação descartam todas as observações do arquivo e produzem saída opaca, ou recusam o lote em modo estrito.

OOXML não extrai arquivos no disco, recusa caminhos com traversal, membros duplicados e declarações XML de entidades/DOCTYPE; links externos de planilhas não são acessados. A implementação não executa macros ou fórmulas. Leitores multimídia trabalham em memória sobre os bytes verificados, sem abrir URLs fornecidas pelo conteúdo.

A partir da v0.3.0, os protocolos executam em workers separados com limites POSIX de CPU/memória/saída e timeout. O controle de auditoria Python não é uma sandbox de kernel para bibliotecas nativas. A ingestão original conserva sua leitura integral por arquivo. Consulte os [controles e limites do Semantic Core](semantic-core.md).

## Validação desta versão

Os testes cobrem a integração com PDF/WAV/MKV/XLSX reais, linhas UTF-8, JSON Pointers, CSV multilinha, DOCX, fórmulas XLSX, imagens, limites, protocolo ausente, formato inválido e saída opaca. Também verificam idempotência, perfil, concorrência, CLI, corrupção, traversal, links simbólicos, relações semânticas indevidas, mudança de geração Hot Hub e interrupção de publicação.

O exemplo de quatro formatos produz 83 objetos e 235 arestas: 2 objetos PDF, 2 WAV, 27 XLSX e 52 MKV. As raízes estão incluídas na contagem. `verify` confere o estado publicado; a reconstrução byte a byte continua sendo responsabilidade do Hot Hub.
