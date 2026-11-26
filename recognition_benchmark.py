"""Score saved recognition results against independently labelled objects offline."""

import argparse
import csv
from decimal import Decimal
import json
import math
from pathlib import Path
import re
from statistics import median
import unicodedata


FIELDS = ("identity", "brand", "model", "ocr", "period", "material", "country",
          "condition", "completeness", "defects", "category_id")
MEASUREMENTS = ("weight_kg", "length_cm", "width_cm", "height_cm")
PHOTO_FIELDS = ("product_photos", "evidence_photos")
PARITY_FIELDS = ("identity", "brand", "model", "material", "condition", "defects", *MEASUREMENTS, "category_id")
MINIMUM_OBJECTS = 50
UNRESOLVED_IDENTITIES = {"", "unknown", "unresolved", "невідомо", "не визначено"}


def normalize(value, field):
    value = unicodedata.normalize("NFC", value)
    if field == "ocr":
        return value.replace("\r\n", "\n").strip()
    return " ".join(value.split()).casefold()


def nonnegative(value):
    return type(value) in (int, float) and 0 <= value <= 10**12


def index_rows(rows, *, reference, allow_partial_categories=False):
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
                if field == "identity" or field in row or (field == "category_id" and not allow_partial_categories):
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
        measurements = row.get("measurement", {})
        if not isinstance(measurements, dict) or measurements.keys() - set(MEASUREMENTS):
            raise ValueError(f"{lot}: invalid measurement fields")
        for field, value in measurements.items():
            if reference:
                valid = (isinstance(value, dict) and nonnegative(value.get("value"))
                         and value["value"] > 0 and nonnegative(value.get("tolerance")))
            else:
                valid = value is None or nonnegative(value) and value > 0
            if not valid:
                raise ValueError(f"{lot}: invalid {field} value or tolerance")
        for field in PHOTO_FIELDS:
            if field in row and (not isinstance(row[field], list) or any(
                type(n) is not int or not 1 <= n <= 32 for n in row[field]
            ) or len(set(row[field])) != len(row[field])):
                raise ValueError(f"{lot}: invalid {field}")
        if set(row.get("product_photos", ())) & set(row.get("evidence_photos", ())):
            raise ValueError(f"{lot}: a photo cannot have both roles")
        if row.get("main_photo") is not None:
            number = row["main_photo"]
            if type(number) is not int or not 1 <= number <= 32:
                raise ValueError(f"{lot}: invalid main_photo")
            if reference and "product_photos" in row and number not in row["product_photos"]:
                raise ValueError(f"{lot}: reference main_photo must be a product photo")
        if reference and "title_terms" in row:
            terms = row["title_terms"]
            if not isinstance(terms, list) or not terms or any(
                not isinstance(term, str) or not term.strip() for term in terms
            ):
                raise ValueError(f"{lot}: title_terms requires an array of required terms")
        if row.get("seo_passed") is not None and type(row["seo_passed"]) is not bool:
            raise ValueError(f"{lot}: seo_passed must be a boolean or null")
        if not reference and row.get("title") is not None and not isinstance(row["title"], str):
            raise ValueError(f"{lot}: title must be a string or null")
        for field in ("elapsed_ms", "cost_usd", "manual_actions"):
            if row.get(field) is not None and not nonnegative(row[field]):
                raise ValueError(f"{lot}: invalid {field}")
        indexed[lot] = row
    return indexed


def cohort_references(path):
    """Load the independent CSV manifest without scoring catalogue measurements as specimen facts."""
    with path.open(encoding="utf-8", newline="") as source:
        rows = []
        for record in csv.DictReader(source):
            if not record.get("reference_id") or not record.get("object_type"):
                raise ValueError("Cohort requires reference_id and object_type")
            row = {"lot": record["reference_id"], "identity": [record["object_type"]]}
            if record.get("model_scope") == "exact":
                for field in ("brand", "model"):
                    if record.get(field):
                        row[field] = [record[field]]
            if record.get("material"):
                row["material"] = [record["material"]]
            if record.get("category_id"):
                row["category_id"] = [record["category_id"]]
            rows.append(row)
        return rows


