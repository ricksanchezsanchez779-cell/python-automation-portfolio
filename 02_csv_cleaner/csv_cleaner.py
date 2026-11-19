#!/usr/bin/env python3
"""Messy CSV -> clean CSV, with a full audit trail.

Every cell that changes is written to a changelog (CSV), so the client can
review exactly what was modified before trusting the output.

Pipeline
--------
1. Header normalization  (BOM, accents, spaces, duplicates)
2. Cell hygiene          (trim, collapse whitespace, drop zero-width chars)
3. Type inference        (date / currency / boolean / email / phone / number)
4. Type-specific cleanup (ISO dates, numeric currency, E.164-ish phones)
5. Structural cleanup    (drop empty rows, drop exact duplicates)

Usage
-----
    python csv_cleaner.py --make-sample
    python csv_cleaner.py messy.csv --out clean.csv --changelog changes.csv
    python csv_cleaner.py messy.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("csv_cleaner")

DATE_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

TRUE_VALUES = {"sim", "s", "yes", "y", "true", "1", "verdadeiro"}
FALSE_VALUES = {"nao", "n", "no", "false", "0", "falso", "não"}

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]{2,}$")
PHONE_RE = re.compile(r"^\+?[\d\s().-]{9,20}$")
CURRENCY_RE = re.compile(r"^\s*(R\$|US\$|\$|£|€)?\s*[-+]?[\d.,'\s]+\s*(BRL|USD|GBP|EUR)?\s*$", re.I)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")

CURRENCY_MAP = {"R$": "BRL", "US$": "USD", "$": "USD", "£": "GBP", "€": "EUR"}


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class Change:
    row: int
    column: str
    rule: str
    before: str
    after: str


@dataclass
class Report:
    rows_in: int = 0
    rows_out: int = 0
    columns_in: int = 0
    empty_cells_in: int = 0
    empty_cells_out: int = 0
    duplicates_removed: int = 0
    changes: list[Change] = field(default_factory=list)

    @property
    def cells_in(self) -> int:
        return self.rows_in * self.columns_in


# --------------------------------------------------------------------------
# Normalizers
# --------------------------------------------------------------------------


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_header(raw: str, index: int, seen: dict[str, int]) -> str:
    text = strip_accents(ZERO_WIDTH_RE.sub("", raw or "")).strip().lower()
    text = re.sub(r"[^\w]+", "_", text).strip("_")
    if not text:
        text = f"column_{index + 1}"
    if text in seen:
        seen[text] += 1
        text = f"{text}_{seen[text]}"
    else:
        seen[text] = 0
    return text


def clean_cell(value: str) -> str:
    text = ZERO_WIDTH_RE.sub("", value or "")
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_date(text: str) -> str | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_money(text: str) -> str | None:
    if not CURRENCY_RE.match(text):
        return None
    currency = None
    upper = text.upper()
    for symbol, code in CURRENCY_MAP.items():
        if symbol in upper:
            currency = code
            break
    for code in ("BRL", "USD", "GBP", "EUR"):
        if re.search(rf"\b{code}\b", upper):
            currency = code

    digits = re.sub(r"[^\d,.-]", "", text)
    if not digits or not re.search(r"\d", digits):
        return None

    has_comma, has_dot = "," in digits, "." in digits
    if has_comma and has_dot:
        decimal = "," if digits.rfind(",") > digits.rfind(".") else "."
        digits = digits.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif has_comma:
        head, _, tail = digits.rpartition(",")
        # "1,234" is a thousands separator; "12,5" and "89,90" are decimals.
        digits = f"{head}.{tail}" if len(tail) in (1, 2) and head else digits.replace(",", "")
    elif has_dot:
        head, _, tail = digits.rpartition(".")
        if len(tail) == 3 and head:
            digits = digits.replace(".", "")

    try:
        value = float(digits)
    except ValueError:
        return None
    formatted = f"{value:.2f}"
    return f"{formatted} {currency}" if currency else formatted


def parse_phone(text: str) -> str | None:
    if not PHONE_RE.match(text):
        return None
    digits = re.sub(r"\D", "", text)
    if not 10 <= len(digits) <= 13:
        return None
    if text.strip().startswith("+"):
        return "+" + digits
    if len(digits) in (10, 11):
        return "+55" + digits
    if digits.startswith("55") and len(digits) in (12, 13):
        return "+" + digits
    return "+" + digits


def infer_type(values: list[str]) -> str:
    sample = [v for v in values if v][:200]
    if not sample:
        return "text"

    def ratio(predicate) -> float:
        return sum(1 for v in sample if predicate(v)) / len(sample)

    if ratio(lambda v: v.lower() in TRUE_VALUES | FALSE_VALUES) >= 0.95:
        return "boolean"
    if ratio(lambda v: parse_date(v) is not None) >= 0.95:
        return "date"
    if ratio(lambda v: EMAIL_RE.match(v) is not None) >= 0.95:
        return "email"
    if ratio(lambda v: parse_money(v) is not None) >= 0.95:
        return "currency"
    if ratio(lambda v: parse_phone(v) is not None) >= 0.95:
        return "phone"
    if ratio(lambda v: re.fullmatch(r"[-+]?\d+", v) is not None) >= 0.95:
        return "integer"
    return "text"


def normalize_value(value: str, kind: str) -> tuple[str, str | None]:
    """Return (new_value, rule_applied_or_None)."""
    if not value:
        return value, None

    if kind == "date":
        parsed = parse_date(value)
        if parsed and parsed != value:
            return parsed, "date_to_iso"
    elif kind == "currency":
        parsed = parse_money(value)
        if parsed and parsed != value:
            return parsed, "currency_to_numeric"
    elif kind == "boolean":
        lowered = value.lower()
        if lowered in TRUE_VALUES:
            return "true", "boolean_normalized"
        if lowered in FALSE_VALUES:
            return "false", "boolean_normalized"
    elif kind == "email":
        lowered = value.lower()
        if lowered != value:
            return lowered, "email_lowercased"
    elif kind == "phone":
        parsed = parse_phone(value)
        if parsed and parsed != value:
            return parsed, "phone_to_e164"
    elif kind == "integer":
        normalized = str(int(value))
        if normalized != value:
            return normalized, "integer_normalized"

    # Safety net: an e-mail is lowercased even in a column typed as free text.
    if kind == "text" and EMAIL_RE.match(value) and value != value.lower():
        return value.lower(), "email_lowercased"
    return value, None


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def clean_table(header: list[str], rows: list[list[str]], report: Report) -> tuple[list[str], list[list[str]]]:
    seen_headers: dict[str, int] = {}
    clean_header = [normalize_header(h, i, seen_headers) for i, h in enumerate(header)]
    report.columns_in = len(header)
    report.rows_in = len(rows)

    # 1. Cell hygiene
    hygienic: list[list[str]] = []
    for row_index, row in enumerate(rows, start=1):
        if len(row) != len(header):
            LOG.warning(
                "Row %d has %d fields, header has %d. Padded/truncated — check for unquoted commas.",
                row_index,
                len(row),
                len(header),
            )
        padded = list(row) + [""] * (len(header) - len(row))
        new_row = []
        for col_index, value in enumerate(padded[: len(header)]):
            cleaned = clean_cell(value)
            if cleaned != value:
                report.changes.append(
                    Change(row_index, clean_header[col_index], "whitespace_cleanup", value, cleaned)
                )
            new_row.append(cleaned)
        hygienic.append(new_row)

    report.empty_cells_in = sum(1 for row in hygienic for cell in row if not cell)

    # 2. Type inference per column
    kinds = [
        infer_type([row[col] for row in hygienic])
        for col in range(len(clean_header))
    ]
    LOG.info("Column types: %s", ", ".join(f"{h}={k}" for h, k in zip(clean_header, kinds)))

    # 3. Type-specific normalization
    normalized: list[list[str]] = []
    for row_index, row in enumerate(hygienic, start=1):
        new_row = []
        for col_index, value in enumerate(row):
            new_value, rule = normalize_value(value, kinds[col_index])
            if rule:
                report.changes.append(Change(row_index, clean_header[col_index], rule, value, new_value))
            new_row.append(new_value)
        normalized.append(new_row)

    # 4. Drop empty rows
    non_empty = [row for row in normalized if any(cell for cell in row)]
    for removed in range(len(normalized) - len(non_empty)):
        report.changes.append(Change(0, "*", "empty_row_dropped", "", ""))

    # 5. Drop exact duplicates (keep first)
    deduped: list[list[str]] = []
    seen_rows: set[tuple[str, ...]] = set()
    for row_index, row in enumerate(non_empty, start=1):
        key = tuple(row)
        if key in seen_rows:
            report.duplicates_removed += 1
            report.changes.append(Change(row_index, "*", "duplicate_row_dropped", "|".join(row), ""))
            continue
        seen_rows.add(key)
        deduped.append(row)

    report.rows_out = len(deduped)
    report.empty_cells_out = sum(1 for row in deduped for cell in row if not cell)
    return clean_header, deduped


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_changelog(path: Path, changes: list[Change]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["row", "column", "rule", "before", "after"])
        for change in changes:
            writer.writerow([change.row, change.column, change.rule, change.before, change.after])


def quality_score(report: Report) -> float:
    """0-100 heuristic: fewer empty cells and zero duplicates is better."""
    if report.cells_in == 0:
        return 0.0
    empty_penalty = 40 * (report.empty_cells_out / max(1, report.rows_out * report.columns_in))
    dup_penalty = 30 * (report.duplicates_removed / max(1, report.rows_in))
    return round(max(0.0, 100 - empty_penalty - dup_penalty), 1)


# --------------------------------------------------------------------------
# Sample generator (so the script runs end-to-end with zero setup)
# --------------------------------------------------------------------------

MESSY_SAMPLE = """﻿Nome ,Email,Telefone,Valor,Data,Ativo
  João da Silva ,JOAO@EXEMPLO.COM ,(11) 98888-7777,"R$ 1.234,56",15/03/2024,SIM
