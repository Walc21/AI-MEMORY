# AI MEMORY / Mimir

Ingestão local de arquivos com originais preservados e uma representação comum de **campos de valores, eixos e tempo**. O armazenamento não separa arquivos por extensão, formato ou modalidade. A entrada pública é `python -m input`.

O Hot Hub agora faz duas operações: mantém uma cópia verificada do lote e publica representações numéricas dos conteúdos que consegue decodificar. Um formato desconhecido permanece disponível como bytes, com estado explícito de **não modelado**. Não é apresentado como uma transformação concluída.

## Fluxo implementado

```mermaid
flowchart TB
    I["Input · janela manual"] --> P["Pacote · originais"]
    P -->|"somente n"| N["Namer"]
    N -->|"1_ID até n_ID"| P
    P -->|"nomes, bytes e integridade"| B["BBN1_1 · armazenamento plano"]
    B --> H["Hot Hub"]
    H --> O["Originais verificados"]
    H --> F["Campos numéricos e tempo"]
    F --> M["Manifesto completo do lote"]
    O --> M
```

1. `open` abre a janela; `add` recebe arquivos regulares de qualquer formato. Cada envio é uma entrada, mesmo com bytes iguais.
2. `close` fecha a janela. O Pacote conta o lote e envia somente `n` ao Namer.
3. O Namer gera um ID de três caracteres alfanuméricos por lote e devolve os stems `1_ID` até `n_ID`. O Pacote associa os stems aos arquivos, salva a associação e renomeia. O sufixo original é preservado no nome, sem determinar o destino ou o decodificador.
4. O BBN1_1 recebe a lista diretamente do Pacote, requisita os bytes e verifica os hashes de nome e conteúdo. Todos os arquivos ficam na mesma pasta.
5. O Hot Hub recebe os originais, verifica o lote e gera campos numéricos a partir do conteúdo. Amostras, tempos, diferenças e proveniência são publicados em uma geração verificada.
6. O BBN1_1 libera suas cópias somente depois da publicação e da confirmação de dois conjuntos completos de originais: Pacote e Hot Hub.

**Sorter, IDD e partições por extensão foram removidos.** As fontes arquiteturais de 28/09/2026 descrevem a versão anterior; o [contrato de campos](docs/temporal-fields.md) registra a alteração e os limites da implementação atual.

## O que a representação faz

| Entrada | Representação implementada | Tempo |
| --- | --- | --- |
| Imagem estática reconhecida | Planos de amostras na resolução e profundidade fornecidas pelo decodificador; sem conversão automática para RGB de 8 bits | Campo constante no tempo; variação temporal zero |
| Áudio decodificável | Amostras e canais nativos, em blocos; diferenças entre amostras e entre blocos | Taxa de amostragem e timestamps nativos |
| Vídeo decodificável | Todos os quadros das trilhas suportadas; planos nativos e diferenças entre quadros | Timestamps racionais reais; não fixa 24 fps |
| PDF com texto extraível | Um vetor de pontos de código Unicode por página; ordem devolvida pelo extrator, sem geometria de página | Página tratada como instantâneo estático |
| XLSX com células numéricas | Uma grade numérica por aba, com eixos linha e coluna; os valores são publicados em `float64` | Aba tratada como instantâneo estático |
| Tensor NumPy numérico (`.npy`) | Eixos e dtype preservados, sem projetar os dados em três dimensões | Não presume que algum eixo seja tempo |
| Conteúdo não suportado, corrompido ou acima dos limites de derivação | Referência íntegra aos bytes originais e motivo de não modelagem | Não inventa um relógio |

O decodificador é escolhido por sondagem do conteúdo, sem classificação por extensão. A cobertura de mídia depende do FFmpeg incluído no PyAV e dos layouts numéricos suportados por este projeto. PDFs digitalizados sem texto extraível, planilhas XLSX com células vazias, fórmulas, texto, booleanos ou valores fora dos limites admitidos, documentos sem adaptador e outros conteúdos sem cobertura permanecem como bytes não modelados. Arquivos ZIP genéricos não são tratados como planilhas. Não há OCR, execução de programas ou conversão especulativa para imagens.

Um arquivo pode conter várias modalidades. Trilhas não modeladas aparecem no registro e tornam o resultado `partial`. Formatos de pixels não suportados ou falhas de decodificação deixam o resultado `opaque`, sem publicar uma sequência numérica truncada como se estivesse completa.

## Executar

Requer **Python 3.10+ em POSIX**, NumPy, PyAV, pypdf e openpyxl. O exemplo também usa ReportLab para criar seu PDF de teste. O PyAV normalmente fornece FFmpeg em seu wheel; não é necessário chamar o programa `ffmpeg` pela linha de comando.

