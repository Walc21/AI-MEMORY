# Entrada, saída, Drive e MCP — v0.4.0

Os canais se apoiam no pipeline existente: Pacote → BN1_1 → Hot Hub → protocolos/Curadoria/G_P → BN1_2 → Semantic Core. A integração transporta dados e acrescenta proveniência externa às inferências; não atribui significado durante o transporte. O MCP de memória consulta o mesmo ledger verificado que a CLI e a API `Memory`.

## Dois servidores e duas formas de acessar Drive

| Componente | Função | Autenticação |
| --- | --- | --- |
| MCP `io` | Ingestão em staging, recebimento de saídas, outbox e confirmação | Acesso POSIX no stdio; bearer privado no HTTP |
| MCP `memory` | Contexto limitado, busca temporal, explicação, memória de trabalho e episódios opcionais | Acesso POSIX no stdio; bearer privado no HTTP |
| Ponte com o plugin Drive | Host combina os MCPs Mimir com as ferramentas Google já autenticadas | OAuth protegido do plugin; não é exportado |
| API Drive v3 | Processo autônomo com polling, download, exportação e upload | OAuth próprio de cliente Desktop, na mesma conta |

O MCP `io` não chama ferramentas internas de ChatGPT por nome nem herda automaticamente a conexão Google de uma sessão. A ponte é uma skill do host com suas ferramentas disponíveis. Para operação sem supervisão, use a API oficial com credenciais concedidas pelo Google. O servidor de memória funciona sem Drive quando a memória local já contém dados.

## Instalação e diretório persistente

Requer Python 3.10+, Linux/POSIX e SQLite FTS5, como a v0.3.0:

```bash
python -m pip install '.[mcp,drive,structural]'
export MIMIR_MEMORY_DIR=/caminho/absoluto/memoria
export MIMIR_NAMESPACE=projeto
mimir io status
```

Defina essas variáveis no processo que inicia os MCPs e o sincronizador, ou use `--memory-dir` e `--namespace` antes do comando. A conta e as pastas ficam vinculadas ao namespace, não ao repositório. Trocar de conta/pastas requer outro namespace. Se já existir uma memória v0.3.0, use seu mesmo caminho; não há migração destrutiva.

## Usar a conta do plugin

O [plugin local](../plugins/mimir-memory) fornece as configurações dos servidores e a [skill drive-memory](../plugins/mimir-memory/skills/drive-memory/SKILL.md). Gere um ZIP com o interpretador instalado e os caminhos absolutos reais:

```bash
python tools/package_mimir_plugin.py \
  --memory-dir /caminho/absoluto/memoria --namespace projeto \
  --output /caminho/fora/do/repositorio/mimir-memory-local.zip
```

Instale pelo fluxo de plugins locais suportado pelo seu host. O pacote exige o plugin Google Drive disponível no mesmo host; não contém conexão Google fictícia, tokens ou um endereço MCP remoto. A distribuição local serve para clientes que executam processos stdio. A presença de um ZIP não instala o plugin nem mantém este ambiente remoto ativo após uma sessão.

A skill lê o perfil da conta, busca/cria a raiz `AI MEMORY - Mimir`, depois `Entrada` e `Saída`. Ela relê os metadados de cada pasta, verifica parentes e vincula IDs observados. Se houver homônimas, não escolhe uma arbitrariamente. A vinculação também está disponível em `mimir io configure arquivo.json`, com `root`, `incoming`, `outgoing` (metadados observados do Drive) e `account_email` (perfil confirmado). Guarde esse arquivo fora do repositório.

Para cada entrada, o host lê metadados antes/depois do download, copia o arquivo recebido para `io_status.staging_directory`, chama `io_ingest_file` e remove apenas seu arquivo temporário. Essa ponte requer staging compartilhado entre host e MCP; um MCP remoto sem filesystem compartilhado deve usar a API autônoma. Os downloads do plugin usam retorno de arquivo/stream; o limite de corpo MCP HTTP é 4 MiB e não é um canal para transferir grandes blobs.

Para saídas, o host chama `io_pending`, verifica se um upload anterior já existe pelo nome/size/hash, publica `local_path` usando as ferramentas Drive e relê metadados remotos antes de `io_acknowledge`. Em caso de resultado incerto, a fila permanece pendente para reconciliação, sem afirmar que foi entregue.

O Google precisa conceder os escopos de leitura e escrita. `ACCESS_TOKEN_SCOPE_INSUFFICIENT` é um bloqueio do provedor: reconecte o plugin nas configurações do host. Autorizar uma tarefa na conversa não altera o OAuth concedido pelo Google.

## API oficial para sincronização autônoma

