# Auditoria de execução — v0.4.1

A auditoria cobre a entrada/Pacote/BN1_1, Hot Hub, protocolos estruturais e BN1_2, Semantic Core e persistência, canais de I/O, adaptador Drive, CLI, MCP e empacotamento do plugin. A suíte anterior de 114 testes passava; as novas regressões reproduzem falhas em casos válidos e durante interrupções operacionais.

## Recuperação dos canais de I/O

| Reprodução | Falha anterior | Correção |
| --- | --- | --- |
| Ingestão publica uma geração, depois a outbox falha | Recibo de entrada muda de COMPLETE para FAILED apesar da memória já confirmada | Indexação/enfileiramento ocorrem após o checkpoint completo; retry publica o recibo sem outra inferência |
| `--reprocess` falha depois de uma ingestão concluída | A última ingestão completa é sobrescrita por PROCESSING/FAILED | A nova tentativa usa `attempt.json`; o recibo confirmado permanece disponível |
| Índice de revisão é perdido antes de retry | O recibo é reutilizado, mas o índice não é reparado; ciclos seguintes repetem downloads | A reutilização do recibo também reconstrói a entrada em `IO/seen` |
| Ponte MCP recebe metadados de pasta incompletos | KeyError/TypeError e traceback | Metadados e parents são validados e retornam erro de domínio |

As regressões estão em `tests/test_io_runtime_regressions.py`. Preservar COMPLETE refere-se ao checkpoint semântico; uma saída continua PENDING até confirmação real do Drive.

## Pipeline estrutural

Campos CSV/TSV válidos que excedem o limite padrão de 128 KiB agora são aceitos até `max_text_chars`, com restauração do limite global mesmo quando o parser falha e serialização segura para chamadas concorrentes. O leitor DOCX preserva tabulações, quebras, hífens e limites entre parágrafos de células. A versão do protocolo estrutural passou para `0.2.1`, fazendo ciclos existentes reprocessarem derivados gerados pela versão anterior.

## Semantic Core

Uma consulta salva exige permissão de escrita, enquanto a consulta somente leitura continua disponível para leitores. O filtro temporal é aplicado antes do limite lexical BM25; manifestos de índice que são JSON válido mas não são objetos fazem o índice ser reconstruído. Datas civis inválidas em uma frase isolada não descartam as demais afirmações válidas do lote, e reflexões só entram no contexto quando todas as dependências permanecem disponíveis no instante consultado.

## Drive, CLI e MCP

Paginação vazia/parcial e falhas transitórias de rede do Drive são tratadas sem abandonar a entrega da outbox. Falhas de refresh OAuth produzem erros acionáveis sem expor detalhes privados. Configurações malformadas, tokens HTTP acima do limite, portas inválidas e caminhos Unicode no TOML retornam erros controlados. O empacotador valida o subprocesso e impede arquivar o próprio ZIP.

Esses casos somam 30 novas regressões, executadas junto dos testes existentes. A auditoria não converte erros de provedor em sucesso local: status OAuth, pasta remota e entrega Drive continuam sendo confirmados apenas por metadados observados.

## Limites da verificação

Os testes de Drive reproduzem o contrato da API com fixtures; não provam que a conta Google da sessão está sincronizada. A conexão desta sessão recusou criação de pasta com `ACCESS_TOKEN_SCOPE_INSUFFICIENT` na v0.4.0. Corrigir o código não concede esse escopo OAuth, e a auditoria não cria IDs de pasta ou recibos remotos fictícios.

OCR, ASR e modelos locais continuam opcionais e dependem dos binários/pesos configurados. Nenhuma correção é uma garantia de ausência de erros em todos os formatos e ambientes. Os casos cobertos e os comandos de validação devem ser usados para verificar a implantação escolhida.
