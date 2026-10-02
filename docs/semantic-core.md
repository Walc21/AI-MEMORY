# Semantic Core v0.3.0

A implementação segue o [manual fornecido](reference/semantic-core-manual.pdf), auditado sobre o commit `f2995ca` da v0.2.0. O fluxo completo publica gerações canônicas e fornece consultas locais. O caminho padrão usa regras conservadoras, feature hashing, BM25 e grafo; modelos gerais são selecionados explicitamente.

## Fronteira e identidade

`G_P` continua sendo a proveniência estrutural: `derived_from`, `contains`, `precedes`. A extração semântica não altera seus documentos, IDs, propriedades ou bytes. `occurrences.jsonl` arquiva os documentos BN1_2 integrais e seus vínculos Hot Hub.

| Identidade | Fórmula / escopo |
| --- | --- |
| `source_occurrence_id` | `source.id` original, incluindo geração Hot Hub e nome canônico. |
| `content_id` | `content:` + SHA-256 dos bytes reais. |
| `EvidenceAnchor` | Hash de conteúdo, tipo do objeto, localizador e rota/versão do protocolo. |
| `binding` | Anchor + ocorrência histórica + nó estrutural. |
| Demais registros | Prefixo + SHA-256 do registro canônico e seu schema. |

Reingerir o mesmo conteúdo conserva anchors e entidades candidatas exatas, acrescentando ocorrências/bindings e inferências. Arquivos com bytes iguais permanecem auditáveis como envios distintos. Uma nova versão de protocolo pode mudar o anchor sem alterar `content_id`.

## Ledger e publicação

O manifesto `mimir.semantic-generation.v1` identifica uma geração, exatamente um upstream BN1_2, seu fingerprint, perfil, operação, parent/fingerprint, hashes/contagens de cada coleção e hashes de G_M/G_S. Upstream e Hot Hub correspondente ficam arquivados como registros. Cada geração carrega o ledger completo e preserva os registros de seus parents.

Coleções: `upstreams`, `occurrences`, `anchors`, `bindings`, `observations`, `mentions`, `entities`, `resolutions`, `events`, `propositions`, `assertions`, `inference_runs`, `relations`, `episodes`, `reflections`, `reports`. Schemas e conjuntos exatos de campos estão em `model.py` e `validation.py`. Linhas JSONL são canônicas e ordenadas por identidade.

O processo adquire os locks de ciclo/memória, verifica Pacote/Hot Hub/BN1_2, preserva fontes por conteúdo, extrai, valida e escreve uma nova geração. Após fsync e verificação, substitui atomicamente `manifest.json`. Um ledger canônico corrompido é recusado. Falhas deixam o último manifesto publicado disponível e podem deixar uma tentativa órfã, removível por `gc`.

`verify` percorre parents, detecta reescrita/remoção de registros e confere o inventário exato de arquivos. Bytes devem ter tamanho/hash declarados e reconstituir o hash da serialização JSONL original do Hot Hub. Cada documento deve corresponder ao hash arquivado no BN1_2.

Modelo, revisão, prompt, parâmetros, dependências, limites e código integram o perfil. Repetir upstream/perfil retorna a geração ativa; `--force` acrescenta outra execução. O perfil inclui versão do pacote, commit Git quando disponível e hash do código semântico, inclusive em uma wheel sem `.git`.

## Evidência e inferência

Referências contêm anchor/binding/node, propriedade, intervalo de caracteres, quote e hash do quote. O validator resolve esses campos à propriedade estrutural ou observação derivada e confere a citação exata. Citações expõem conteúdo, ocorrência, fonte, localizador e upstream; observações derivadas também expõem método, confiança e run.

`G_M` projeta menções de entidade/data/relação com `grounded_in` e resoluções candidatas. `G_S` projeta afirmações reificadas, proposições, sujeitos/objetos, conflitos, atualizações e resoluções vigentes. Ambos são recomputáveis a partir do ledger.

Assertions contêm proposição, polaridade, `valid_time`, `transaction_time`, evidências, `epistemic_status=reported`, run e menção. Registram o relato da fonte, sem comprovar sua verdade no mundo. Uma proposição possui sujeito, predicado e objeto tipado (`entity` ou `literal`). Eventos com datas explícitas apontam para sua assertion.