Maria Souza,maria@exemplo.com,11977776666,"R$ 89,90",2024-03-16,sim
  João da Silva ,JOAO@EXEMPLO.COM ,(11) 98888-7777,"R$ 1.234,56",15/03/2024,SIM
,,,,
Carlos Lima,carlos@exemplo.com, +55 21 96666-5555 ,US$ 45.00,17/03/2024,Não
Ana  Paula ,ana.paula@exemplo.com,21955554444,"R$ 2.500,00",18/03/2024,S
Pedro Alves,pedro@exemplo,21 94444-3333,"R$ 12,50",19/03/2024,nao
"""


def make_sample(path: Path) -> None:
    path.write_text(MESSY_SAMPLE, encoding="utf-8")
    LOG.info("Sample written to %s", path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean a messy CSV and log every change.")
    parser.add_argument("input", nargs="?", help="Input CSV file")
    parser.add_argument("--out", default=None, help="Output CSV (default: <input>.clean.csv)")
    parser.add_argument("--changelog", default=None, help="Changelog CSV (default: <input>.changes.csv)")
    parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing")
    parser.add_argument("--make-sample", action="store_true", help="Create a messy sample CSV and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.make_sample:
        make_sample(Path("messy.csv"))
        return 0
    if not args.input:
        LOG.error("Input CSV required (or use --make-sample)")
        return 2

    source = Path(args.input)
    if not source.exists():
        LOG.error("File not found: %s", source)
        return 3

    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = list(csv.reader(handle))
    if not reader:
        LOG.error("Empty file")
        return 4

    header, rows = reader[0], reader[1:]
    report = Report()
    clean_header, clean_rows = clean_table(header, rows, report)

    score = quality_score(report)
    LOG.info("Rows: %d -> %d | duplicates removed: %d", report.rows_in, report.rows_out, report.duplicates_removed)
    LOG.info("Cells changed: %d | empty cells: %d -> %d", len(report.changes), report.empty_cells_in, report.empty_cells_out)
    LOG.info("Quality score: %.1f/100", score)

    if args.dry_run:
        LOG.info("Dry run: nothing written.")
        return 0

    out_path = Path(args.out) if args.out else source.with_suffix(".clean.csv")
    log_path = Path(args.changelog) if args.changelog else source.with_suffix(".changes.csv")
    write_csv(out_path, clean_header, clean_rows)
    write_changelog(log_path, report.changes)
    LOG.info("Clean file: %s", out_path)
    LOG.info("Audit log : %s", log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