1. Em um projeto Google Cloud, habilite a Drive API e configure a tela de consentimento OAuth. Se o app estiver em teste, inclua a conta Google nos usuários de teste.
2. Crie um cliente OAuth do tipo **Desktop app** e baixe seu JSON para um caminho privado, fora do checkout.
3. Execute o fluxo local e escolha a mesma conta que a ponte vinculou:

```bash
mimir drive auth --client-secrets /caminho/privado/client_secret.json
mimir drive bootstrap
mimir drive sync
mimir drive sync --watch --interval 30
```

O callback OAuth usa `127.0.0.1` e porta livre. `--port` fixa uma porta; `--no-browser` mostra o link do consentimento sem abrir o navegador. Em uma máquina remota, encaminhe essa porta localmente antes de concluir o consentimento. Não copie um código de OAuth de outro fluxo nem extraia o token do plugin.

A credencial é armazenada com modo 0600 em `<memória>/<namespace>/IO/credentials/google-token.json`; `--token-file` ou `MIMIR_GOOGLE_TOKEN_FILE` seleciona uma credencial privada existente. `auth` não sobrescreve arquivos. Tokens temporários são renovados pela biblioteca oficial durante requisições; o refresh token mantém o acesso concedido até expirar/revogação. Apps OAuth externos em modo de teste podem expirar o refresh token em sete dias; o estado pendente é conservado e um novo consentimento pode ser feito para outro arquivo privado.

O escopo é `https://www.googleapis.com/auth/drive`: arquivos que o usuário coloca na Entrada podem ter sido criados por outros aplicativos, portanto `drive.file` sozinho não cobre a função. A aplicação restringe transferências aos IDs configurados, verifica a identidade da conta a cada ciclo e não altera compartilhamento ou apaga arquivos. Não há upload de credenciais para GitHub ou Saída.

O modo `--watch` faz polling, não usa webhooks. Implemente a supervisão no seu ambiente com um serviço persistente (por exemplo, systemd), iniciando o comando com o interpretador instalado, mesmo usuário POSIX, memória persistente e variáveis acima. Encerre com SIGINT/SIGTERM. Um ciclo falho emite JSON `complete:false`; o próximo tenta reconciliar saídas pendentes. Monitore esse campo. Revogar acesso encerra o sucesso das operações, sem apagar o ledger.

`sync` enumera todas as páginas de filhos diretos da Entrada, ignorando pastas. Suporta arquivos binários com tamanho/checksum e exporta Docs→DOCX, Sheets→XLSX e Slides→PDF. Atalhos e outros tipos Google são relatados como não suportados. Exportações nativas da API normalmente têm limite de 10 MB; excedê-lo produz falha explícita, sem aceitar truncamento. Ausência/movimento de arquivo remoto não remove a memória anterior.

```bash
# Opções do pipeline anterior continuam disponíveis:
mimir drive sync --ocr --ocr-language por+eng --strict-multimodal
mimir drive sync --extractor ollama --model modelo-local
# Reprocessa revisões existentes ao mudar o perfil de extração:
mimir drive sync --reprocess --signing-key /caminho/privado/chave.pem
```

Uma revisão completa é reconhecida antes de outro download. Mudanças em conteúdo/revisão geram outro job e preservam história. Renomear/mover pode atualizar a versão do Drive; a revisão inclui ID, version, modifiedTime, MIME, size e checksums. Arquivos em entrada não são tratados como instruções administrativas.

## Harnesses por stdio

```bash
mimir mcp config --client json --output /caminho/privado/mimir-harness.json
mimir mcp config --client codex --output /caminho/privado/mimir-harness.toml
```

Escolha o formato que seu cliente suporta. JSON contém `mcpServers`; TOML usa `[mcp_servers.mimir-io]` e `[mcp_servers.mimir-memory]`. O comando imprime a configuração se não houver `--output`; nunca sobrescreve a configuração existente. Cole/mescle a seção no cliente, preservando seus outros servidores, e reinicie-o. Os comandos gerados utilizam `sys.executable`, o caminho absoluto da memória e o namespace.

Para permitir episódios, gere com `--allow-write`, ou adicione essa opção apenas ao processo `memory`. O processo `io` contém ferramentas de ingestão e publicação; dê esse perfil ao integrador. Para um harness de consulta, basta o processo `memory`. Ambos podem rodar ao mesmo tempo sobre o mesmo namespace e usam locks para publicação canônica.

| Perfil | Ferramentas |
| --- | --- |
| `memory` | `memory_query`, `memory_explain`, `memory_status`, `memory_working`; recurso `mimir://working` |
| `memory --allow-write` | Acrescenta `memory_remember` e `memory_set_working` |
| `io` | `io_status`, `io_configure`, `io_ingest_file`, `io_receive`, `io_pending`, `io_acknowledge`, `io_export_memory`, `io_publish_query`, `io_sync_drive` |

