# Jurisprudência das Juntas — acervo público de decisões

> **English summary.** Data pipeline that collects ~12,000 administrative decisions
> (1st and 2nd instance) of Belo Horizonte's tax/inspection appeal boards from Google
> Drive, extracts text from PDF/DOCX/Google Docs (with OCR fallback), enriches each
> decision with structured data scraped from the city's inspection system (SIF:
> offender, infraction, legal provision, location), and publishes a full-text search
> site on GitHub Pages. Three concurrent stages linked by queues, SQLite FTS5 index,
> incremental sync by Drive `modifiedTime`, and a versioned resume marker.
> The corpus also powers [JurisAI](https://github.com/sauliiin/jurisai), a RAG +
> agent platform for analysing administrative proceedings.

Pipeline único para baixar votos dos relatores do Google Drive, ler decisões em
PDF/Word/Google Docs, consultar os autos no SIF e organizar os arquivos por
`LEI / ATO OU FATO CONSTITUTIVO`.

Hoje o fluxo principal está concentrado em um único arquivo:

```text
baixar_e_organizar_por_ato.py
```

Os scripts antigos foram removidos porque a lógica deles foi incorporada ou
substituída por esse pipeline.

## O Que O Código Faz

O script trabalha em 3 estágios paralelos, ligados por filas:

```text
indexação do Drive -> download dos arquivos -> organização por lei/ato
```

1. A indexação procura, no Google Drive, as pastas de interesse:
   - 1ª instância: pastas `SESSÃO NNN` (inclusive as sessões novas após a 449).
   - 2ª instância: pastas com variações de `Votos dos Relatores`.

2. O download baixa os arquivos encontrados:
   - PDF é baixado como `.pdf`.
   - Google Docs é exportado como `.txt`.
   - Word é mantido como `.docx` ou `.doc`.
   - Arquivos temporários usam `.part` até o download terminar.

3. A organização lê o texto do voto e decide o que fazer:
   - Se o texto não tiver `DISPOSITIVO DA DECISÃO`, o arquivo é tratado como
     não-decisão e não consulta o SIF.
   - Se for ATA, controle de votação ou documento genérico de sessão, também é
     descartado antes do SIF, mesmo que contenha vários `Dispositivo da decisão`
     no corpo.
   - Se for decisão, extrai protocolo, assunto e autos encontrados no voto.
   - Para cada auto, consulta o SIF, baixa/lê o PDF do auto e extrai:
     - lei;
     - ato ou fato constitutivo da infração.
   - Copia o voto para a saída final:

```text
pdfs_por_lei_e_ato/<instancia>/<LEI>/<ATO>/arquivo
```

Depois que um arquivo bruto é processado, ele é apagado da pasta temporária de
download. Ou seja, `votos_relatores_pdfs/` funciona como área de trabalho do
pipeline, não como acervo final permanente, exceto quando se usa `--so-download`.

## Fluxo Completo

```text
Google Drive
  |
  | indexa pastas e arquivos permitidos
  v
pipeline_cache.db
  |
  | baixa PDF, Google Docs e Word
  v
votos_relatores_pdfs/
  |
  | extrai texto, filtra não-decisões, acha autos
  v
SIF
  |
  | extrai lei e ato do PDF do auto
  v
pdfs_por_lei_e_ato/
  |
  | registra protocolo, assunto e atos
  v
assuntos.csv
```

## Preparação

Instale as dependências Python:

```bash
python3 -m pip install -r requirements.txt
```

Para processar arquivos `.doc`, também precisa haver LibreOffice disponível no
sistema. O script tenta encontrar `soffice`, `libreoffice` ou a instalação via
Flatpak.

Coloque o OAuth do Google Cloud em:

```text
credentials.json
```

Na primeira execução o navegador abre para autorizar acesso somente leitura ao
Google Drive. O token local fica salvo em:

```text
token.json
```

As credenciais do SIF ficam no ambiente, nunca no código. Crie um `.env` na raiz
(ignorado pelo Git):

```bash
SIF_USER=seu_usuario
SIF_PASS=sua_senha
```

O `atualizar_tudo.sh` carrega esse arquivo automaticamente. Para rodar os scripts
Python diretamente, exporte as variáveis antes (`set -a; . ./.env; set +a`).

## Como Rodar

Execução padrão:

```bash
python3 -u baixar_e_organizar_por_ato.py
```

Execução longa com log e PID:

```bash
nohup python3 -u baixar_e_organizar_por_ato.py \
  --index-workers 60 \
  --download-workers 30 \
  --organize-workers 24 \
  --sif-rate 10.0 \
  > pipeline_ato.log 2>&1 &
echo $! > pipeline_ato.pid
```

Acompanhar:

```bash
tail -f pipeline_ato.log
```

Retomar uma execução aproveitando cache e pulando o que já foi organizado:

```bash
python3 -u baixar_e_organizar_por_ato.py --continuar
```

Refazer o índice do Drive:

```bash
python3 -u baixar_e_organizar_por_ato.py --refresh-index
```

Reconsultar o SIF para todos os votos:

```bash
python3 -u baixar_e_organizar_por_ato.py --refresh-sif
```

## Site Público (GitHub Pages)

O site estático fica em:

```text
index.html
.nojekyll
site_publico/
site_data/votos.jsonl
```

Ele foi feito para funcionar no GitHub Pages sem backend: a busca roda no
navegador carregando `site_data/votos.jsonl`, e a prévia/abertura dos arquivos
usa o preview público do Google Drive.

O site permite:

- busca por significado (IA), que é o padrão nas Decisões e na Legislação; consulta
  com número (auto, protocolo, artigo) ou sem termo conhecido pela IA cai
  automaticamente na busca por expressão;
- busca por expressão, padrão nos Pareceres;
- busca por palavras, exigindo que todas estejam no documento;
- destaque amarelo dos termos encontrados na prévia textual;
- filtro por instância;
- filtro por mês/ano sem reindexar, usando busca oculta por expressões como
  `maio de 2025`;
- filtro só por ano, usando busca oculta por expressões como `de 2025`;
- prévia de PDF pelo Drive;
- prévia textual para `.doc`, `.docx`, `.txt` e `.rtf`;
- botão `Abrir inteiro` apontando para o arquivo público no Drive.

O botão de modo no topo alterna entre três buscas: Decisões (`site_publico/`) →
Pareceres (`site_publico/pareceres/`) → Legislação (`site_publico/legislacao/`) →
Decisões. Pareceres e Legislação usam o mesmo código (`site_publico/pareceres/app.js`)
e os mesmos filtros: categoria e Expressão/Palavras, sem instância, mês e ano. A
página da Legislação troca o acervo e os rótulos pelos atributos `data-*` do
`<body>` e liga a IA da legislação (`data-ia`).

## Legislação

As leis, os decretos e as tabelas de penalidades ficam na pasta pública do Drive
`1WUOuLv7-ddLERjUpNc7klpH9KjxNnzlm` e são indexados pelo mesmo
`preparar_pareceres.py`, no modo `--legislacao`:

```bash
python3 -u preparar_pareceres.py --legislacao
```

Esse modo grava `site_data/legislacao.jsonl` com o texto inteiro de cada arquivo e:

- **categoria = norma**, tirada do nome do arquivo (`lei10534_Anexo II...xls` entra
  em `Lei 10.534`);
- **redação revogada**: os textos "-atual" trazem a redação antiga riscada, seguida
  da vigente. A extração passa pelo HTML do LibreOffice, que preserva o risco, e os
  trechos riscados ficam em `riscado` (posições no texto). O site os mostra riscados;
- **dispositivos**: o texto dividido em artigos (ou em itens, nas tabelas de
  penalidades), com capítulo/seção e a marca de revogado. No site, cada dispositivo é
  um resultado, e a prévia mostra o artigo destacado dentro da norma inteira.

A leitura do HTML, a divisão em artigos e a leitura das citações dos autos ficam em
`legislacao.py`. Arquivos editados no Drive são reindexados pela data de
modificação, e os apagados saem do índice, nos pareceres e na legislação.

Para testar localmente no mesmo modo estático do GitHub Pages:

```bash
python3 -m http.server 8010 --bind 127.0.0.1
```

Abra:

```text
http://127.0.0.1:8010/
```

Para publicar no GitHub Pages, use a raiz do repositório como origem do Pages.
O arquivo `.nojekyll` já está na raiz para servir os arquivos estáticos
literalmente.

`site_data/votos.jsonl` deve ser versionado no Git: ele é o índice estático do
site. Já `votos_brutos/` e `indice_busca.db` ficam fora do Git.

Há também um servidor opcional em `site_server.py`, que usa `indice_busca.db`
para busca via SQLite FTS. Ele é útil para desenvolvimento local, mas o site do
GitHub Pages não depende dele.

## IA Do Site (`treinar_ia.py`)

O site tem funções de IA que rodam inteiramente no navegador, sem servidor e sem
API externa:

- **Casos semelhantes**: ao abrir uma decisão, mostra as cinco mais parecidas e o
  resultado de cada uma.
- **Busca por significado**: modo `Significado (IA)` do seletor de busca. A consulta
  é descrita em linguagem comum e o site indica também a infração mais provável.
- **Legislação**: `Significado (IA)` no modo Legislação acha os artigos vigentes
  pelo fato descrito, e cada artigo aberto mostra os **artigos semelhantes** e em
  quantas decisões do acervo ele é citado.

O modelo é uma rede neural pequena, treinada do zero só com o acervo:

```text
texto -> TF-IDF -> camada de 128 dimensões -> infração do auto (gabarito do SIF)
```

A camada do meio é um espaço em que decisões sobre a mesma infração ficam
próximas; é ela que alimenta as duas funções. O treino é feito em NumPy puro
(entropia cruzada + Adam), leva cerca de um minuto e é determinístico.

```bash
python3 -u treinar_ia.py                  # avalia e gera site_data/ia/
python3 -u treinar_ia.py --sem-avaliacao  # só regenera o modelo
```

Antes de gerar o modelo final, o script treina em 80% das decisões rotuladas e
mede nas 20% restantes. Na última execução (1.802 decisões de teste, 204 infrações):

| Métrica | Resultado |
|---|---|
| Casos semelhantes com a mesma infração (precisão@5) | 81% (ao acaso: 4%) |
| Acerto da infração a partir do texto da decisão | 86% |
| Busca a partir do relato do fiscal (MRR@10) | 0,60 |

Os números ficam em `site_data/ia/modelo.json` e aparecem em
`site_publico/ia.html`, a página que explica a função para quem usa o site.

Na legislação, cada artigo vigente entra no mesmo espaço de 128 dimensões pelo
próprio texto, somado à média das decisões cujos autos o citam no "dispositivo legal
transgredido" (`LEI 9725/09 - ART. 31, DECRETO 13842/10, ARTS. 84...`, lido por
`legislacao.py`). Assim o artigo fica perto das infrações em que é aplicado. O teste
usa as mesmas decisões fora do treino: a partir do texto de cada uma, o artigo citado
no auto aparece entre os cinco primeiros?

