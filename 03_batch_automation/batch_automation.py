#!/usr/bin/env python3
"""Batch file renamer with a reversible audit trail and PDF report.

Built for the boring, expensive task: renaming a folder of 300 scans, photos or
invoices into a consistent pattern, with proof of what happened.

Safety first
------------
* Dry-run by default: nothing moves until --apply.
* Collisions are detected before touching the disk.
* Every applied change is written to rename_manifest.csv, so --undo restores
  the original names exactly.

Usage
-----
    python batch_automation.py ./invoices --pattern "{date}_{name}{ext}" --apply
    python batch_automation.py ./invoices --regex "(\\d{4})-(\\d{2})" --replace "\\1_" --dry-run
    python batch_automation.py ./invoices --undo
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

LOG = logging.getLogger("batch_automation")
MANIFEST = "rename_manifest.csv"


@dataclass
class Plan:
    source: Path
    target: Path
    status: str  # ok | collision | unchanged


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def sanitize(name: str) -> str:
    """Strip characters that break on Windows/macOS/Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "unnamed"


def build_name(path: Path, pattern: str, index: int) -> str:
    stat = path.stat()
    values = {
        "name": path.stem,
        "ext": path.suffix.lower(),
        "date": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
        "time": datetime.fromtimestamp(stat.st_mtime).strftime("%H%M%S"),
        "size": str(stat.st_size),
        "n": str(index),
        "parent": path.parent.name,
    }
    rendered = pattern
    # Support zero-padded counters: {n:03d}
    counter = re.search(r"\{n:0(\d)d\}", pattern)
    if counter:
        width = int(counter.group(1))
        rendered = rendered.replace(counter.group(0), f"{index:0{width}d}")
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return sanitize(rendered)


def plan_regex(folder: Path, regex: str, replacement: str) -> list[Plan]:
    compiled = re.compile(regex)
    plans: list[Plan] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name == MANIFEST:
            continue
        new_stem = compiled.sub(replacement, path.stem, count=1)
        target = path.with_name(sanitize(new_stem) + path.suffix)
        status = "unchanged" if target == path else "ok"
        plans.append(Plan(path, target, status))
    return plans


def plan_pattern(folder: Path, pattern: str) -> list[Plan]:
    plans: list[Plan] = []
    files = [p for p in sorted(folder.iterdir()) if p.is_file() and p.name != MANIFEST]
    for index, path in enumerate(files, start=1):
        target = path.with_name(build_name(path, pattern, index))
        status = "unchanged" if target == path else "ok"
        plans.append(Plan(path, target, status))
    return plans


def detect_collisions(plans: list[Plan]) -> list[Plan]:
    taken: set[str] = set()
    for plan in plans:
        if plan.status != "ok":
            continue
        key = str(plan.target).lower()
        if plan.target.exists() and plan.target != plan.source or key in taken:
            plan.status = "collision"
        taken.add(key)
    return plans


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def apply_plans(plans: list[Plan], folder: Path) -> int:
    applied = 0
    manifest_rows = []
    for plan in plans:
        if plan.status != "ok":
            manifest_rows.append([str(plan.source), str(plan.target), plan.status, ""])
            continue
        try:
            plan.source.rename(plan.target)
            applied += 1
            manifest_rows.append([str(plan.source), str(plan.target), "applied", datetime.now().isoformat(timespec="seconds")])
        except OSError as exc:
            LOG.error("Failed to rename %s: %s", plan.source.name, exc)
            manifest_rows.append([str(plan.source), str(plan.target), f"error: {exc}", ""])

    with (folder / MANIFEST).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "target", "status", "applied_at"])
        writer.writerows(manifest_rows)
    return applied


def undo(folder: Path) -> int:
    manifest = folder / MANIFEST
    if not manifest.exists():
        LOG.error("No manifest found in %s", folder)
        return 2

    restored = 0
    with manifest.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["status"] != "applied":
                continue
            current, original = Path(row["target"]), Path(row["source"])
            if not current.exists():
                LOG.warning("Missing, skipped: %s", current.name)
                continue
            try:
                current.rename(original)
                restored += 1
            except OSError as exc:
                LOG.error("Could not restore %s: %s", current.name, exc)
    LOG.info("Restored %d file(s)", restored)
    return 0


# --------------------------------------------------------------------------
# PDF report
# --------------------------------------------------------------------------


def write_pdf(plans: list[Plan], out_path: Path, applied: int, mode: str) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Relatório de renomeação em lote",
    )

    counts = {"ok": 0, "collision": 0, "unchanged": 0}
    for plan in plans:
        counts[plan.status] = counts.get(plan.status, 0) + 1

    story = [
        Paragraph("Relatório de automação em lote", styles["Title"]),
        Spacer(1, 6 * mm),
        Paragraph(
            f"Modo: <b>{mode}</b> &nbsp;|&nbsp; Arquivos analisados: <b>{len(plans)}</b> "
            f"&nbsp;|&nbsp; Renomeados: <b>{applied}</b> &nbsp;|&nbsp; "
            f"Colisões: <b>{counts.get('collision', 0)}</b>",
            styles["Normal"],
        ),
        Paragraph(f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["Normal"]),
        Spacer(1, 6 * mm),
    ]

    data = [["#", "Original", "Novo nome", "Status"]]
    for index, plan in enumerate(plans[:200], start=1):
        data.append([str(index), plan.source.name, plan.target.name, plan.status])

    table = Table(data, colWidths=[12 * mm, 68 * mm, 68 * mm, 25 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
            ]
        )
    )
    story.append(table)
    if len(plans) > 200:
        story.append(Paragraph(f"... e mais {len(plans) - 200} arquivo(s).", styles["Italic"]))

    doc.build(story)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch rename files safely, with undo and a PDF report.")
    parser.add_argument("folder", help="Folder containing the files")
    parser.add_argument("--pattern", default=None, help='Template, e.g. "{date}_{name}{ext}"')
    parser.add_argument("--regex", default=None, help="Regex applied to the file stem")
    parser.add_argument("--replace", default="", help="Replacement for --regex")
    parser.add_argument("--apply", action="store_true", help="Actually rename (default is dry-run)")
    parser.add_argument("--undo", action="store_true", help="Revert the last applied run")
    parser.add_argument("--pdf", default=None, help="PDF report path (default: rename_report.pdf)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    folder = Path(args.folder)
    if not folder.is_dir():
        LOG.error("Not a folder: %s", folder)
        return 2

    if args.undo:
        return undo(folder)

    if not args.pattern and not args.regex:
        LOG.error("Provide --pattern or --regex")
        return 2

    if args.regex:
        plans = plan_regex(folder, args.regex, args.replace)
        mode = f"regex {args.regex!r} -> {args.replace!r}"
    else:
        plans = plan_pattern(folder, args.pattern)
        mode = f"pattern {args.pattern!r}"

    plans = detect_collisions(plans)
    for plan in plans:
        if plan.status == "ok":
            LOG.info("%s  ->  %s", plan.source.name, plan.target.name)

    applied = apply_plans(plans, folder) if args.apply else 0
    mode_label = mode if args.apply else f"DRY-RUN ({mode})"
    LOG.info("Planned: %d | Renamed: %d", len(plans), applied)

    pdf_path = Path(args.pdf) if args.pdf else folder / "rename_report.pdf"
    write_pdf(plans, pdf_path, applied, mode_label)
    LOG.info("Report: %s", pdf_path)
    if args.apply:
        LOG.info("Manifest: %s (use --undo to revert)", folder / MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
