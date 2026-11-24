import unittest

from recognition_benchmark import score


class RecognitionBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.reference = [
            {"lot": str(i), "identity": ["film camera"], "category_id": ["3323"]}
            for i in range(50)
        ]
        self.predictions = [
            {"lot": str(i), "identity": "Film Camera", "category_id": "3323", "admitted": True}
            for i in range(50)
        ]

    def test_missing_results_count_against_identity_threshold(self):
        report = score(self.reference, self.predictions[:45])
        self.assertTrue(report["passed"])
        self.assertEqual(report["fields"]["identity"], {"correct": 45, "labelled": 50})
        self.assertEqual(report["admission_rate"], 0.9)
        self.assertEqual(report["missing_predictions"], ["45", "46", "47", "48", "49"])
        self.assertFalse(score(self.reference, self.predictions[:44])["passed"])
        self.assertFalse(score(self.reference[:49], self.predictions[:49])["passed"])

    def test_category_and_ocr_are_scored_separately(self):
        self.reference[0]["ocr"] = ["СВ 12"]  # Cyrillic letters
        self.predictions[0].update(ocr="CB 12", category_id="9999")
        report = score(self.reference, self.predictions)
        self.assertFalse(report["passed"])
        self.assertEqual(report["mismatches"]["0"], ["ocr", "category_id"])
        self.assertEqual(report["fields"]["ocr"], {"correct": 0, "labelled": 1})
        self.assertEqual(report["critical_categories"], ["0"])
        self.predictions[0]["admitted"] = False
        self.assertTrue(score(self.reference, self.predictions)["passed"])

    def test_duplicate_and_unlabelled_lots_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            score(self.reference + self.reference[:1], self.predictions)
        with self.assertRaisesRegex(ValueError, "outside reference"):
            score(self.reference, self.predictions + [{"lot": "extra", "admitted": False}])