| Consulta (decisões fora do treino) | Só palavras | Modelo, texto do artigo | Modelo + citações |
|---|---|---|---|
| Relato do fiscal (1.185 consultas) | 17% | 46% | 65% (MRR@10 0,51) |
| Descrição da infração no auto (1.795) | 89% | 90% | 98% (MRR@10 0,87) |

A coluna do meio foi medida durante o desenvolvimento, com a redação revogada ainda junto.
Misturar a busca por palavras à do modelo não melhorou o resultado. A redação
revogada fica fora da IA. Os arquivos são `legislacao.json` (ids e citações),
`legislacao_docs.bin` e `legislacao_vizinhos.bin` (510 KB no total).

Arquivos gerados em `site_data/ia/` (6 MB, pesos compactados em 8 bits):
`modelo.json`, `termos.bin`, `docs.bin`, `vizinhos.bin`, `resultados.bin` e
`classificador.bin`. O navegador só baixa os maiores quando alguém usa a busca
por significado. O código do navegador está em `site_publico/ia.js`; o
tokenizador de lá precisa continuar idêntico ao de `treinar_ia.py`.

O `atualizar_tudo.sh` retreina o modelo a cada atualização: é assim que ele passa
a conhecer as decisões novas. Ele não aprende com as buscas dos visitantes.

