# Automação em Python — portfólio

Três utilitários de produção para as tarefas que mais aparecem em freelance:
**extrair dados da web**, **limpar planilhas** e **automatizar arquivos em lote**.

Tudo roda local, sem API paga, sem credencial, sem dependência de serviço externo.

| # | Projeto | Problema que resolve | Saída |
|---|---------|----------------------|-------|
| 1 | `01_price_scraper` | "Preciso da tabela de preços do site X, atualizada toda semana" | CSV normalizado com valor numérico + moeda |
| 2 | `02_csv_cleaner` | "Essa planilha está um caos, não consigo importar no sistema" | CSV limpo + log de auditoria de cada célula alterada |
| 3 | `03_batch_automation` | "Tenho 400 arquivos com nome errado" | Arquivos renomeados + relatório PDF + rollback |

---

## 1. Price scraper (`01_price_scraper/price_scraper.py`)

Extrai tabelas de preço de qualquer página HTML via **seletores CSS** — sem
alterar código para cada cliente.

```bash
python price_scraper.py --demo                      # roda offline, sem rede
python price_scraper.py \
  --url https://exemplo.com/produtos \
  --item-selector "div.product" \
  --name-selector "h3" \
  --price-selector "span.price" \
  --next-selector "a.next" \
  --max-pages 5 \
  --out precos.csv
```

**O que tem por dentro**

- Converte `R$ 1.234,56`, `$51.77`, `1 234,56 €` em `1234.56` + código ISO da moeda
  (o cliente consegue somar e dar `SORT` no Excel direto).
- Retry com backoff exponencial + jitter em 429/5xx e timeout.
- Respeita `robots.txt` por padrão (só ignora com permissão escrita).
- Delay configurável entre requisições, paginação automática, `User-Agent` identificável.
- Saída em `utf-8-sig` (abre no Excel sem acento quebrado).

## 2. CSV cleaner (`02_csv_cleaner/csv_cleaner.py`)

Recebe a planilha bagunçada e devolve o CSV pronto para importar — **com prova
de tudo que foi alterado**.

```bash
python csv_cleaner.py --make-sample        # gera messy.csv de exemplo
python csv_cleaner.py messy.csv            # gera messy.clean.csv + messy.changes.csv
python csv_cleaner.py messy.csv --dry-run  # só o diagnóstico
```

Antes → depois:

```
  João da Silva ,JOAO@EXEMPLO.COM ,(11) 98888-7777,"R$ 1.234,56",15/03/2024,SIM
  ↓
João da Silva,joao@exemplo.com,+5511988887777,1234.56 BRL,2024-03-15,true
```

**Regras aplicadas** (cada uma registrada no changelog)

- Cabeçalhos: remove BOM, acento, espaço e duplicidade → `snake_case`.
- Células: trim, colapso de espaço duplo, remoção de caractere invisível (`\u200b`).
- Tipos inferidos por coluna: data, moeda, booleano, e-mail, telefone, inteiro.
- Datas em 8 formatos → ISO `YYYY-MM-DD`.
- Telefone → E.164 (`+5511988887777`).
- Booleano → `true` / `false` (`sim`, `S`, `1`, `não`, `N`...).
- Remove linha vazia, remove duplicata exata, avisa linha com coluna desalinhada.

Saída extra: `quality score` 0–100 e contagem de células vazias antes/depois.

## 3. Batch automation (`03_batch_automation/batch_automation.py`)

Renomeia arquivos em lote com padrão ou regex, **reversível**.

```bash
python batch_automation.py ./notas --pattern "{date}_{n:03d}{ext}"        # dry-run
python batch_automation.py ./notas --pattern "{date}_{n:03d}{ext}" --apply
python batch_automation.py ./notas --undo                                 # desfaz tudo
```

- `{name} {ext} {date} {time} {size} {n} {parent}` como variáveis, com contador
  zero-padded (`{n:03d}`).
- Modo regex: `--regex "(\d{4})-(\d{2})" --replace "\1_"`.
- **Dry-run é o padrão** — nada toca no disco sem `--apply`.
- Detecta colisão de nome antes de renomear.
- `rename_manifest.csv` permite `--undo` exato.
- Gera `rename_report.pdf` com tabela de original → novo → status.

---

## Instalação

```bash
python -m pip install -r requirements.txt
```

Python 3.10+ (testado em 3.14). Dependências: `requests`, `beautifulsoup4`,
`lxml`, `reportlab`.

## Testado

```
01: demo offline -> 5 linhas, 5 preços parseados
02: 7 linhas -> 5 (1 duplicata, 1 linha vazia), 33 células corrigidas, score 95.7/100
03: 5 arquivos renomeados, PDF gerado, --undo restaurou os 5 nomes originais
```

## Contato

Disponível para automações de até 24h. Envie o arquivo/site e eu devolvo o
script + o resultado.