Consultas MCP limitam `k` a 1–100, a pergunta a 8000 caracteres e `budget_chars` a 256–80000. O orçamento mede o contexto, não o JSON completo com evidências. `memory_explain` retorna a origem e `external_source` quando fornecido pela integração. A identidade da conta é registrada como fingerprint; os IDs, URLs e nomes das fontes são dados privados da memória e podem aparecer nas respostas autorizadas.

## Streamable HTTP

```bash
mkdir -m 700 -p .mimir-keys
mimir mcp token .mimir-keys/memory.token
mimir mcp serve --role memory --transport streamable-http \
  --port 8765 --token-file .mimir-keys/memory.token
# Outro processo, outro perfil/porta/token:
mimir mcp token .mimir-keys/io.token
mimir mcp serve --role io --transport streamable-http \
  --port 8766 --token-file .mimir-keys/io.token
```

O endpoint é `/mcp`, em loopback por padrão; usa MCP padrão via SDK oficial, JSON estruturado e protocolo negociado pelo cliente. Configure no harness `http://127.0.0.1:8765/mcp` e o header `Authorization: Bearer <valor do arquivo privado>`, usando o mecanismo seguro de credenciais do cliente. O CLI não imprime o token. Todas as requisições exigem o bearer; o SDK limita o corpo e verifica Host/Origin contra rebinding. Credenciais com permissões de leitura para grupo/outros são recusadas.

Para outro computador, use túnel SSH ou proxy HTTPS que preserve Host/Origin localmente e forneça autenticação. O processo recusa bind público direto. Essas credenciais MCP são separadas das credenciais Google. Um bearer de I/O autoriza as operações do perfil `io`; não o disponibilize a um cliente que só deve consultar memória.

## Layout, contratos e recuperação

```text
<memória>/<namespace>/
  IO/drive.json                     # conta/pastas verificadas; sem token
  IO/credentials/google-token.json  # opcional, OAuth privado
  IO/staging/                       # downloads materializados pelo host/API
  IO/seen/                          # índice de revisões concluídas
  Input_Storage/jobs/<sha256>/       # fonte, runtime e recibo de ingestão
  Output_Storage/outbox/<sha256>/    # payload.json e receipt.json
  generations/, sources/, indexes/ # memória canônica existente
```

- `mimir.drive-config.v1`: fingerprint da conta, três IDs distintos e backend plugin/API.
- `mimir.input-receipt.v1`: job derivado do ID/revisão/SHA-256/exportação, estado `PROCESSING`, `FAILED` ou `COMPLETE`, metadados remotos e geração/fingerprint semânticos. A proveniência está na inferência canônica e é validada com o upstream, sem alterar `source.id` ou G_P.
- `mimir.exchange.v1`: namespace, tópico, geração opcional e payload JSON. Saídas são de conteúdo imutável, com ID derivado do envelope.
- `mimir.output-receipt.v1`: SHA-256, MD5, tamanho, ID remoto reservado quando houver, e estado `PENDING` ou `DELIVERED`. Confirmação exige o nome esperado, pasta Saída, tamanho e correspondência de todos os checksums presentes no readback.

Publicações locais usam arquivo temporário, fsync e rename; payload publicado antes de uma interrupção pode recuperar seu recibo. Ingestões interrompidas retomam o runtime existente. Um upload API usa ID remoto pré-reservado e `appProperties.mimir_output_id`; após timeout, o próximo ciclo consulta esse ID e valida a entrega antes de repetir criação. A ponte do plugin reconcilia nome/checksum porque não controla IDs pré-reservados.

Limites padrão: entrada 64 MiB, saída 16 MiB e 10000 arquivos por enumeração. `IOLimits` permite ajuste explícito pela API Python. O envelope nunca é truncado. `io export-memory` exporta ledger/manifesto verificados; não é um backup completo e não inclui blobs originais, credenciais, ACL ou Working Memory. Para restauração integral, preserve o diretório privado inteiro.

```bash
mimir io status
mimir io pending
mimir io export-memory
mimir query 'Onde João trabalha?' --publish
# Depois, drive sync ou a ponte entrega a resposta citada na Saída.
# Receber uma saída JSON produzida por outro sistema:
mimir io receive /caminho/resultado.json --topic answer
```

Os testes exercitam download alterado, conta/pastas erradas, hashes, symlinks, deduplicação, recuperação de falhas, paginação da API, exportação nativa e upload após resposta perdida. Os testes MCP iniciam processos reais, negociam sessões, chamam ferramentas, leem recursos e validam acesso HTTP. Os testes automatizados de Drive usam fixtures; uma conexão real ainda depende de um grant Google válido e não pode ser inferida do sucesso desses testes.