## Acervo Público

Pasta canônica para o site de busca. Diferente de `pdfs_por_lei_e_ato/`, ela
guarda uma única cópia de cada voto, então um voto com vários autos/atos não é
duplicado.

### Atualização incremental (recomendada)

Para descobrir e processar somente arquivos novos ou alterados desde a última
atualização:

```bash
python3 -u preparar_acervo_publico.py --buscar-novos
```

Esse modo consulta no Drive apenas os metadados com `modifiedTime` posterior ao
último scan, mantém o cache existente e cria uma fila persistente de pendências.
Só essa fila é baixada; os demais arquivos não são baixados nem reprocessados.
Uma sobreposição de cinco minutos na consulta, combinada com deduplicação por
`file_id` e `modifiedTime`, evita perder arquivos na virada entre duas
execuções sem baixar novamente uma revisão já concluída.

Se o SQLite local não existir ou estiver incompleto, o script restaura nele os
registros do `site_data/votos.jsonl` antes de exportar. Assim, uma atualização
pequena é mesclada ao índice existente em vez de substituí-lo apenas pelos
arquivos novos.

Para definir manualmente o início da primeira consulta incremental:

```bash
python3 -u preparar_acervo_publico.py \
  --buscar-novos \
  --desde 2026-06-01T00:00:00-03:00
```