def score(reference, predictions, *, minimum_admission_rate=None, allow_partial_categories=False):
    if minimum_admission_rate is not None and (
        not nonnegative(minimum_admission_rate) or not 0 < minimum_admission_rate <= 1
    ):
        raise ValueError("Minimum admission rate must be greater than zero and at most one")
    labels = index_rows(reference, reference=True, allow_partial_categories=allow_partial_categories)
    results = index_rows(predictions, reference=False)
    if not labels:
        raise ValueError("Reference dataset is empty")
    extra = results.keys() - labels.keys()
    if extra:
        raise ValueError(f"Predictions outside reference dataset: {', '.join(sorted(extra))}")

    fields = (*FIELDS, *MEASUREMENTS, *PHOTO_FIELDS, "main_photo", "title_terms", "seo_passed")
    metrics = {field: {"correct": 0, "labelled": 0} for field in fields}
    unknown_by_field = dict.fromkeys(fields, 0)
    admitted = 0
    abstained = 0
    critical_categories = []
    admitted_without_category_reference = []
    mismatches = {}
    for lot, label in labels.items():
        result = results.get(lot, {})
        abstained += int(normalize(result.get("identity") or "", "identity") in UNRESOLVED_IDENTITIES)
        checks = {}
        for field in FIELDS:
            if field not in label:
                continue
            accepted = {normalize(value, field) for value in label[field]}
            prediction = normalize(result.get(field) or "", field)
            unknown_by_field[field] += int(prediction in UNRESOLVED_IDENTITIES)
            checks[field] = prediction in accepted
        for field, expected in label.get("measurement", {}).items():
            value = result.get("measurement", {}).get(field)
            unknown_by_field[field] += int(value is None)
            checks[field] = value is not None and (
                abs(Decimal(str(value)) - Decimal(str(expected["value"]))) <= Decimal(str(expected["tolerance"]))
            )
        for field in PHOTO_FIELDS:
            if field in label:
                unknown_by_field[field] += int(field not in result)
                checks[field] = field in result and set(result[field]) == set(label[field])
        if label.get("main_photo") is not None:
            unknown_by_field["main_photo"] += int(result.get("main_photo") is None)
            checks["main_photo"] = result.get("main_photo") == label["main_photo"]
        if "title_terms" in label:
            title = (result.get("title") or "").strip()
            normalized = normalize(title, "title")
            unknown_by_field["title_terms"] += int(not title)
            checks["title_terms"] = 0 < len(title) <= 80 and all(
                re.search(r"(?<!\w)" + re.escape(normalize(term, "title")) + r"(?!\w)", normalized)
                for term in label["title_terms"]
            )
        if label.get("seo_passed") is not None:
            unknown_by_field["seo_passed"] += int(result.get("seo_passed") is None)
            checks["seo_passed"] = result.get("seo_passed") is label["seo_passed"]
        errors = [field for field, correct in checks.items() if not correct]
        for field, correct in checks.items():
            metrics[field]["labelled"] += 1
            metrics[field]["correct"] += int(correct)
        if errors:
            mismatches[lot] = errors
        if result.get("admitted"):
            admitted += 1
            if "category_id" in errors:
                critical_categories.append(lot)
            elif "category_id" not in label:
                admitted_without_category_reference.append(lot)

    total = len(labels)
    identity_target_met = total >= MINIMUM_OBJECTS and metrics["identity"]["correct"] * 10 >= total * 9
    operations = {}
    for field in ("elapsed_ms", "cost_usd", "manual_actions"):
        values = [row[field] for row in results.values() if row.get(field) is not None]
        operations[field] = {"reported": len(values), "total": math.fsum(values) if values else None,
                             "median": median(values) if values else None}
    return {
        "objects": total,
        "admitted": admitted,
        "admission_rate": admitted / total,
        "minimum_admission_rate": minimum_admission_rate,
        "missing_predictions": sorted(labels.keys() - results.keys()),
        "fields": metrics,
        "unknown_by_field": unknown_by_field,
        "identity_abstentions": abstained,
        "identity_abstention_rate": abstained / total,
        "identity_target_met": identity_target_met,
        "critical_categories": critical_categories,
        "admitted_without_category_reference": admitted_without_category_reference,
        "mismatches": mismatches,
        "operations": operations,
        "passed": (
            identity_target_met
            and metrics["category_id"]["labelled"] == total
            and not critical_categories
            and minimum_admission_rate is not None
            and admitted / total >= minimum_admission_rate
        ),
    }


def compare_systems(reference, lotnexo, chatgpt, *, minimum_admission_rate=None):
    """Compare field-by-field against one locked reference set."""
    ours = score(reference, lotnexo, minimum_admission_rate=minimum_admission_rate,
                 allow_partial_categories=True)
    theirs = score(reference, chatgpt, allow_partial_categories=True)
    unlabelled = [field for field in PARITY_FIELDS if not ours["fields"][field]["labelled"]]
    fields = {}
    for field in (*FIELDS, *MEASUREMENTS):
        measured, baseline = ours["fields"][field], theirs["fields"][field]
        count = measured["labelled"]
        fields[field] = {
            "labelled": count, "lotnexo_correct": measured["correct"], "chatgpt_correct": baseline["correct"],
            "lotnexo_unknown": ours["unknown_by_field"][field],
            "chatgpt_unknown": theirs["unknown_by_field"][field],
            "at_least_baseline": measured["correct"] >= baseline["correct"] if count else None,
        }
    complete = (ours["objects"] >= MINIMUM_OBJECTS and not unlabelled
                and not ours["missing_predictions"] and not theirs["missing_predictions"])
    meets_baseline = all(result["at_least_baseline"] for result in fields.values() if result["labelled"]) if complete else None
    return {"lotnexo": ours, "chatgpt": theirs, "paired_fields": fields, "unlabelled_fields": unlabelled,
            "meets_chatgpt_baseline": meets_baseline,
            "passed": ours["passed"] and meets_baseline is True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--min-admission-rate", type=float, help="Threshold agreed before acceptance (0 < rate <= 1)")
    parser.add_argument("--chatgpt-predictions", type=Path, help="Score the frozen photo pairs against this baseline.")
    args = parser.parse_args()
    try:
        manifest = args.reference.suffix.casefold() == ".csv"
        reference = cohort_references(args.reference) if manifest else json.loads(args.reference.read_text(encoding="utf-8"))
        predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
        if args.chatgpt_predictions:
            chatgpt = json.loads(args.chatgpt_predictions.read_text(encoding="utf-8"))
            report = compare_systems(reference, predictions, chatgpt, minimum_admission_rate=args.min_admission_rate)
        else:
            report = score(reference, predictions, minimum_admission_rate=args.min_admission_rate,
                           allow_partial_categories=manifest)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