`InferenceRun` registra extractor/versão, modelo/revisão, hash de prompt, parâmetros, código, upstream, timestamp e hash dos registros produzidos. Sua identidade exclui `output_hash` para evitar dependência circular entre run e output. O validator recomputa esse hash, incluindo menções, resoluções, observações, assertions, eventos e relatório associados. Evidências não sustentadas e campos extras rejeitam o lote.

Regras padrão cobrem padrões fechados de frases PT/EN. Não há correferência automática de pronomes nem resolução automática de aliases ambíguos. O adapter Ollama exige JSON, sujeito, objeto, tempo e quote presentes no texto. A validação de spans impede substituição de evidência; a qualidade de relações inferidas pelo modelo requer avaliação própria.

## Histórico, resolução e conflitos

Timestamps usam UTC ISO 8601 canônico. Validade admite início/fim por ano/data ISO e precisão `year`, `day` ou `unknown`; limites ausentes são abertos. `--at` filtra validade; `--as-of` filtra quando a memória já conhecia o registro. A pergunta pode fornecer data; sem indicação, a consulta padrão usa a data atual. `--history` inclui afirmações substituídas e não impõe automaticamente a data atual.

Resoluções registram método, score, features, override humano e tempo. O padrão gera candidatos por nome normalizado exato. Novo override substitui a resolução vigente na projeção, preservando as anteriores. Assertions mantêm os candidatos originais; a projeção liga candidatos à entidade resolvida para recuperação por alias. Não ocorre fusão destrutiva de identidade.

Conflitos automáticos cobrem polaridades opostas sobre a mesma proposição e valores positivos distintos de `born_in` em intervalos sobrepostos. Descrições genéricas com `is` podem coexistir. A detecção conservadora não pretende reconhecer todas as contradições em linguagem natural.

`supersedes` e `retracts` relacionam assertions explicitamente. A consulta corrente exclui o alvo após a atualização; consultas históricas ou anteriores à transação mantêm sua visibilidade. Supersession cíclica é recusada. Conflitos/atualizações invalidam resumos dependentes recursivamente, sem removê-los.

## Retrieval e memória de agentes

SQLite contém passages, FTS5/BM25, entidades, relações, tempos e vetores. Passagens usam até 2.000 caracteres, overlap de 200 e refs exatas; somente assertions com spans sobrepostos pertencem à passagem. Observações multimodais ficam conhecidas na transação de seu próprio run.

O planner registra subconsultas/tempo. Rankings lexical/vetorial/entidade/grafo são fundidos por RRF e reranking por cobertura lexical/evidência. PageRank personalizado conecta assertions por entidades para consultas de múltiplos saltos. O cálculo vetorial padrão percorre as passagens locais; não é um índice ANN.

`VectorIndex` usa palavras/trigramas normalizados em 256 dimensões por padrão. SentenceTransformers opcional carrega pesos locais ou cache com revisão imutável; modelo, dimensão, normalização e versão integram o perfil. Índices separados por geração/perfil podem ser apagados sem perda de conteúdo canônico.

O context pack limita caracteres dos trechos, configuração de trabalho e resumos derivados; metadados de auditoria ficam fora do orçamento de texto. Evidências são dados não confiáveis. A resposta cita afirmações, sinaliza conflito, mostra passagem sem assertion ou se abstém. Não executa ferramentas ou conteúdo do documento.

`episode` materializa uma fonte em ciclo temporário, arquiva bytes/documentos e registra o episódio. `working` guarda campos declarados como configuração do usuário. `consolidate` agrupa episódios/assertions por identidade e cria resumos extrativos hierárquicos; `reflective`, `procedural` e `community` classificam os registros. Não implementa clustering semântico RAPTOR nem descoberta automática de procedimentos. A consulta inclui resumos pertinentes e válidos com suas dependências.

## Multimodalidade e operações

OCR usa Tesseract TSV agrupado por linha, bbox em pixels e confiança média. Suporta frames de imagem/vídeo, imagens embutidas e páginas PDF rasterizadas opcionais. ASR usa faster-whisper local CPU/int8, stream selecionado, segundos e amostras de PCM reamostrado. Visão usa Ollama local e descrição da imagem inteira com confiança declarada pelo modelo. Vídeo aplica stride explícito. Coordenadas derivadas e texto inferido não substituem a origem estrutural.

