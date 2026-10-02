# Changelog

## v0.2.0 — 2026-10-02

O pipeline avança do Hot Hub de bytes até uma entrega estrutural verificada ao BN1_2, seguindo as etapas Frankenstein e Curadoria/G_P do mapa arquitetural.

### Adicionado

- Route Hub com roteamento por extensão após a ingestão verificável.
- Protocolos para texto UTF-8, JSON, CSV/TSV, DOCX, XLSX, PDF, imagens, WAV PCM e áudio/vídeo via PyAV.
- Frankenstein: representação uniforme de objetos, com identidade, posição, pai e propriedades observáveis.
- Curadoria e G_P com relações `derived_from`, `contains` e `precedes` e validação de origem, sequência, localização e pertencimento.
- BN1_2 com gerações completas, hashes de registros, vínculo ao snapshot Hot Hub e publicação atômica.
- Comandos `transform`, `verify` e `inspect`; modo estrito, reconstrução forçada e limites configuráveis.
- Reutilização idempotente por perfil, reparo de saídas corrompidas e preservação da última geração em falhas anteriores à publicação.
- Demonstração completa com PDF, WAV, MKV e XLSX; documentação de contratos e referência arquitetural fornecida.
- Testes de protocolos, recuperação, integridade, concorrência, CLI e barreira semântica.

### Compatibilidade

- Preservados o layout 3, `mimir.byte-chunks.v1`, nomes, marcadores e comandos anteriores.
- `close` continua em `HUB_READY`; a transformação possui estado separado.
- Um ciclo existente do layout 3 pode avançar sem reenvio dos originais.
- A ingestão e os protocolos stdlib não exigem dependências novas. Leitores opcionais estão em `requirements-structural.txt`.

### Limites

- A entrega é estrutural: Semantic Core/G_S, OCR, transcrição, embeddings e busca semântica permanecem futuros.
- DOCX cobre o corpo principal e inventário de mídia; PDF cobre páginas e imagens XObject diretas. A reconstrução completa do layout visual está fora desta versão.
- O processamento ainda reconstrói um arquivo inteiro em memória e conserva originais e gerações antigas.
- Extensões não suportadas, falhas de parser e limites de extração produzem documentos opacos por padrão; `--strict` recusa o lote. Tamanho acima do limite interrompe a transformação.

Esta é a primeira release versionada publicada no repositório. A base anterior correspondia ao commit `fffbd13`, de 29/09/2026.
