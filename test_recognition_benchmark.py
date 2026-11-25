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
        report = score(self.reference, self.predictions[:45], minimum_admission_rate=.8)
        self.assertTrue(report["passed"])
        self.assertEqual(report["fields"]["identity"], {"correct": 45, "labelled": 50})
        self.assertEqual(report["admission_rate"], 0.9)
        self.assertEqual(report["missing_predictions"], ["45", "46", "47", "48", "49"])
        self.assertEqual(report["identity_abstentions"], 5)
        self.assertEqual(report["identity_abstention_rate"], .1)
        self.predictions[44]["identity"] = " Unknown "
        explicit = score(self.reference, self.predictions[:45], minimum_admission_rate=.8)
        self.assertEqual(explicit["identity_abstentions"], 6)
        self.assertFalse(explicit["passed"])
        self.assertFalse(score(self.reference, self.predictions[:44], minimum_admission_rate=.8)["passed"])
        self.assertFalse(score(self.reference[:49], self.predictions[:49], minimum_admission_rate=.8)["passed"])

    def test_category_and_ocr_are_scored_separately(self):
        self.reference[0]["ocr"] = ["СВ 12"]  # Cyrillic letters
        self.predictions[0].update(ocr="CB 12", category_id="9999")
        report = score(self.reference, self.predictions, minimum_admission_rate=.8)
        self.assertFalse(report["passed"])
        self.assertEqual(report["mismatches"]["0"], ["ocr", "category_id"])
        self.assertEqual(report["fields"]["ocr"], {"correct": 0, "labelled": 1})
        self.assertEqual(report["critical_categories"], ["0"])
        self.predictions[0]["admitted"] = False
        self.assertTrue(score(self.reference, self.predictions, minimum_admission_rate=.8)["passed"])

    def test_quality_requires_a_predefined_admission_rate(self):
        self.assertFalse(score(self.reference, self.predictions)["passed"])
        for prediction in self.predictions:
            prediction["admitted"] = False
        report = score(self.reference, self.predictions, minimum_admission_rate=.8)
        self.assertEqual(report["critical_categories"], [])
        self.assertEqual(report["fields"]["identity"]["correct"], 50)
        self.assertFalse(report["passed"])
        with self.assertRaisesRegex(ValueError, "admission rate"):
            score(self.reference, self.predictions, minimum_admission_rate=0)

    def test_catalogue_and_item_specific_attributes_keep_separate_scores(self):
        self.reference[0].update(brand=['Brand'], model=['Model 2'], condition=['USED'],
                                 country=['Japan'], completeness=['Incomplete'], defects=['Cracked cap'])
        self.predictions[0].update(brand='brand', model='Model 2 W', condition='NEW',
                                   country='Japan', defects='Cracked cap')
        report = score(self.reference, self.predictions, minimum_admission_rate=.8)
        self.assertEqual(report['mismatches']['0'], ['model', 'condition', 'completeness'])
        self.assertEqual(report['fields']['brand'], {'correct': 1, 'labelled': 1})
        self.assertEqual(report['fields']['defects'], {'correct': 1, 'labelled': 1})

    def test_scores_measurement_photo_roles_title_and_operation_coverage(self):
        self.reference[0].update(
            measurement={"weight_kg": {"value": .25, "tolerance": .01},
                         "length_cm": {"value": 10, "tolerance": .1}},
            product_photos=[2, 3], evidence_photos=[1], main_photo=3, title_terms=["film camera"],
            seo_passed=True,
        )
        self.predictions[0].update(
            measurement={"weight_kg": .26}, product_photos=[3, 2], evidence_photos=[1],
            main_photo=1, title="A photo of a film cameraphone", elapsed_ms=100,
            seo_passed=False, cost_usd=0,
        )
        report = score(self.reference, self.predictions, minimum_admission_rate=.8)
        self.assertEqual(report["mismatches"]["0"], ["length_cm", "main_photo", "title_terms", "seo_passed"])
        self.assertEqual(report["fields"]["weight_kg"]["correct"], 1)
        self.assertEqual(report["fields"]["product_photos"]["correct"], 1)
        self.assertEqual(report["operations"]["elapsed_ms"], {"reported": 1, "total": 100, "median": 100})
        self.assertEqual(report["operations"]["cost_usd"]["total"], 0)
        self.assertIsNone(report["operations"]["manual_actions"]["total"])

    def test_duplicate_and_unlabelled_lots_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            score(self.reference + self.reference[:1], self.predictions)
        with self.assertRaisesRegex(ValueError, "outside reference"):
            score(self.reference, self.predictions + [{"lot": "extra", "admitted": False}])
