"""Operator commands for a reviewed category migration. No implicit production target."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_bot.category_migration import read_google_spreadsheet, read_xlsx
from finance_bot.migration_apply import (
    MigrationPlan, _private_json, apply_to_copy, payload_snapshot,
    prepare_migration, restore_copy, snapshot_payload,
)
from finance_bot.migration_mapping import LabelDecision, ReviewedMapping, seed_legacy_taxonomy
from finance_bot.native_migration import install_copy, verify_and_finalize_copy


def _client(path: Path):
    import gspread
    return gspread.service_account(filename=str(path))


def _load(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    source = payload_snapshot(data["source"])
    fields = data["plan"]
    plan = MigrationPlan(fields["spreadsheet_id"], fields["source_fingerprint"],
                         fields["source_digest"], fields["plan_digest"],
                         payload_snapshot(fields["output"]))
    return source, plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "plan-native"):
        command = commands.add_parser(name)
        command.add_argument("--target-id", required=True)
        command.add_argument("--mapping", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "plan":
            command.add_argument("--xlsx", type=Path, required=True)
        else:
            command.add_argument("--credentials", type=Path, required=True)
    for name in ("apply-local", "install-copy"):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--original-script", type=Path, required=True)
        command.add_argument("--confirm", action="store_true", required=True)
        if name == "install-copy":
            command.add_argument("--credentials", type=Path, required=True)
            command.add_argument("--backup-id", required=True)
            command.add_argument("--maintenance-confirmed", action="store_true", required=True)
    command = commands.add_parser("restore-local")
    command.add_argument("--workspace", type=Path, required=True)
    command.add_argument("--confirm", action="store_true", required=True)
    command = commands.add_parser("finalize-copy")
    command.add_argument("--plan", type=Path, required=True)
    command.add_argument("--credentials", type=Path, required=True)
    command.add_argument("--script-proof", type=Path, required=True)
    command.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    if args.command in {"plan", "plan-native"}:
        source = (read_xlsx(args.xlsx) if args.command == "plan" else
                  read_google_spreadsheet(_client(args.credentials).open_by_key(args.target_id)))
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        reviewed = ReviewedMapping(
            mapping["source_fingerprint"], mapping["spreadsheet_id"], mapping["confirmed"],
            tuple(LabelDecision(**item) for item in mapping.get("decisions", [])),
        )
        if reviewed.spreadsheet_id != args.target_id:
            parser.error("The reviewed mapping belongs to another target.")
        registry = seed_legacy_taxonomy(source, args.target_id)
        plan = prepare_migration(source, registry, reviewed)
        _private_json(args.output, {"source": snapshot_payload(source), "plan": {
            "spreadsheet_id": plan.spreadsheet_id, "source_fingerprint": plan.source_fingerprint,
            "source_digest": plan.source_digest, "plan_digest": plan.plan_digest,
            "output": snapshot_payload(plan.output),
        }})
        print("Reviewed migration plan saved. No workbook changed.")
    elif args.command == "restore-local":
        restore_copy(args.workspace, confirmed=args.confirm)
        print("Local source snapshot and original script recovered from backup.")
    else:
        source, plan = _load(args.plan)
        if args.command == "apply-local":
            apply_to_copy(source, plan, args.workspace, args.original_script, confirmed=args.confirm)
            print("Local copy verified. Native formula and Apps Script verification are still required.")
        elif args.command == "install-copy":
            client = _client(args.credentials)
            install_copy(source, plan, client.open_by_key(plan.spreadsheet_id),
                         client.open_by_key(args.backup_id), args.workspace,
                         args.original_script, confirmed=args.confirm,
                         maintenance_confirmed=args.maintenance_confirmed)
            print("Copy installed in maintenance. Install Apps Script and run verifyFinanceBotMigration.")
        else:
            client = _client(args.credentials)
            proof = json.loads(args.script_proof.read_text(encoding="utf-8"))
            verify_and_finalize_copy(source, plan, client.open_by_key(plan.spreadsheet_id),
                                     proof, confirmed=args.confirm)
            print("Native formulas verified; ready/open published as final write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
