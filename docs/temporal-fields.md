# Contrato de campos amostrados — v1

Decisão de 29/09/2026: remover a organização por extensão do BN1_1 e do Hot Hub e iniciar uma representação comum de valores, eixos e tempo. Esta decisão substitui as partições por IDD/extensão e o Sorter das fontes de 28/09/2026. Preservação de originais, proveniência e integridade continuam obrigatórias.

## Formulação

Um campo tem domínio declarado, um vetor de valores e, quando o conteúdo fornece um relógio, coordenadas temporais:

\[
F:D\times T\longrightarrow\mathbb{R}^{c}.
\]

`D` pode ser a grade de uma imagem, os índices de uma tabela ou outro conjunto de coordenadas. O número de componentes `c` e a dimensão de `D` não são limitados a três. Eixos, unidades, dtype, origem e disposição das amostras fazem parte do contrato. Coordenadas de pixels começam em zero e medem posições na grade de cada plano, não metros no mundo.

Para uma imagem estática, adota-se a extensão constante `F(x,y,t)=I(x,y)`. Nesse modelo, a derivada **temporal** é zero. O gradiente **espacial** pode ser diferente de zero. Isso não afirma que a cena fotografada estava parada antes ou depois da fotografia.

Para amostras em tempos distintos:

\[
\Delta F_k=F_{k+1}-F_k,
\qquad
\overline{\partial_t F}_k=\frac{\Delta F_k}{t_{k+1}-t_k}.
\]

A segunda expressão é a taxa média no intervalo. Não é uma prova da existência de uma derivada contínua nem recupera o que aconteceu entre as amostras. Os arquivos guardam as amostras completas, as diferenças e os intervalos racionais; nenhuma interpolação é presumida.

Uma trajetória identificada `p(t)` permite calcular a velocidade média `Δp/Δt`. Mas a variação de cor em um pixel não identifica, sozinha, o movimento de uma partícula. O módulo `calculus` oferece o cálculo para posições já correspondentes ao mesmo objeto. Rastreamento, fluxo óptico e reconstrução física não estão implementados.

Guardar apenas gradientes destruiria informação: `F` e `F+C` têm os mesmos gradientes para qualquer constante `C`. Por isso valores iniciais e amostras permanecem disponíveis. Nem um gráfico 3D nem uma EDO constituem um decodificador universal de arquivos.

## Universalidade do contêiner e limites da interpretação

Qualquer arquivo finito pode ser representado exatamente por um vetor de bytes:

\[
B=(b_0,\ldots,b_{m-1}),\qquad b_i\in\{0,\ldots,255\}.
\]

Isso permite um contrato de armazenamento comum, mas não revela o significado desses bytes. O eixo `byte_index` não é tempo e diferenças entre bytes de um arquivo comprimido não são velocidades ou gradientes da cena codificada.

Som, imagem e vetor também não são três classes exclusivas: imagens e sons já admitem representações vetoriais, e um arquivo pode conter ambos, além de texto, estruturas, instruções ou conteúdo cifrado. A implementação usa **campos e trilhas**, sem impor essas categorias como pastas.

A representação de todo arquivo inclui um `byte_field`, referenciando os originais sem duplicá-los novamente. Os adaptadores acrescentam campos decodificados somente quando conhecem a codificação. Não há execução de programas, carregamento de objetos pickle, resolução de referências externas de playlists ou simulação arbitrária de arquivos.

## Contrato físico

`fields.json` publica uma geração `mimir.sampled-fields.v1` completa:

- `sources`: nome temporário → SHA-256 do original.
- `records`: nome temporário → caminho do registro dentro da geração.
- `files`: caminho de cada registro/array → SHA-256.
- `summary`: contagens `decoded`, `partial` e `opaque`.

Cada `record.json` contém `source`, `original_retained`, `byte_field`, `status`, identificação do adaptador e a lista de `fields`. Quando opaco, contém também `reason`. Campos decodificados usam o mesmo contêiner estrutural, mesmo quando seus eixos ou dtypes são diferentes.

Cada campo declara `id`, `axes`, `time` e `chunks`. Os campos de mídia também declaram unidades e propriedades nativas. Cada bloco referencia um array `.npy` por `samples.file`, com `dtype` e `shape`. Esse caminho é relativo ao diretório do registro.

### Tempo

| Caso | Contrato |
| --- | --- |
| Imagem estática | `time.kind=static`, variação temporal zero |
| Áudio | `start`, `step=1/sample_rate`, duração e relógio nativo por bloco |
| Vídeo | `start=PTS*time_base`, sem FPS fixo; duração quando fornecida pelo decodificador |
| Tensor genérico e bytes | `time.kind=unmodeled` ou `time=unmodeled` |

Tempos e intervalos são pares `[numerador, denominador]` em segundos. A origem temporal nativa, inclusive negativa, é mantida. A duração desconhecida do último quadro é `null`; não é preenchida com uma duração inventada. Para áudio, cada amostra do bloco ocupa `start + índice * step`.