Adapters exigem executáveis, bibliotecas e pesos opcionais. Relatórios registram perfil, cobertura estrutural, erros e uso; tokens externos indisponíveis são `null`. `--strict-multimodal` recusa falhas; sem ele, erros são publicados explicitamente com as demais evidências válidas. O validator confere bbox, tempos e vínculos upstream/run.

Workers possuem limites POSIX de memória/CPU/arquivo, timeout, IPC JSON limitado e encerramento do grupo de processos em timeout. Auditoria Python bloqueia rede, escrita fora do temporário e executáveis não autorizados; permite Tesseract/FFmpeg e loopback no perfil Ollama. Não isola syscalls de bibliotecas nativas: dados hostis exigem sandbox do sistema operacional.

ACL por UID protege a API local; permissões POSIX são a camada física de proteção e precisam ser configuradas para compartilhamento. O pacote não criptografa dados canônicos. GC conserva parents, coleções e bytes referenciados, eliminando somente gerações órfãs e índices. Não remove histórico por retenção temporal.

Ed25519 opcional assina o fingerprint que cobre coleções e parents. A primeira chave fixa a chave pública em `policy.json`; proteja essa política e a chave privada. Gerações anteriores não assinadas permanecem auditáveis; novas publicações exigem assinatura. A âncora de confiança depende das permissões/backup externos.

## Correspondência com S0–S15

| Etapa | Implementação executável / limites |
| --- | --- |
| S0 | `content_id`, EvidenceAnchor e bindings longitudinais. |
| S1 | Gerações, upstream fingerprint, parent chain e publicação atômica. |
| S2 | Mention, Entity, Event, Proposition, Assertion e InferenceRun. |
| S3 | Validator de schemas, IDs, evidência, run, tempo e proveniência. |
| S4 | SQLite/FTS5 com passagens e refs reconstruíveis. |
| S5 | Regras offline e adapter Ollama local estruturado. |
| S6 | Candidatos exatos, overrides reversíveis e tempo; aliases ambíguos são explícitos. |
| S7 | Conflitos conservadores, supersession, retratação e bitemporalidade. |
| S8 | Interface vetorial, hashing local e SentenceTransformers opcional. |
| S9 | Planner determinístico, RRF, reranking e context pack. |
| S10 | PageRank personalizado sobre G_S. |
| S11 | Episódios persistentes e working memory. |
| S12 | Consolidação extrativa hierárquica e invalidação por dependência. |
| S13 | OCR, ASR e visão derivados, coordenadas/perfis e falhas explícitas. |
| S14 | ACL POSIX, GC conservador, hash chain e Ed25519; isolamento nativo externo. |
| S15 | Fixture por subsistema e adapters de datasets LongMemEval/LoCoMo locais. |

O [Hot Hub v1](byte-chunks.md) é preservado, conforme a recomendação de adiar sua migração para blobs binários.

## Aceitação e avaliação

Os testes exercitam os 14 critérios do apêndice B: geração/upstream, assertions fundamentadas, evidência resolúvel, identidade entre ciclos, conflitos/histórico, rebuild de índice, mudança de perfil, falha de publicação, outputs inválidos, prompt injection inerte, explicação e métricas separadas. Testes anteriores continuam cobrindo ingestão e formatos estruturais.

`mimir eval` usa uma fixture com fatos anotados, consultas factuais, multi-hop, tempo, atualização explícita, contradição e abstention. Reporta extração P/R/F1 por igualdade de sujeito/predicado/objeto, Recall@8, MRR, nDCG@8, categorias, conflitos, atualização, abstention, evidence resolution, rebuild, latência e tokens. É uma regressão pequena e controlada, sem representar qualidade geral ou escala de produção.

Datasets externos entram via `eval --dataset arquivo.json`. Formato Mimir: `episodes`, `queries`, `updates` por quotes únicos e `expected_assertions`. Cada query aceita `question`, `expected` (trechos esperados), `category`, `at`, `as_of`, `history`, `conflict`, `abstain`, `excluded`. Adapters aceitam LongMemEval (`haystack_sessions`, `question`, `answer`) e LoCoMo (`conversation`, `qa`). Medem cobertura de resposta nas evidências; não implementam os avaliadores oficiais de respostas finais. Modelos reais precisam de avaliações próprias por corpus.