Depois de sincronizar os arquivos novos de `votos_brutos/` com a pasta pública,
reaplique os links no índice:

```bash
python3 -u atualizar_links_drive_publico.py ID_DA_PASTA_PUBLICA
```

O token atual do pipeline usa acesso somente leitura ao Drive: a sincronização
com a pasta pública continua sendo uma etapa separada. Os registros que ainda
não foram enviados permanecem pesquisáveis e exibem a prévia textual no site.

Crie/atualize a pasta bruta e o índice enriquecido com:

```bash
python3 -u preparar_acervo_publico.py
```

Para testar em poucos arquivos antes da execução completa:

```bash
python3 -u preparar_acervo_publico.py --limit 10
```

Para recriar o índice do zero, sem manter registros de testes anteriores:

```bash
python3 -u preparar_acervo_publico.py --rebuild
```

Ou para processar um arquivo específico do cache do Drive:

```bash
python3 -u preparar_acervo_publico.py --file-id ID_DO_ARQUIVO
```

O script gera:

```text
votos_brutos/<instancia>/voto-unico
indice_busca.db
site_data/votos.jsonl
marco_atualizacao.json
```

O índice salva o voto uma vez e relaciona apenas os autos encontrados no campo
`Assunto` da decisão, por exemplo `cancelamento ou prazo para cumprimento do(s)
auto(s) n° 20230011362AN, 20230011294AI`. Para cada auto, consulta o SIF e
extrai os campos que o site deve exibir:

- autuado: `NOME (RAZÃO SOCIAL OU PESSOA FÍSICA)`;
- infração: `ATO OU FATO CONSTITUTIVO DA INFRAÇÃO`;
- dispositivo legal transgredido: `DISPOSITIVO LEGAL TRANSGREDIDO`;
- local da constatação: `LOCAL DA CONSTATAÇÃO DA INFRAÇÃO`;
- tipo de decisão: `Decisão de 1ª instância` ou `Decisão de 2ª instância`.

O autuado e o caminho local do arquivo (`caminho_bruto`) ficam só no
`indice_busca.db`. No `site_data/votos.jsonl` publicado, o campo `autuado` sai
dos autos, o nome é trocado por `[AUTUADO]` no texto, no assunto e no nome do
arquivo, e `caminho_bruto` não é gravado.

Por padrão, ATAs, pautas e documentos sem `DISPOSITIVO DA DECISÃO` são removidos
de `votos_brutos/` e registrados como pulados no índice. Para mantê-los na pasta
bruta mesmo assim:

```bash
python3 -u preparar_acervo_publico.py --manter-nao-decisoes
```

### Marco De Retomada

`marco_atualizacao.json` é versionado no Git e funciona como um ponto de
retomada leve do acervo público. A cada execução de
`preparar_acervo_publico.py`, o script lê esse arquivo no início e pula os
`file_id` já anotados como votos indexados ou documentos pulados. No fim da
execução, o marco é regravado com contagens atuais do banco, JSONL, pasta bruta
e IDs processados.

Isso evita baixar e varrer tudo de novo em execuções normais, mesmo que
`indice_busca.db` e `votos_brutos/` continuem fora do Git. Para forçar uma
varredura sem usar o marco:

```bash
python3 -u preparar_acervo_publico.py --ignorar-marco
```

Para recriar tudo do zero, inclusive o SQLite local:

```bash
python3 -u preparar_acervo_publico.py --rebuild
```

O script `atualizar_links_drive_publico.py` também atualiza o mesmo marco com a
pasta pública do Drive e o total de registros que receberam link público.

Depois de enviar/sincronizar `votos_brutos/` para a pasta pública do Google
Drive, atualize os links públicos dentro do JSONL:

```bash
python3 -u atualizar_links_drive_publico.py 1LW-tLhZfsc8l1-vBFqrFK86rtuUPIDer
```

Esse passo casa os arquivos pelo nome gerado em `votos_brutos/` e grava no
JSONL:

- `drive_file_id_publico`;
- `drive_view_url`;
- `drive_preview_url`.

Se algum arquivo não for encontrado na pasta pública, o script gera
`site_data/arquivos_sem_link_publico.txt`. Esse arquivo é diagnóstico local e
fica ignorado pelo Git.

## Atualização Completa (`atualizar_tudo.sh`)

Para não rodar cada script manualmente, `atualizar_tudo.sh` executa em sequência
a atualização incremental de votos, links públicos, pareceres e legislação, retreina a IA do
site e no final já publica (`git add` + `commit` + `push`) as mudanças em `site_data/*.jsonl`,
`site_data/ia/` e `marco_atualizacao.json`:

```bash
./atualizar_tudo.sh
```

Ele exige `credentials.json`, `token.json` e o `.env` com `SIF_USER`/`SIF_PASS`
na raiz do projeto (veja [Preparação](#preparação)). Se não houver nada novo desde a última execução, o
script não cria commit.

Para acompanhar o progresso localmente enquanto ele roda em segundo plano:

```bash
tail -f atualizar_tudo.log
```

## Saídas

### `pdfs_por_lei_e_ato/`

Saída final dos votos organizados:

```text
pdfs_por_lei_e_ato/
  1a_instancia/
    LEI 9725_09/
      OCUPAR, HABITAR OU UTILIZAR EDIFICAÇÃO/
        voto.pdf
  2a_instancia/
    LEI 11181_19/
      INSTALAR TOLDO SEM LICENÇA PRÉVIA/
        voto.docx
```

O nome da pasta do ato é normalizado pelo código:

- usa caixa alta;
- remove números soltos e `AFERIDA`, que aparecem como lixo do SIF;
- usa no máximo 7 primeiras palavras;
- remove conectivos soltos no fim, como `DA`, `DE`, `DO`, `E`, `OU`, `COM`,
  `SEM`, `POR`;
- limita o nome a 120 caracteres.

### `assuntos.csv`

CSV gerado durante a organização:

```csv
protocolo,assunto,ato_constitutivo
```

Por padrão ele é retomado em modo append e evita linhas duplicadas. Para recriar
do zero:

```bash
python3 -u baixar_e_organizar_por_ato.py --fresh-csv
```

Para não gerar CSV:

```bash
python3 -u baixar_e_organizar_por_ato.py --no-csv
```

### `pipeline_cache.db`

Cache SQLite usado para acelerar retomadas:

- `arquivos`: arquivos descobertos no Drive;
- `index_folders`: pastas-raiz já indexadas;
- `meta`: marcadores globais, como índice completo;
- `sif_pares`: pares `(lei, ato)` por voto.

Um voto salvo no cache com lista vazia significa: já foi processado e não teve
par lei/ato útil.

## Filtro De Documentos De Sessão

ATAs, controles de votação e documentos genéricos de sessão são descartados
dentro do fluxo principal, logo depois da extração de texto e antes de qualquer
consulta ao SIF. Isso evita que documentos com vários trechos `Dispositivo da
decisão` sejam tratados como votos individuais.

O filtro também normaliza nomes com prefixos soltos, como `_ATA...`, `- ATA...`
ou `Cópia de ATA...`, e só descarta quando o conteúdo confirma que é documento
de sessão.

## Principais Opções

| Opção | Padrão | Descrição |
| --- | --- | --- |
| `--tipos` | `pdf,gdoc,docx,doc` | Formatos aceitos na indexação. |
| `--instancia` | `ambas` | Processa `1`, `2` ou `ambas`. |
| `--output` | `votos_relatores_pdfs` | Área de download bruto/temporário. |
| `--ato-output` | `pdfs_por_lei_e_ato` | Saída final por lei e ato. |
| `--credentials` | `credentials.json` | OAuth do Google Cloud. |
| `--token` | `token.json` | Token OAuth local. |
| `--cache` | `pipeline_cache.db` | Banco SQLite de cache. |
| `--index-workers` | `60` | Paralelismo da indexação. |
| `--download-workers` | `30` | Paralelismo dos downloads. |
| `--organize-workers` | `24` | Paralelismo da organização. |
| `--doc-concurrency` | `3` | Conversões `.doc` simultâneas via LibreOffice. |
| `--sif-rate` | `10.0` | Limite global de consultas SIF por segundo. |
| `--csv-output` | `assuntos.csv` | Caminho do CSV. |
| `--fresh-csv` | desligado | Recria o CSV do zero. |
| `--no-csv` | desligado | Não escreve CSV. |
| `--so-download` | desligado | Baixa arquivos sem organizar nem consultar SIF. |
| `--refresh-index` | desligado | Ignora o índice salvo e reindexa o Drive. |
| `--refresh-sif` | desligado | Limpa pares do SIF e consulta tudo de novo. |
| `--no-cache` | desligado | Desativa o cache SQLite. |
| `--continuar` | desligado | Pula votos já organizados ou já marcados sem par. |
| `--max-retries` | `20` | Tentativas por falha de rede. `0` significa infinito. |

## Leitura Por Formato

| Formato | Como é lido |
| --- | --- |
| `.pdf` | `pdfplumber`, usando as primeiras páginas do voto. |
| Google Docs | Exportado pela API do Drive como texto `.txt`. |
| `.docx` | `docx2txt`. |
| `.doc` | Conversão para texto via LibreOffice. |

Se uma dependência opcional estiver ausente, o script avisa no início. Arquivos
que não puderem ser lidos entram na contagem `Sem ato`.

## Progresso No Log

O log mostra um painel a cada 3 segundos com:

- início, tempo transcorrido e previsão de fim;
- pastas indexadas;
- arquivos descobertos, baixados, já existentes e pulados;
- progresso geral;
- autos verificados no SIF;
- tamanho das filas;
- organizados, sem ato e erros;
- atos mais frequentes até o momento.

Exemplo:

```text
---------- [10:45:38] ----------
Pastas indexadas :  638 / 638
Arquivos descob. : 16392
Baixados         :  16392  (36 novos + 16356 já tinham)

Progresso geral  :   7376 / 16392  (45.0%)
Autos verificados:  29451 / 29519  (99.8%)

Fila download    :    0  |  Fila organizar: 9016
Organizados      :   6744  |  Sem ato: 632  |  Erros: 0
```

## Arquivos Ignorados Pelo Git

Os artefatos locais ou sensíveis ficam fora do Git:

- `credentials.json`
- `token.json`
- `.env` (credenciais do SIF)
- `votos_relatores_pdfs/`
- `pdfs_por_lei_e_ato/`
- `votos_brutos/`
- `pareceres_brutos/` e `legislacao_brutos/`
- `assuntos.csv`
- `indice_busca.db`
- `pipeline*_cache.db`
- `site_data/arquivos_sem_link_publico.txt`
- `*.log`
- `*.pid`
- `*.part`
- `__pycache__/`

O arquivo `site_data/votos.jsonl` não é ignorado: ele é necessário para o site
estático no GitHub Pages. O arquivo `marco_atualizacao.json` também não é
ignorado: ele é o ponto de retomada versionado do acervo público.

## PDFs Dos Autos (`scripts/autos/`)

Scripts sob demanda, fora do `atualizar_tudo.sh`, que mantêm os PDFs dos autos de
infração acessíveis pelo site:

1. `scripts/autos/baixar_autos_sif.py` baixa do SIF os PDFs dos autos citados em
   `site_data/votos.jsonl` para `site_publico/Autos/` (retomável).
2. `scripts/autos/upload_autos_drive.py` envia essa pasta para o Google Drive
   (escopo de escrita, token separado em `token_upload.json`).
3. `scripts/autos/mapear_autos_drive.py` gera `site_publico/Autos/manifest.json`,
   que o site usa para apontar cada auto para o arquivo no Drive.

## Observações

- A pasta final organizada por lei/ato é `pdfs_por_lei_e_ato/`.
- A fonte canônica do site público é `votos_brutos/` + `site_data/votos.jsonl`.
- `votos_relatores_pdfs/` é área intermediária e pode ficar vazia ao fim.
- O site do GitHub Pages não executa Python; qualquer busca publicada precisa
  funcionar em HTML/CSS/JS estático.
- O SIF tem rate-limit global. Aumentar `--organize-workers` não passa do limite
  definido em `--sif-rate`.
- Falhas transitórias de rede usam retry com backoff. Se um item estourar o
  limite de tentativas, o erro é registrado e o pipeline segue.
- Para incluir novos formatos em um índice já salvo, use `--refresh-index`.