`temporal.delta` descreve as diferenças entre amostras do mesmo bloco de áudio. `from_previous.delta` descreve a diferença entre o último valor do bloco anterior e o primeiro do atual, ou entre dois quadros na mesma grade. `dt` fornece o intervalo correspondente. Uma lacuna temporal produz uma taxa média sobre essa lacuna, nunca amostras interpoladas. Timestamps ausentes ou não crescentes deixam a mídia opaca. Mudança de resolução, dtype ou configuração de trilha também exige um adaptador futuro de segmentos e, nesta versão, resulta em `opaque`.

### Precisão e amostras

Os arrays de mídia preservam os planos nativos do decodificador, sem redimensionamento, reamostragem de áudio ou redução automática a RGB de 8 bits. A cobertura atual inclui RGB/BGR de 8 bits, RGBA e variantes de 8 bits, RGB/RGBA de 16 bits, cinza de 8/16 bits e planos separados com um componente inteiro de 8/9/10/12/14/16 bits, incluindo layouts YUV compatíveis. Layouts empacotados não reconhecidos, paletas e outros casos ficam opacos. Imagens animadas dependem do contêiner e do layout decodificados; não há promessa de cobertura de todos os formatos.

Padding alocado pelo decodificador é descartado; os valores dos pixels não são alterados. Metadados de cor disponíveis e propriedades de trilha são registrados, mas informações auxiliares, anexos e detalhes não expostos pelo adaptador continuam no original. Converter um codec com perdas em amostras não recupera informação que já havia sido perdida antes da ingestão.

As diferenças de amostras inteiras de até 32 bits usam `int64`, evitando estouro em transições como `-32768 → 32767`. Tensores inteiros de 64 bits são preservados, mas a API de diferenças exatas rejeita esse dtype. Diferenças e taxas de valores flutuantes são cálculos aproximados. Gradientes espaciais são calculados sob demanda, para evitar multiplicar o armazenamento de cada quadro por todos os eixos.

## Leitura e cálculo

Após `close`, o seguinte exemplo abre um registro decodificado e seu primeiro bloco:

```python
import json
from pathlib import Path
import numpy as np
from Transformer_Core.Fields.calculus import spatial_gradient

hub = Path('.mimir-runtime/Transformer_Core/Hot_Hub')
manifest = json.loads((hub / 'fields.json').read_text())
generation = hub / 'representations' / manifest['generation']
for name, relative in manifest['records'].items():
    path = generation / relative
    record = json.loads(path.read_text())
    print(name, record['status'], record.get('reason'))
    for field in record['fields']:
        chunk = field['chunks'][0]
        values = np.load(path.parent / chunk['samples']['file'], allow_pickle=False)
        if field['axes'][:2] == ['y', 'x']:
            gradient = spatial_gradient(values, axes=(0, 1))
            print(field['id'], values.shape, gradient[0].shape)
```

`spatial_gradient` calcula diferenças progressivas nos eixos escolhidos e aceita espaçamentos explícitos. A dimensão diferenciada fica uma amostra menor; um eixo com apenas uma amostra retorna derivada vazia. Em campos com vários componentes, o conjunto das derivadas parciais corresponde a um Jacobiano discreto. O resultado representa a variação dos componentes nativos; não deve ser interpretado automaticamente como intensidade perceptual ou distância física.

Para uma trajetória já conhecida:

```python
import numpy as np
from Transformer_Core.Fields.calculus import trajectory_velocity

positions = np.array([[0., 0., 0.], [2., 4., 0.], [8., 4., 6.]])
velocity = trajectory_velocity(positions, times=[0, 2, 5])
# [[1., 2., 0.], [2., 0., 2.]] — unidades espaciais por unidade de tempo
```

## Estados e recuperação

`decoded` significa que as trilhas elegíveis foram decodificadas nos layouts suportados. `partial` registra trilhas de outro tipo que permaneceram apenas no original. `opaque` indica ausência de adaptador, erro, tempo inválido, layout não suportado ou limite de recursos. Uma falha de escrita em disco é propagada e impede a publicação e a limpeza; não é disfarçada de formato desconhecido.

As gerações são imutáveis após a publicação. `close` reutiliza uma geração íntegra e reconstrói uma geração ausente ou corrompida. Hashes detectam divergência em relação ao manifesto; não substituem autenticação contra alguém capaz de modificar simultaneamente dados e manifestos. O Pacote retém a cópia de recuperação dos originais. Nenhuma política de expiração foi adicionada.

## Referências técnicas

- [PyAV — tempo](https://pyav.org/docs/stable/api/time.html): PTS e bases temporais racionais.
- [PyAV — vídeo](https://pyav.org/docs/stable/api/video.html): planos, componentes, profundidade e stride.
- [PyAV — áudio](https://pyav.org/docs/stable/api/audio.html): amostras, canais e disposição planar/empacotada.
- [W3C — PNG](https://www.w3.org/TR/png-3/): tipos de cor e profundidade das amostras.

As fontes MIMIR de 28/09/2026 permanecem documentos históricos. Este contrato implementa a revisão solicitada em 29/09/2026 e não afirma que uma representação física universal de qualquer conteúdo tenha sido demonstrada.