```bash
python -m pip install -r requirements.txt
python -m input open
python -m input add /caminho/imagem.png /caminho/audio.wav /caminho/video.mkv
python -m input add /caminho/arquivo-sem-extensao
python -m input close
python -m input status
```

`close` informa quantos arquivos foram decodificados, parcialmente modelados ou mantidos como opacos. `HUB_READY` significa que o lote foi preservado e que todos os registros foram publicados; **não significa que todos os formatos foram decodificados**.

Uma nova chamada a `close` retoma uma execução interrompida sem gerar outro ID nem refazer o sorteio. Ela também verifica os originais e os derivados e pode reconstruir dados alterados a partir do Pacote íntegro. Cada diretório de execução aceita um ciclo; escolha outro `MIMIR_RUNTIME_DIR` para um lote independente.

## Demonstração reproduzível: quatro formatos, um ciclo

O gerador [`examples/hot_hub_four_formats.py`](examples/hot_hub_four_formats.py) cria entradas **sintéticas**: uma página PDF com uma frase, cinco segundos de áudio PCM que simula ruído de tráfego, cinco segundos de estrada desenhada em vídeo Matroska/FFV1 (50 quadros a 10 fps) e uma aba XLSX de 5 linhas por 5 colunas, preenchida de 1 a 25. São dados de teste, não uma gravação nem uma filmagem reais.

**Matemática comum.** Cada fonte gera campos amostrados `F:D×T→ℝᶜ`, com domínio e relógio declarados. Para duas amostras comparáveis, o código usa `ΔF=F₂−F₁` e `taxa=ΔF/(t₂−t₁)`. Não inventa tempo para o eixo de bytes nem trajetória física a partir de pixels. Aqui as especializações são:

| Fonte | Campo amostrado | Coordenadas e operação |
| --- | --- | --- |
| PDF textual | `F(i)=ord(caractere_i)` | `i` é o índice no texto extraído da página; instantâneo estático, variação temporal zero. |
| Rua (WAV, 5 s) | `F(n,c)=amostra PCM` | `t_n=n/8000 s`; diferenças sucessivas em cada canal e taxa média `ΔF/(1/8000 s)`. |
| Estrada (MKV, 5 s) | `F(k,y,x)=valor cinza do quadro` | `t_k=k/10 s`; diferença entre quadros na mesma posição da grade e taxa média `ΔF/(1/10 s)`. |
| Planilha (XLSX, 5×5) | `F(r,c)=valor numérico da célula` | Linha e coluna são eixos espaciais discretos; instantâneo estático, sem relógio de eventos. |

**Mesmo script de processamento para os quatro arquivos**, sem `if` por extensão ou tipo de mídia. Os adaptadores são parte do Hot Hub e são escolhidos pelo conteúdo; [`calculus.py`](Transformer_Core/Fields/calculus.py) implementa a diferença e a taxa usadas para as trilhas temporais. O bloco imprime o estado e os eixos/formatos dos campos; os nomes temporários e hashes mudam em cada execução.

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from BN1_1.Pacote.cache import Pacote
from examples.hot_hub_four_formats import create_examples

with TemporaryDirectory() as directory:
    base = Path(directory)
    files = create_examples(base / "inputs")
    pacote = Pacote(base / "runtime")
    pacote.open()
    pacote.add(files)          # os quatro seguem a mesma chamada
    pacote.close()             # publica os campos e preserva os originais

    hub = base / "runtime/Transformer_Core/Hot_Hub"
    manifest = json.loads((hub / "fields.json").read_text())
    print(manifest["summary"])
    for relative in manifest["records"].values():
        record = json.loads((hub / "representations" /
                             manifest["generation"] / relative).read_text())
        for field in record["fields"]:
            print(record["adapter"], record["status"], field["axes"],
                  field["chunks"][0]["samples"]["shape"], len(field["chunks"]))
