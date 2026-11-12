from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from finance_bot.category_schema import TRANSACTION_HEADERS
from finance_bot.ledger import reconcile_rows
from finance_bot.taxonomy import Category, HistoricalPair, Subcategory, Taxonomy, new_id


class ReconcileContractTests(unittest.TestCase):
    def test_python_and_apps_script_share_exact_row_results(self):
        self.assertIsNotNone(shutil.which("node"), "Node is required for the Apps Script contract suite")
        parent = Category(new_id(), "Расход", "Продукты")
        second = Category(new_id(), "Расход", "Кафе")
        child = Subcategory(new_id(), parent.category_id, "Доставка")
        registry = Taxonomy("copy", 0, (parent, second), (child,))
        original = ["2026-11-01", parent.name, child.name, 100, "Расход", ""]
        first = reconcile_rows([list(TRANSACTION_HEADERS), original], registry, {})[0]
        fixed = [*original, *first.service_values()]
        snapshots = {first.transaction_id: first.identity()}
        moved = replace(registry, subcategories=(replace(child, current_category_id=second.category_id),), historical_pairs=(HistoricalPair(parent.category_id, child.subcategory_id),))
        cases = [
            (registry, [fixed], snapshots),
            (moved, [fixed], snapshots),
            (moved, [[*fixed[:1], "Кафе", *fixed[2:]]], snapshots),
            (registry, [fixed, fixed], snapshots),
            (registry, [["bad", *fixed[1:]]], snapshots),
            (registry, [[*fixed[:3], 0, *fixed[4:]]], snapshots),
            (registry, [[""] * 11], snapshots),
            (registry, [fixed] + [[""] * 11 for _ in range(160)] + [fixed[:8] + [new_id()] + fixed[9:]], snapshots),
        ]
        payload, expected = [], []
        for taxonomy, rows, trusted in cases:
            source = [list(TRANSACTION_HEADERS), *rows]
            expected.append([item.service_values() for item in reconcile_rows(source, taxonomy, trusted)])
            payload.append({"rows": source, "next_id": new_id(),
                "registry": {
                    "categories": [{"category_id": item.category_id, "type": item.transaction_type, "name": item.name, "active": item.active, "deactivated_period": str(item.deactivated_period) if item.deactivated_period else ""} for item in taxonomy.categories],
                    "children": [asdict(item) for item in taxonomy.subcategories],
                    "history": [asdict(item) for item in taxonomy.historical_pairs],
                }, "snapshots": [asdict(item) for item in trusted.values()]})
        process = subprocess.run(["node", "tests/reconcile_contract.cjs"], input=json.dumps(payload), capture_output=True, text=True, check=True)
        observed = json.loads(process.stdout)
        self.assertEqual([item["service"] for item in observed], expected)


if __name__ == "__main__":
    unittest.main()
