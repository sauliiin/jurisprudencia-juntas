# Google Drive Downloader and Organizer

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
   - 1ª instância: pastas `SESSÃO NNN`, de `001` até `449`.
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

- busca por expressão, que é o padrão;
- busca por palavras, exigindo que todas estejam no documento;
- destaque amarelo dos termos encontrados na prévia textual;
- filtro por instância;
- filtro por mês/ano sem reindexar, usando busca oculta por expressões como
  `maio de 2025`;
- filtro só por ano, usando busca oculta por expressões como `de 2025`;
- prévia de PDF pelo Drive;
- prévia textual para `.doc`, `.docx`, `.txt` e `.rtf`;
- botão `Abrir inteiro` apontando para o arquivo público no Drive.

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

## Acervo Público

Pasta canônica para o site de busca. Diferente de `pdfs_por_lei_e_ato/`, ela
guarda uma única cópia de cada voto, então um voto com vários autos/atos não é
duplicado.

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
- `votos_relatores_pdfs/`
- `pdfs_por_lei_e_ato/`
- `votos_brutos/`
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
