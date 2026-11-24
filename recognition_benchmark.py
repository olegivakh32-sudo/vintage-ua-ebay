"""Score saved recognition results against independently labelled objects offline."""

import argparse
import json
from pathlib import Path
import unicodedata


FIELDS = ("identity", "ocr", "period", "material", "category_id")
MINIMUM_OBJECTS = 50


def normalize(value, field):
    value = unicodedata.normalize("NFC", value)
    if field == "ocr":
        return value.replace("\r\n", "\n").strip()
    return " ".join(value.split()).casefold()


def index_rows(rows, *, reference):
    if not isinstance(rows, list):
        raise ValueError("Expected a JSON array of objects")
    indexed = {}
    for row in rows:
        lot = row.get("lot") if isinstance(row, dict) else None
        if not isinstance(lot, str) or not lot.strip() or lot != lot.strip() or lot in indexed:
            raise ValueError(f"Missing, invalid or duplicate lot ID: {lot!r}")
        if reference:
            for field in FIELDS:
                values = row.get(field)
                if field in {"identity", "category_id"} or field in row:
                    if not isinstance(values, list) or not values or any(
                        not isinstance(value, str) or not value.strip() for value in values
                    ):
                        raise ValueError(f"{lot}: {field} requires an array of accepted values")
        else:
            if type(row.get("admitted")) is not bool:
                raise ValueError(f"{lot}: admitted must be a boolean")
            for field in FIELDS:
                if row.get(field) is not None and not isinstance(row[field], str):
                    raise ValueError(f"{lot}: {field} must be a string or null")
        indexed[lot] = row
    return indexed


def score(reference, predictions):
    labels = index_rows(reference, reference=True)
    results = index_rows(predictions, reference=False)
    if not labels:
        raise ValueError("Reference dataset is empty")
    extra = results.keys() - labels.keys()
    if extra:
        raise ValueError(f"Predictions outside reference dataset: {', '.join(sorted(extra))}")

    metrics = {field: {"correct": 0, "labelled": 0} for field in FIELDS}
    admitted = 0
    critical_categories = []
    mismatches = {}
    for lot, label in labels.items():
        result = results.get(lot, {})
        errors = []
        for field in FIELDS:
            if field not in label:
                continue
            metrics[field]["labelled"] += 1
            accepted = {normalize(value, field) for value in label[field]}
            if normalize(result.get(field) or "", field) in accepted:
                metrics[field]["correct"] += 1
            else:
                errors.append(field)
        if errors:
            mismatches[lot] = errors
        if result.get("admitted"):
            admitted += 1
            if "category_id" in errors:
                critical_categories.append(lot)

    total = len(labels)
    return {
        "objects": total,
        "admitted": admitted,
        "admission_rate": admitted / total,
        "missing_predictions": sorted(labels.keys() - results.keys()),
        "fields": metrics,
        "critical_categories": critical_categories,
        "mismatches": mismatches,
        "passed": (
            total >= MINIMUM_OBJECTS
            and metrics["identity"]["correct"] * 10 >= total * 9
            and not critical_categories
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("predictions", type=Path)
    args = parser.parse_args()
    try:
        report = score(
            json.loads(args.reference.read_text(encoding="utf-8")),
            json.loads(args.predictions.read_text(encoding="utf-8")),
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
