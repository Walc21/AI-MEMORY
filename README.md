# AI MEMORY / Mimir

[![CI](https://github.com/Walc21/AI-MEMORY/actions/workflows/ci.yml/badge.svg)](https://github.com/Walc21/AI-MEMORY/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)
[![Release](https://img.shields.io/badge/release-v0.5.0-purple)](https://github.com/Walc21/AI-MEMORY/releases/tag/v0.5.0)

**Memória local auditável: bytes → estrutura → evidências → afirmações temporais → consulta com fontes.**

A **v0.5.0 corrige a passagem indevida de um resultado de busca para uma resposta factual**. A consulta exige suporte para a informação solicitada, conserva os vínculos entre registros e campos em JSON/tabelas e devolve trechos concisos com fontes. Uma pergunta sobre a cor de Alice se abstém quando a memória contém apenas seu emprego. Negação, histórico e limites de contexto são verificados antes da resposta. A mesma regra vale para Python, CLI, MCP e respostas enfileiradas no Drive.

O pipeline original continua: preservação por arquivo no Hot Hub, chunks de 1.024 bytes, estrutura G_P/BN1_2 e interpretação semântica separada. A decisão de bytes/chunks de 29/09 substituiu a organização inicial por extensão; esta release não restaura Sorter/IDD. Consulte o [contrato de resposta e aceite](docs/grounded-memory-v0.5.0.md), o [manual](docs/reference/semantic-core-manual.pdf), o [guia de Drive/MCP](docs/drive-mcp.md) e a [auditoria anterior](docs/runtime-audit-v0.4.1.md).

```mermaid
flowchart LR
    D[Drive / Entrada] --> IO[MCP I/O ou API OAuth]
    IO --> I[Input / Pacote]
    I --> N[Namer / BBN1_1]
    N --> H[Hot Hub: bytes]
    H --> T[Route Hub / protocolos]
    T --> C[Frankenstein / Curadoria]
    C --> GP[G_P: estrutura e origem]
    GP --> B[BN1_2 verificado]
    B --> E[EvidenceAnchor / G_M]
    E --> S[Semantic Core / G_S]
    S --> R[Busca híbrida / contexto]
    R --> V[Validação de suficiência]
    V --> O[Output Storage / resposta ou abstenção]
    O --> Q[Outbox / recibo verificado]
    Q --> DS[Drive / Saída]
    R --> MCP[MCP de memória]
    MCP <--> AI[Harness de IA]
```

## Comece agora

Requer **Python 3.10+ e POSIX/Linux**, com SQLite FTS5. O núcleo funciona offline, sem chave de API ou serviço externo.

```bash
git clone https://github.com/Walc21/AI-MEMORY.git
cd AI-MEMORY
python -m venv .venv
source .venv/bin/activate
python -m pip install .

mimir episode 'João trabalha na OpenAI em 2023.'
mimir query 'Onde João trabalha?' --text
mimir verify
```

Resposta: `Segundo a fonte [1]: João trabalha na OpenAI em 2023.`, acompanhada da referência à fonte. Sem `--text`, a consulta retorna JSON com evidências, afirmações, tempos, plano de busca e contexto. Todos os comandos também aceitam `python -m mimir`.

Para ingerir arquivos e salvar uma consulta:

```bash
mimir --runtime /tmp/mimir-lote-1 run /caminho/notas.txt /caminho/dados.json \
  --query 'Onde João trabalha?'
mimir query 'Onde João trabalha?' --save
```

Cada `run` usa um **diretório de ciclo novo**. Lotes distintos entram no mesmo ledger, mantendo seus históricos. A memória padrão fica em `.mimir-memory/default`; use `--memory-dir /caminho/memoria --namespace projeto` antes do comando para escolher outra. `MIMIR_MEMORY_DIR`, `MIMIR_NAMESPACE` e `MIMIR_RUNTIME_DIR` configuram os padrões.

Para avançar um ciclo já construído na v0.2.0:

```bash
mimir --runtime /caminho/do/ciclo semantic
mimir verify
```

O Semantic Core verifica e consome o BN1_2 existente. Os contratos `mimir.byte-chunks.v1`, `mimir.structural.v1`, `mimir.bn1_2.v1`, o layout 3 e os comandos `python -m input` continuam compatíveis. Sem BN1_2, a ingestão e transformação são concluídas automaticamente.

## Google Drive e harnesses de IA

Instale as integrações no ambiente Python que executará os MCPs:

```bash
python -m pip install '.[mcp,drive,structural]'
mimir --memory-dir /caminho/absoluto/memoria mcp config --client json
# Para Codex: substitua --client json por --client codex.
```

A configuração gerada contém comandos e caminhos absolutos para **`mimir-io`** e **`mimir-memory`**. Cole-a na configuração MCP do harness e reinicie-o. Ambos usam o mesmo namespace. O MCP de memória oferece `memory_query`, `memory_explain`, `memory_status` e `memory_working`; `--allow-write` na geração habilita episódios e atualização da Working Memory. O MCP de I/O recebe dados por `io_receive`, ingere fontes materializadas e confirma publicação no Drive.

**Conta do plugin desta sessão:** use o [plugin local Mimir Memory](plugins/mimir-memory) e sua [skill de ponte](plugins/mimir-memory/skills/drive-memory/SKILL.md) junto ao Google Drive autenticado. O host cria/verifica `AI MEMORY - Mimir/Entrada` e `AI MEMORY - Mimir/Saída`, materializa downloads no staging e confirma uploads. O token protegido da sessão permanece no host. Essa ponte precisa do Drive com leitura/escrita e acesso ao mesmo filesystem dos MCPs; não roda sozinha em segundo plano.

**Sincronização contínua pela API oficial**, autorizando a mesma conta Google:

```bash
# Crie um cliente OAuth Desktop no Google Cloud com Drive API habilitada.
# Guarde o JSON do cliente fora do repositório e conclua o consentimento no navegador.
mimir drive auth --client-secrets /caminho/privado/client_secret.json
mimir drive bootstrap
mimir drive sync
mimir drive sync --watch --interval 30
```

`bootstrap` cria/reutiliza as pastas privadas e vincula a conta; uma configuração feita pela ponte pode ser utilizada pela API se a conta for a mesma. `sync` processa os arquivos diretamente na Entrada, preserva revisões anteriores e publica a outbox na Saída. Não apaga os originais. `io status` mostra a situação real; `PENDING` é uma saída local, `DELIVERED` é uma publicação remota verificada. Use `mimir query 'PERGUNTA' --publish` ou `io_publish_query` para enfileirar uma resposta citada. Instale os leitores opcionais para os formatos desejados. OCR, modelos locais e assinatura também são aceitos em `drive sync`.

Para HTTP autenticado, empacotamento do plugin, exportação de memória e operação contínua, siga o [guia completo](docs/drive-mcp.md).

## Memória e consulta

| Comando | Uso |
| --- | --- |
| `run ARQUIVOS... [--query PERGUNTA]` | Executa o pipeline completo; a consulta opcional salva a saída. |
| `semantic [--force]` | Consome BN1_2; reutiliza a geração do mesmo perfil ou acrescenta nova inferência. |
| `query PERGUNTA [--text] [--save]` | Combina BM25, vetores, entidades, tempo, RRF e PageRank personalizado. |
| `query PERGUNTA --at 2020` | Consulta o tempo de validade de uma informação. |
| `query PERGUNTA --as-of TIMESTAMP` | Consulta o que já estava registrado naquele instante, com fuso ISO 8601. |
| `query PERGUNTA --history [--audit]` | Inclui afirmações substituídas; `--audit` permite recuperar passagens inativas. |
| `inspect [COLEÇÃO]` | Lista assertions, mentions, entities, inference_runs, episodes, reflections etc. |
| `explain ASSERTION_ID` | Percorre afirmação → inferência → evidência → origem e bytes. |
| `export-source CONTENT_ID DESTINO` | Exporta os bytes canônicos para um arquivo novo. |
| `update NOVA_ASSERTION ANTIGA_ASSERTION` | Acrescenta supersession; `--relation retracts` registra retratação. |
| `resolve MENTION_ID 'Nome' --type person` | Registra uma resolução humana reversível da entidade. |
| `episode TEXTO [--type interaction]` | Preserva interações como fontes e episódios persistentes. |
| `working --json '{"objective":"..."}'` | Configura identidade, objetivo, projeto, restrições e compromissos. |
| `consolidate [--max-chars 1600] [--type reflective]` | Cria resumos extrativos limitados, sem duplicar frases nem cortar negação/unidades. |
| `reindex` | Reconstrói as projeções sem alterar a memória canônica. |
| `gc [--apply]` | Lista/remove tentativas órfãs e índices; preserva a cadeia canônica e os bytes. |
| `verify` | Verifica gerações, histórico, hashes, grafos, inferências, spans e fontes. |
| `eval [--dataset ARQUIVO.json]` | Executa avaliações locais e adaptadores LongMemEval/LoCoMo. |

Assertions registram **o que a fonte relata**, com polaridade, `valid_time` e `transaction_time`. Conflitos permanecem visíveis; atualizações acrescentam relações, sem apagar afirmações anteriores. A resolução automática usa nomes normalizados exatos como candidatos. Aliases ambíguos requerem resolução explícita.

A extração padrão reconhece padrões fechados em português e inglês: trabalho/entrada em organização, residência, nascimento, responsabilidade e descrições com “é/is”, incluindo negação e datas explícitas. Texto fora desses padrões permanece pesquisável. A camada de resposta aceita relações verificadas, campos explícitos de um registro e atributos declarativos cujo sujeito/campo apareçam no mesmo trecho. Comparações, agregações, pedidos exaustivos e correferência implícita não são inferidos: podem causar abstenção mesmo com informação potencialmente relevante. Um modelo opcional não pode contornar o teste de suficiência.

**Contrato do resultado:** `hits` contém candidatos de busca; `answer_evidence` contém as citações que sustentam a resposta; `claims` contém apenas afirmações verificadas e pertinentes; `sufficiency` explica suporte ou abstenção. Use `answer_evidence` e `context` ao fornecer dados ao harness. `abstained=true` exige abstenção, inclusive quando há hits. Os campos novos são aditivos ao schema v1; nenhum arquivo original é alterado.

JSON preserva valores numéricos, booleanos, nulos, caminhos e fronteiras de registros. CSV/TSV e tabelas DOCX usam a primeira linha como cabeçalho quando seus rótulos são não vazios e distintos; XLSX conserva colunas esparsas e resultados existentes, sem executar fórmulas. As citações identificam `structural_projection` com os nós/localizadores usados para reconstruir o registro. Valores ausentes e fórmulas sem resultado armazenado não viram respostas.

```bash
mimir --runtime /tmp/lote-unico run notas.txt registros.json tabela.csv documento.pdf
mimir query 'Qual orçamento do projeto Aurora?' --text
mimir query 'Qual senha do projeto Aurora?' --text
# Sem senha nos registros: "Não encontrei evidência suficiente na memória para responder."
mimir consolidate --max-chars 1600
```

Fontes externas com `provider`, `file_id` e `revision` usam a revisão mais recente observada, priorizando `modified_time`. `--history` conserva as revisões anteriores; `--as-of` usa somente o conhecimento disponível naquele instante. Arquivos locais independentes não são substituídos por nome. Resumos são derivados e limitados; o canon conserva as fontes completas para não perder informação na compressão.

A busca vetorial padrão usa **feature hashing de palavras e trigramas**, que mede semelhança lexical. Embeddings neurais são opcionais. As projeções SQLite são reconstruídas automaticamente quando ausentes ou corrompidas. Consultas trabalham com o ledger local inteiro; corpora muito grandes exigem dimensionamento e um índice vetorial especializado.

## Formatos e modelos opcionais

Texto UTF-8, JSON, CSV/TSV, DOCX, XLSX e WAV PCM usam a biblioteca padrão. Para PDF, imagens e outros arquivos de áudio/vídeo:

```bash
python -m pip install '.[structural]'
mimir --runtime /tmp/mimir-multimodal run documento.pdf imagem.png audio.wav video.mkv
```

Os protocolos preservam texto, localização, sequência, streams, frames e mídia embutida. DOCX cobre o corpo principal; PDF cobre páginas e imagens XObject diretas. A cobertura de leitura consta nos relatórios. Arquivos opacos conservam seus bytes e motivo de falha, sem inventar conteúdo. Consulte o [contrato estrutural](docs/structural-pipeline.md).

**OCR:** instale Tesseract e os idiomas no sistema, além das dependências Python:

```bash
# Debian/Ubuntu
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-por
python -m pip install '.[ocr]'
mimir --runtime /tmp/mimir-ocr run imagem.png digitalizado.pdf \
  --ocr --ocr-language por+eng --strict-multimodal
```

OCR registra linhas, caixas em pixels e confiança. **ASR:** instale `pip install '.[asr]'` e forneça um diretório já baixado de modelo compatível com faster-whisper usando `--asr-model /caminho/modelo`. A transcrição usa CPU/int8 e registra intervalos de tempo/amostras no PCM reamostrado a 16 kHz. Os pesos são identificados por hash; o comando não baixa modelos.

**Extração por LLM e visão:** instale e execute Ollama localmente, preparando os modelos escolhidos no próprio Ollama:

```bash
mimir --runtime /tmp/mimir-llm run notas.txt --extractor ollama --model SEU_MODELO
mimir --runtime /tmp/mimir-visao run imagem.png video.mkv \
  --vision-model SEU_MODELO_DE_VISAO --video-stride 30
```

O endpoint padrão é `http://127.0.0.1:11434`; somente endpoints HTTP de loopback são aceitos. A revisão do modelo e o prompt entram no perfil. A saída do extractor passa por validação de JSON, entidades, datas, quotes e offsets; uma evidência inventada impede a publicação. Descrições visuais e transcrições são observações derivadas, identificadas nas citações, e não alteram G_P. Vídeo aplica OCR/visão em um frame a cada `--video-stride` frames observados.

**Embeddings neurais:** instale `pip install '.[vectors]'`, prepare um modelo SentenceTransformers local e use `query ... --embedding-model /caminho/modelo` ou `reindex --embedding-model /caminho/modelo`. Um identificador de modelo em cache exige `--embedding-revision` imutável de 40 caracteres. O carregamento é offline. Cada perfil tem seu próprio índice; mudar embeddings não modifica o ledger.

Modelos de ASR, visão, extração geral e embeddings neurais não são distribuídos neste repositório. Sem uma modalidade disponível, seu erro é registrado; `--strict-multimodal` impede a publicação daquele lote. OCR real é testado; ASR e os modelos Ollama têm testes de contrato com fixtures locais, sem alegação de qualidade de modelos não executados.

## Persistência, auditoria e proteção

```text
.mimir-memory/<namespace>/
  policy.json                     # ACL POSIX / chave pública confiável
  manifest.json                   # geração ativa, com publicação atômica
  sources/<sha256>.bin             # bytes originais endereçados por conteúdo
  generations/<id>/               # ledger completo, manifesto, G_M e G_S
  indexes/<generation>/<profile>/  # SQLite FTS5, vetores, entidades, tempo e grafo
  working.json                    # configuração de memória de trabalho
  Output_Storage/output.json      # resposta citada ligada à geração/fingerprint
```

`content_id` identifica bytes iguais entre ciclos. `EvidenceAnchor` mantém identidade por conteúdo/localizador/protocolo; bindings conservam cada ocorrência histórica e seu `source.id`. Os originais e snapshots estruturais são arquivados na memória: episódios continuam verificáveis após a remoção de seus runtimes temporários.

Namespaces têm ACL por UID POSIX (`policy --readers ... --writers ...`); permissões do sistema de arquivos também precisam permitir o acesso desejado. O processo local deve ser executado como usuário autorizado. Não há servidor público, autenticação web ou criptografia de disco incorporados. Use proteção e backup do sistema operacional para o diretório canônico.

Assinaturas opcionais:

```bash
python -m pip install '.[signing]'
mkdir -p .mimir-keys
mimir keygen .mimir-keys/memory.pem
mimir episode 'Alice works at Acme.' --signing-key .mimir-keys/memory.pem
```

Depois da primeira assinatura, novas gerações exigem a chave confiável. Operações `semantic`, `episode`, `update`, `resolve` e `consolidate` aceitam `--signing-key`. Proteja a chave privada e `policy.json`; hashes detectam corrupção, e Ed25519 autentica as gerações contra a chave pública fixada.

Parsers e extractors executam em processos separados com timeout, limites POSIX de CPU/memória/saída, bloqueio de execução arbitrária e escrita restrita ao temporário por auditoria Python. Rede é bloqueada, exceto loopback quando o modelo local foi escolhido. O limite padrão do worker é 60 s, 2 GiB e 64 MiB de saída; o lote semântico limita 2 milhões de caracteres, 20 mil assertions e 200 mil registros. A API aceita `SemanticLimits`.

Esse controle de processos **não é uma sandbox de kernel para código nativo**. Para documentos hostis em ambientes compartilhados, execute sob uma sandbox/container do sistema operacional. Conteúdo de documentos é tratado como dado, sem execução de instruções, scripts ou ferramentas. O Hot Hub continua no contrato v1 e reconstrói um arquivo inteiro por vez.

## API, demonstração e validação

```python
from pathlib import Path
from mimir import Memory

memory = Memory(Path(".mimir-memory"), namespace="projeto")
memory.episode("Alice works at Acme in 2020.")
result = memory.query("Where Alice works?", budget_chars=8000)
print(result["answer"])
print(memory.explain(result["claims"][0]["assertion_id"]))
```

```bash
python -m examples.semantic_memory
mimir eval
python -m pip install -r requirements.txt -r requirements-demo.txt '.[structural,ocr,signing]'
python -m unittest discover -s tests -v
python -S -m unittest discover -s tests -p 'test_semantic_core.py' -v
```

A CI verifica Python 3.10 e 3.12, o núcleo sem site-packages, OCR, assinaturas, instalação da CLI e regressões do pipeline anterior. A suíte inclui um lote real com **TXT, Markdown, JSON, CSV, TSV, DOCX, XLSX, PDF e PNG/OCR**, 11 perguntas com resposta, 8 sem resposta e fontes distintas para cada informação, além de testes com 1, 40 e 1.000 registros distratores. As verificações julgam a resposta e `answer_evidence`, não apenas os hits. Uma nova versão só é publicada após todos os jobs de CI passarem e a wheel instalada fora do checkout demonstrar resposta e abstenção.

O harness reporta cobertura de extração anotada, Recall@8, MRR, nDCG@8, categorias de tempo/multi-hop/atualização, conflito, abstention, resolubilidade da evidência, reconstrução de índices e latência. As métricas locais usam fixtures e correspondência de evidências; os adaptadores de LongMemEval/LoCoMo **não equivalem aos escores oficiais desses benchmarks**. Os testes não provam ausência universal de erros em linguagem arbitrária, volume ilimitado ou modelos ASR/visão não executados.

Os [contratos e mapa S0–S15](docs/semantic-core.md), [chunks](docs/byte-chunks.md), [pipeline estrutural](docs/structural-pipeline.md), [referência visual](docs/images/mimir-pipeline-2026-09-28.jpg) e [changelog](CHANGELOG.md) detalham a implementação.