```

**Antes → depois**, observado com o gerador e o script acima (`decoded=4, partial=0, opaque=0`):

| Antes: arquivo de entrada | Depois: campo publicado no Hot Hub |
| --- | --- |
| `nota.pdf`: 1 página, “Relato: a rua tem carros e pedestres.” | `pypdf`, `decoded`; `page_1_text`, eixo `[codepoint]`, vetor de 38 inteiros Unicode. A reconstrução do vetor produz a frase e uma quebra de linha final. |
| `rua.wav`: mono PCM 16 bits, 8.000 amostras/s × 5 s | `pyav`, `decoded`; eixo `[sample, channel]`, 40.000 amostras em 79 blocos. `step=[1,8000]`; o primeiro bloco tem forma `[512,1]` e começa em `[0,1]` s. |
| `estrada.mkv`: 50 quadros de uma estrada desenhada, 10 fps × 5 s | `pyav`, `decoded`; eixo `[y,x]`, 50 blocos de `[24,32]`. Primeiro PTS `[0,1]` s, último `[49,10]` s, intervalo sucessivo `[1,10]` s. |
| `medidas.xlsx`: aba `Medidas`, 5×5 números de 1 a 25 | `openpyxl`, `decoded`; `sheet_1`, eixos `[row,column]`, array `[5,5]`, primeira linha `[1,2,3,4,5]` e última `[21,22,23,24,25]`. |

Cada registro conserva a referência ao original e seu SHA-256. A matemática do campo é compartilhada; **a extração dos bytes requer um adaptador por codificação**. O exemplo mostra quatro codificações cobertas, não uma conversão sem decodificadores de qualquer arquivo. O texto do PDF não traz coordenadas confiáveis, a planilha só aceita grades numéricas no adaptador atual, o áudio não é transcrito e o vídeo não identifica veículos ou movimento de objetos.

## Dados e integridade

| Local no diretório de execução | Conteúdo |
| --- | --- |
| `cycle.json` | Estado do ciclo, associação dos nomes e resumo das representações |
| `BN1_1/Pacote/files/<entrada>/<nome>` | Primeiro conjunto de originais |
| `BN1_1/Namer/` | Mensagem contendo apenas `n` e resposta com os stems |
| `BN1_1/BBN1_1/<nome>` | Cópias intermediárias; removidas após confirmação |
| `Transformer_Core/Hot_Hub/data/<nome>` | Segundo conjunto de originais, em uma pasta plana |
| `Transformer_Core/Hot_Hub/manifest.json` | Inventário e SHA-256 dos originais |
| `Transformer_Core/Hot_Hub/representations/<geração>/<objeto>/` | Registro JSON e arrays `.npy`; divisão por identidade, nunca por formato |
| `Transformer_Core/Hot_Hub/fields.json` | Geração publicada, proveniência, inventário dos derivados e contagens |

O diretório padrão é `.mimir-runtime/`, ignorado pelo Git. JSON descreve os contratos; arquivos `.npy` armazenam arrays sem pickle. Os arquivos originais continuam sendo arquivos binários, preservados byte a byte. Não há necessidade de um banco SQL ou YAML de extensões.

A geração é publicada por último, após a gravação dos registros e arrays. Uma falha de escrita impede a conclusão e a limpeza do BBN1_1. Gerações anteriores ou órfãs de uma interrupção podem permanecer no armazenamento; somente a apontada por `fields.json` está ativa. Não existe coleta automática desses derivados nesta versão.

A garantia de preservação refere-se aos originais. Os campos retêm as amostras produzidas pelo decodificador, com metadados de precisão; não substituem todos os metadados, estruturas e comportamentos do arquivo original. Diferenças de inteiros de até 32 bits usam `int64`; diferenças e taxas em ponto flutuante estão sujeitas a arredondamento. Nenhuma interpretação visual, semântica ou trajetória é inferida automaticamente.

## Compatibilidade e limites

- O layout atual é a versão 2. Ciclos anteriores são recusados antes de alterar o estado ou os arquivos. Preserve-os e reenvie seus originais para outro `MIMIR_RUNTIME_DIR`; não há migração automática.
- Os limites padrão por arquivo são 32 milhões de elementos por array, 1 GiB de arrays derivados e 100 mil quadros/blocos por trilha. Ao atingir um limite, o arquivo fica opaco, com motivo registrado, e os originais são preservados. `FieldStore(..., limits=Limits(...))` permite ajustar esses valores pela API Python.
- Os limites controlam a publicação de derivados; não constituem um sandbox nem garantem um teto rígido de memória dentro dos decodificadores nativos. O processamento mantém um quadro/bloco e seu predecessor por campo, em vez de carregar o vídeo inteiro.
- Não há expiração automática do Pacote nem encerramento final do ciclo. A limpeza controlada do BBN1_1 conserva dois conjuntos íntegros; não é uma garantia contra falha simultânea do dispositivo físico.

## Verificação

```bash
python -m compileall -q input BN1_1 Transformer_Core examples tests
python -m unittest discover -s tests -v
```

A CI executa as regressões em Python 3.10 e 3.12. Os testes cobrem armazenamento plano, concorrência, identidade estável, recuperação, integridade, imagens RGB e cinza de 16 bits, áudio estéreo, vídeo com intervalos variáveis, múltiplas trilhas, gradientes, tensores, entradas opacas e publicação interrompida.

O [contrato técnico](docs/temporal-fields.md) explica a formulação matemática, as diferenças entre variação e movimento e como ler os resultados.
