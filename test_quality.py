import hashlib
import json
import re
import unittest
from unittest.mock import Mock, patch

from test_mobile import m


OBJECT_FIXTURES = (
    ("Fishing Reel", "Sporting Goods > Fishing", "Reels", "Fishing Rod Holders"),
    ("Photographic Enlarger", "Cameras & Photo > Darkroom", "Photographic Enlargers", "Enlarger Lenses"),
    ("Cigarette Case", "Collectibles > Tobacciana", "Cases", "Cigarettes"),
    ("Shoulder Board", "Collectibles > Militaria", "Insignia", "Military Uniforms"),
    ("Collar Tabs", "Collectibles > Militaria", "Collar Tabs", "Dog Collars"),
    ("Military Collar Patch", "Collectibles > Militaria", "Collar Patches", "Pet Collars"),
    ("Printed Display Card", "Collectibles > Paper", "Paper Ephemera", "Greeting Cards"),
    ("Automobile Picture Cards", "Collectibles > Paper", "Paper Ephemera", "Sports Trading Cards"),
    ("Collectible Paper Insert Lot", "Collectibles > Non-Sport Trading Cards", "Card Lots", "Singles"),
    ("Paper Insert", "Collectibles > Non-Sport Trading Cards", "Singles", "Card Lots"),
    ("Pocket Watch", "Jewelry & Watches > Watches", "Pocket Watches", "Pocket Watch Chains"),
    ("Wall Clock", "Home & Garden > Clocks", "Wall Clocks", "Clock Parts"),
    ("Film Camera", "Cameras & Photo > Cameras", "Film Cameras", "Camera Film"),
    ("Camera Lens", "Cameras & Photo > Lenses", "Camera Lenses", "Lens Caps"),
    ("Lens Cap", "Cameras & Photo > Accessories", "Lens Caps", "Camera Lenses"),
    ("Light Meter", "Cameras & Photo > Accessories", "Light Meters", "Light Meter Cases"),
    ("Slide Projector", "Cameras & Photo > Projection", "Slide Projectors", "Slide Trays"),
    ("Tripod Head", "Cameras & Photo > Tripods", "Tripod Heads", "Tripod Bags"),
    ("Desk Lamp", "Home & Garden > Lighting", "Desk Lamps", "Lamp Shades"),
    ("Lamp Shade", "Home & Garden > Lighting", "Lamp Shades", "Desk Lamps"),
    ("Candle Holder", "Home & Garden > Decor", "Candle Holders", "Candles"),
    ("Tea Pot", "Pottery & Glass > Serveware", "Tea Pots", "Tea Cups"),
    ("Tea Cup", "Pottery & Glass > Drinkware", "Tea Cups", "Tea Pots"),
    ("Coffee Grinder", "Home & Garden > Small Appliances", "Coffee Grinders", "Coffee Filters"),
    ("Serving Tray", "Home & Garden > Serveware", "Serving Trays", "Serving Bowls"),
    ("Serving Bowl", "Home & Garden > Serveware", "Serving Bowls", "Serving Trays"),
    ("Wall Plate", "Collectibles > Decorative Collectibles", "Wall Plates", "Plate Stands"),
    ("Cake Stand", "Home & Garden > Serveware", "Cake Stands", "Cake Servers"),
    ("Cake Server", "Home & Garden > Flatware", "Cake Servers", "Cake Stands"),
    ("Sugar Bowl", "Pottery & Glass > Serveware", "Sugar Bowls", "Sugar Tongs"),
    ("Salt Shaker", "Collectibles > Kitchenware", "Salt Shakers", "Salt Cellars"),
    ("Pepper Mill", "Home & Garden > Kitchen Tools", "Pepper Mills", "Pepper Shakers"),
    ("Bottle Opener", "Collectibles > Barware", "Bottle Openers", "Bottle Caps"),
    ("Bottle Stopper", "Collectibles > Barware", "Bottle Stoppers", "Bottle Openers"),
    ("Fountain Pen", "Collectibles > Writing Instruments", "Fountain Pens", "Pen Cases"),
    ("Dip Pen", "Collectibles > Writing Instruments", "Dip Pens", "Dip Pen Nibs"),
    ("Ink Bottle", "Collectibles > Writing Instruments", "Ink Bottles", "Ink Bottle Labels"),
    ("Letter Opener", "Collectibles > Desk Accessories", "Letter Openers", "Letter Holders"),
    ("Letter Holder", "Collectibles > Desk Accessories", "Letter Holders", "Letter Openers"),
    ("Drawing Compass", "Collectibles > Drafting Tools", "Drawing Compass", "Compass Cases"),
    ("Sewing Machine", "Collectibles > Sewing", "Sewing Machines", "Sewing Machine Parts"),
    ("Sewing Pattern", "Collectibles > Sewing", "Sewing Patterns", "Sewing Machines"),
    ("Embroidery Hoop", "Crafts > Needlecraft", "Embroidery Hoops", "Embroidery Kits"),
    ("Fabric Shears", "Crafts > Sewing", "Fabric Shears", "Shear Cases"),
    ("Measuring Tape", "Home & Garden > Tools", "Measuring Tapes", "Tape Holders"),
    ("Bench Vise", "Home & Garden > Tools", "Bench Vises", "Vise Jaws"),
    ("Record Player", "Consumer Electronics > Audio", "Record Players", "Player Needles"),
    ("Cassette Deck", "Consumer Electronics > Audio", "Cassette Decks", "Tape Cassettes"),
    ("Radio Receiver", "Consumer Electronics > Audio", "Radio Receivers", "Radio Antennas"),
    ("Violin Case", "Musical Instruments > Accessories", "Violin Cases", "Violins"),
)


def response(code, body=None):
    return Mock(status_code=code, json=Mock(return_value=body or {}),
                text=json.dumps(body or {}), headers={})


class CategoryTests(unittest.TestCase):
    def test_replacement_items_keep_their_category_and_listing_words(self):
        cases = (
            ("Sewing Machine Parts", "Crafts > Sewing"),
            ("Watch Repair Kit", "Jewelry & Watches > Watch Accessories"),
        )
        for name, parent in cases:
            with self.subTest(name=name):
                leaf = "Watch Repair Kits" if name == "Watch Repair Kit" else name
                draft = {
                    "title": name, "item_specifics": {"Type": name}, "seller_tested_status": "Untested",
                    "category_id": "1", "category_path_category_id": "1", "category_guard_version": "P057",
                    "category_path": f"{parent} > {leaf}",
                    "description": "Replacement parts are pictured. Missing parts, if any, are listed.",
                }
                self.assertTrue(m._p057_category_snapshot_check(draft)[0])
                self.assertEqual(m._untested_public_content_violations(draft), [])
                self.assertEqual(m.PARTS_CONDITION_CLAIM.sub("", name), name)

    def test_unverified_parts_only_sale_is_rejected_at_category_and_live_read(self):
        draft = {
            "title": "Pocket Watch", "item_specifics": {"Type": "Pocket Watch"},
            "seller_tested_status": "Untested",
            "category_id": "1", "category_path_category_id": "1", "category_guard_version": "P057",
            "category_path": "Watches > For Parts or Repair > Pocket Watches",
        }
        self.assertFalse(m._p057_category_snapshot_check(draft)[0])
        status_confirmed = dict(draft, seller_tested_status="Tested not working")
        self.assertTrue(m._p057_category_snapshot_check(status_confirmed)[0])
        for text in ("Sold for parts or repair.", "Needs repair.", "Parts only.", "Sold for restoration."):
            with self.subTest(text=text):
                self.assertEqual(m._p062_live_public_failures(draft, {"description": text}), ["Description"])
                self.assertEqual(m._p062_live_public_failures(status_confirmed, {"description": text}), [])

    def test_fifty_object_scenarios(self):
        self.assertGreaterEqual(len({row[0] for row in OBJECT_FIXTURES}), 50)
        for primary, hierarchy, correct, wrong in OBJECT_FIXTURES:
            with self.subTest(primary=primary):
                self.assertTrue(m._category_semantic_match(primary, correct, f"{hierarchy} > {correct}")[0])
                self.assertFalse(m._category_semantic_match(primary, wrong, f"{hierarchy} > {wrong}")[0])

    def test_reported_lot_failure_patterns(self):
        # Reconstructed from the reported symptoms. Original lot data is unavailable.
        for label, primary in (
            ("Synthetic collar tabs", "Military Collar Tabs"),
            ("LOT059", "Soviet Collar Tabs"),
            ("LOT060", "Historical Military Insignia"),
        ):
            draft = {
                "title": primary, "item_specifics": {"Type": primary},
                "category_id": "1", "category_path_category_id": "1",
                "category_guard_version": "P057",
            }
            with self.subTest(lot=label):
                for path in ("Pet Supplies > Dog Supplies > Dog Collars",
                             "Collectibles > Militaria > Modern > Insignia"):
                    self.assertFalse(m._p057_category_snapshot_check(dict(draft, category_path=path))[0])
                neutral = "Collectibles > Militaria > Insignia"
                self.assertTrue(m._p057_category_snapshot_check(dict(draft, category_path=neutral))[0])

    def test_complete_ancestor_chain(self):
        category = {"categoryName": "Pocket Watches"}
        def path(levels):
            return m._p057_category_path({
                "category": category, "categoryTreeNodeLevel": 3,
                "categoryTreeNodeAncestors": [
                    {"categoryName": str(level), "categoryTreeNodeLevel": level}
                    for level in levels
                ],
            })
        self.assertEqual(path([2, 1]), "1 > 2 > Pocket Watches")
        for invalid in ([2], [1, 3], [1, 1, 2]):
            self.assertEqual(path(invalid), "")

    def test_required_aspect_metadata_reused_only_in_request(self):
        aspects = [{"localizedAspectName": "Type", "aspectConstraint": {"aspectRequired": True}}]
        with patch.object(m, "_lot002_request", side_effect=[
            response(200, {"categoryTreeId": "0"}), response(200, {"aspects": aspects}),
            response(200, {"categoryTreeId": "0"}), response(200, {"aspects": aspects}),
        ]) as api:
            with m.app.test_request_context():
                self.assertEqual(m._current_aspect_metadata("token", "1"), aspects)
                self.assertEqual(m._p092_missing_required("token", "1", {"Type": ["Unknown"]}), ["Type"])
                self.assertEqual(m._p092_missing_required("token", "1", {"Type": ["Pocket Watch"]}), [])
                self.assertEqual(api.call_count, 2)
            with m.app.test_request_context():
                self.assertEqual(m._current_aspect_metadata("token", "1"), aspects)
                self.assertEqual(api.call_count, 4)

    def test_same_category_id_keeps_marketplace_metadata_separate(self):
        us = [{"localizedAspectName": "Color"}]
        de = [{"localizedAspectName": "Farbe"}]
        with m.app.test_request_context(), patch.object(m, "_lot002_request", side_effect=[
            response(200, {"categoryTreeId": "0", "categoryTreeVersion": "101"}),
            response(200, {"aspects": us}),
            response(200, {"categoryTreeId": "77", "categoryTreeVersion": "102"}),
            response(200, {"aspects": de}),
        ]) as api:
            self.assertEqual(m._current_aspect_metadata("token", "123"), us)
            self.assertEqual(m._current_aspect_metadata("token", "123", marketplace_id="EBAY_DE"), de)
            self.assertEqual(m._current_aspect_metadata("token", "123"), us)
            self.assertEqual(m._load_category_tree("token", "EBAY_DE").version, "102")
            self.assertEqual(api.call_count, 4)
            self.assertEqual(api.call_args_list[2].kwargs["params"], {"marketplace_id": "EBAY_DE"})
        with self.assertRaises(ValueError):
            m._load_category_tree("token", "EBAY_US/../DE")

    def test_product_directions_and_accessories_are_distinguished(self):
        cases = (
            ("Network Switch", "Computers > Networking", "Network Switches", "Network Switch Mounts"),
            ("Running Shoe", "Clothing > Athletic Footwear", "Running Shoes", "Running Shoe Laces"),
            ("Cabin Air Filter", "Motors > Car Parts", "Cabin Air Filters", "Cabin Air Filter Housings"),
            ("Screen Protector", "Phones > Accessories", "Screen Protectors", "Phone Screens"),
            ("Claw Hammer", "Tools > Hand Tools", "Claw Hammers", "Hammer Handles"),
            ("Термостійка рукавичка", "Дім > Кухня", "Термостійка рукавичка", "Кухонний рушник"),
            ("Kabellose Maus", "Computer > Eingabegeräte", "Kabellose Maus", "Maus Pads"),
        )
        for kind, hierarchy, right, wrong in cases:
            with self.subTest(kind=kind):
                self.assertTrue(m._category_semantic_match(kind, right, f"{hierarchy} > {right}")[0])
                self.assertFalse(m._category_semantic_match(kind, wrong, f"{hierarchy} > {wrong}")[0])
        previous = {
            "title": "Network switch", "category_path": "Computers > Networking > Network Switches",
            "category_id": "123", "category_path_category_id": "123", "category_guard_version": "P057",
        }
        self.assertTrue(m._p057_category_snapshot_check(previous)[0])
        self.assertFalse(m._p057_category_snapshot_check(dict(previous, category_marketplace_id="EBAY_DE"))[0])

    def test_same_leaf_needs_a_compatible_purpose_branch(self):
        for kind, leaf, right, wrong in (
            ('Brake Pad', 'Brake Pads', 'eBay Motors > Car Parts', 'Sporting Goods > Cycling'),
            ('Network Switch', 'Network Switches', 'Computers/Tablets & Networking > Networking',
             'Business & Industrial > Automation Controls'),
            ('Wall Clock', 'Wall Clocks', 'Home & Garden > Clocks', 'Watches > Accessories'),
            ('Running Shoe', 'Running Shoes', "Clothing, Shoes & Accessories > Women's Shoes",
             'Dolls & Bears > Doll Accessories'),
        ):
            with self.subTest(kind=kind):
                self.assertTrue(m._category_semantic_match(kind, leaf, f'{right} > {leaf}')[0])
                self.assertFalse(m._category_semantic_match(kind, leaf, f'{wrong} > {leaf}')[0])

    def test_saved_category_is_rechecked_against_current_tree(self):
        saved = {
            "title": "Network switch", "category_path": "Computers > Networking > Network Switches",
            "category_id": "123", "category_path_category_id": "123", "category_guard_version": "P057",
            "category_marketplace_id": "EBAY_US", "category_tree_id": "0", "category_tree_version": "101",
        }
        with m.app.test_request_context(), patch.object(m, "_lot002_request", side_effect=[
            response(200, {"categoryTreeId": "0", "categoryTreeVersion": "102"}),
        ]) as api:
            self.assertEqual(m._p057_category_snapshot_check(saved, token="token"),
                             (False, "The eBay category tree has changed. Run Preflight again."))
            self.assertTrue(m._p057_category_snapshot_check(dict(saved, category_tree_version="102"), token="token")[0])
            api.assert_called_once()

    def test_matching_leaf_in_two_branches_requires_distinguishing_facts(self):
        def suggestion(category_id, ancestor):
            return {
                "category": {"categoryId": category_id, "categoryName": "Power Adapters"},
                "categoryTreeNodeLevel": 2,
                "categoryTreeNodeAncestors": [{"categoryName": ancestor, "categoryTreeNodeLevel": 1}],
            }

        def lookup(method, url, **kwargs):
            if url.endswith("/get_default_category_tree_id"):
                return response(200, {"categoryTreeId": "0", "categoryTreeVersion": "101"})
            if url.endswith("/get_category_suggestions"):
                return response(200, {"categorySuggestions": [
                    suggestion("1", "Consumer Electronics"),
                    suggestion("2", "Computers and Tablets"),
                ]})
            field = "Audio Equipment Type" if kwargs["params"]["category_id"] == "1" else "Connector Type"
            return response(200, {"aspects": [{"localizedAspectName": field,
                "aspectConstraint": {"aspectRequired": True}}]})

        facts = {"Type": "Power Adapter", "Connector Type": "USB-C"}
        with m.app.test_request_context(), patch.object(m, "_lot002_request", side_effect=lookup):
            draft = {"item_specifics": facts}
            self.assertEqual(m._current_category("token", "Power Adapter", draft), ("2", "Power Adapters"))
            self.assertEqual(draft["category_path"], "Computers and Tablets > Power Adapters")
            disputed = {"item_specifics": dict(facts, **{"Audio Equipment Type": "Mixer"})}
            with self.assertRaisesRegex(m.CategoryAmbiguityError, "same category name in different eBay branches"):
                m._current_category("token", "Power Adapter", disputed)
            self.assertNotIn("category_path", disputed)
            stale = dict(draft, title="Power Adapter", price="10", shipping_usd="5", **disputed)
            with (
                patch.object(m, "_current_draft", return_value=stale),
                patch.object(m, "get_ebay_access_token", return_value="token"),
                patch.object(m, "_save_active_lot_checkpoint") as checkpoint,
            ):
                _, status = m.ebay_current_preflight()
                self.assertEqual(status, 409)
                self.assertFalse(m._p057_category_snapshot_check(stale)[0])
                checkpoint.assert_called_once()

    def test_taxonomy_values_and_dependency_follow_final_specifics(self):
        fields = [
            {"localizedAspectName": "Vehicle", "aspectConstraint": {
                "aspectMode": "SELECTION_ONLY", "itemToAspectCardinality": "SINGLE", "aspectRequired": True,
            }, "aspectValues": [{"localizedValue": "Sedan"}, {"localizedValue": "Wagon"}]},
            {"localizedAspectName": "Model", "aspectConstraint": {
                "aspectMode": "SELECTION_ONLY", "aspectRequired": True,
            }, "aspectValues": [{"localizedValue": "Series X", "valueConstraints": [{
                "applicableForLocalizedAspectName": "Vehicle", "applicableForLocalizedAspectValues": ["Wagon"],
            }]}]},
            {"localizedAspectName": "Material", "aspectConstraint": {
                "aspectMode": "SELECTION_ONLY", "itemToAspectCardinality": "SINGLE",
            }, "aspectValues": [{"localizedValue": "Aluminum"}]},
        ]
        with patch.object(m, "_current_aspect_metadata", return_value=fields):
            valid = {"vehicle": [" wagon "], "model": ["Series X", "Unknown model"],
                     "Material": ["Aluminum, Paper"], "Seller notes": ["Visible wear"]}
            self.assertEqual(m._sanitize_aspect_cardinality("token", "123", valid), {
                "Vehicle": ["Wagon"], "Model": ["Series X"], "Material": ["Aluminum"],
                "Seller notes": ["Visible wear"],
            })
            inconsistent = dict(valid, vehicle=["Sedan", "Wagon"])
            cleaned = m._sanitize_aspect_cardinality("token", "123", inconsistent)
            self.assertEqual(cleaned["Vehicle"], ["Sedan"])
            self.assertEqual(m._p092_missing_required("token", "123", cleaned), ["Model"])
            self.assertEqual(m._p092_missing_required("token", "123", dict(valid, vehicle=["Convertible"])),
                             ["Vehicle", "Model"])

    def test_new_requires_seller_declaration_and_live_market_condition_policy(self):
        def metadata(*ids):
            return response(200, {"itemConditionPolicies": [{"categoryId": "123", "itemConditionRequired": True,
                "itemConditions": [{"conditionId": code} for code in ids]}]})

        with patch.object(m, "_lot002_request", return_value=metadata("1000", "3000")) as api:
            self.assertEqual(m._current_condition("token", "123", {"title": "New cable"}), ("3000", "USED_EXCELLENT"))
            declared = {"seller_declared_condition": "NEW", "condition_note": "No visible scratches. In original package."}
            self.assertEqual(m._current_condition("token", "123", declared, marketplace_id="EBAY_GB"),
                             ("1000", "NEW"))
            self.assertIn("/EBAY_GB/", api.call_args.args[1])
            for note in ("Visible scratches on connectors", "Open-box return"):
                with self.subTest(note=note), self.assertRaisesRegex(RuntimeError, "conflicts"):
                    m._current_condition("token", "123", dict(declared, condition_note=note))
            api.return_value = metadata("3000")
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                m._current_condition("token", "123", declared)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(m.requests.sessions.Session, "request", side_effect=AssertionError("External network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_local_compliance_status(self):
        for draft, expected in (
            ({}, "REVIEW"),
            ({"title": "Pocket watch"}, "CLEAR"),
            ({"title": "Metal tabs", "item_specifics": {"Type": "Military Insignia"}}, "REVIEW"),
            ({"title": "Items", "description": "live&nbsp;<b>ammunition</b>"}, "BLOCK"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(m._p083_compliance(draft)["status"], expected)

    def test_cached_ai_response_available_with_paid_ai_disabled(self):
        payload = {"model": "fixture", "input": "Cached result"}
        signature = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        with m.app.test_request_context(), patch.dict(m.AI_RESPONSE_CACHE, {
            "cached-test": {signature: {"status": "COMPLETE", "text": "Cached result"}},
        }), patch.dict(m.os.environ, {"ALLOW_PAID_AI": "0"}):
            self.assertEqual(m.call_openai(payload, usage_picker_id="cached-test"), "Cached result")
            with self.assertRaisesRegex(RuntimeError, "AI"):
                m.call_openai(dict(payload, input="Cache miss"), usage_picker_id="cached-test")

    def test_ai_metrics_count_transport_errors_but_skip_cached_and_blocked_requests(self):
        picker = 'usage-fixture'
        success = {'model': 'gpt-5.6-luna', 'input': 'Success'}
        failure = {'model': 'gpt-5.6-luna', 'input': 'Timeout'}
        content = {'model': 'gpt-5.6-luna', 'output_text': 'OK',
                   'usage': {'input_tokens': 40, 'output_tokens': 10}}
        with (
            patch.dict(m.AI_USAGE_CACHE, {}),
            patch.dict(m.AI_RESPONSE_CACHE, {}),
            patch.dict(m.os.environ, {'ALLOW_PAID_AI': '1'}),
            patch.object(m, 'OPENAI_API_KEY', 'stub'),
            patch.object(m.time, 'monotonic', side_effect=[1, 1.25, 2, 2.75]),
            patch.object(m.requests, 'post', side_effect=[
                response(200, content), m.requests.exceptions.Timeout(),
            ]) as provider,
        ):
            self.assertEqual(m.call_openai(success, usage_picker_id=picker), 'OK')
            self.assertEqual(m.call_openai(success, usage_picker_id=picker), 'OK')
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'uncertain|UNCERTAIN'):
                    m.call_openai(failure, usage_picker_id=picker)
            self.assertEqual(provider.call_count, 2)
            usage = m.AI_USAGE_CACHE[picker]
            self.assertEqual(usage['models'], {'gpt-5.6-luna': 2})
            self.assertEqual(usage['request_duration_ms'], 1000)
            self.assertEqual((usage['attempts'], usage['calls'], usage['cache_hits'], usage['failures']),
                             (2, 1, 1, 1))
            self.assertEqual((usage['input_tokens'], usage['output_tokens']), (40, 10))

    def test_eps_retry_preserves_all_unchanged_approved_photos(self):
        # LOT058 failure pattern, reconstructed with synthetic approved bytes.
        picker = "eps-24-test"
        sku = "LOT-058"
        items = [{"mediaFile": {"filename": f"{i}.jpg"}} for i in range(1, 26)]
        approved = {i: {"bytes": f"approved-jpeg-{i}".encode()} for i in range(1, 25)}
        def upload(url, **kwargs):
            name, raw, mime = kwargs["files"]["image"]
            self.assertEqual(mime, "image/jpeg")
            self.assertEqual(raw, approved[int(name[:-4])]["bytes"])
            return response(201, {"imageUrl": f"https://images.example/{name}"})

        with (
            m.app.test_request_context(),
            patch.dict(m.PHOTO_CACHE, {picker: items}),
            patch.dict(m.APPROVED_PHOTO_CACHE, {picker: approved}),
            patch.dict(m.PHOTO_ROLE_CACHE, {picker: {**dict.fromkeys(range(1, 25), "EBAY_PRODUCT"), 25: "EVIDENCE_ONLY"}}),
            patch.dict(m.EBAY_MEDIA_CACHE, {sku: []}),
            patch.object(m, "_current_lot_sku", return_value=sku),
            patch.object(m, "_save_active_lot_checkpoint", return_value=True),
            patch.object(m.requests, "post", side_effect=upload) as post,
        ):
            m.session.update(picker_session_id=picker, photo_editor_approved=True, main_photo_index=24)
            rows = m._current_upload_photos("mock")
            self.assertEqual([row["source_number"] for row in rows], [24, *range(1, 24)])
            self.assertEqual(post.call_count, 24)
            m.session["main_photo_index"] = 12
            approved[12]["bytes"] += b"-new-edit"
            post.side_effect = None
            post.return_value = response(400)
            with self.assertRaisesRegex(RuntimeError, "source photo 12"):
                m._current_upload_photos("mock")
            self.assertEqual(len(m.EBAY_MEDIA_CACHE[sku]), 23)
            post.side_effect = upload
            rows = m._current_upload_photos("mock")
            self.assertEqual(post.call_count, 26)
            self.assertEqual(rows[0]["source_number"], 12)
            self.assertEqual(m._p093_verified_eps(sku), [row["image_url"] for row in rows])

    def test_invalid_shipping_numbers_require_review(self):
        for values in ((-1, 10, 10, 10), (0, 10, 10, 10), ("NaN", 10, 10, 10),
                       (0.25, 10, 10, -1), (0.25, 10, 10, "inf"), (2.01, 10, 10, 10)):
            with self.subTest(values=values):
                self.assertEqual(m._ukrposhta_prime_us_shipping(*values)["service"], "MANUAL_REVIEW")
        self.assertEqual(m._ukrposhta_prime_us_shipping(0.25, 10, 10, 10)["recommended_usd"], 10.99)

    def test_prime_quote_exact_limits_and_customs_scope(self):
        quote = m._ukrposhta_prime_us_shipping('2', '60', '29.9', '0.1')
        self.assertEqual((quote['base_usd'], quote['maximum_band_g']), (26, 2000))
        self.assertFalse(quote['includes_duties'])
        self.assertEqual(quote['vat_on_domestic_portion_uah'], 14)
        self.assertEqual(m._ukrposhta_prime_us_shipping('0.250001', 10, 10, 10)['base_usd'], 12)
        self.assertIsNone(m._ukrposhta_prime_us_shipping('2.000001', 60, 30, 0.1)['base_usd'])
        self.assertIsNone(m._ukrposhta_prime_us_shipping('2', 60, 30, 0.1)['base_usd'])
        compliance = m._p083_compliance({'title': 'Old textile item', 'primary_type': 'collar tab'})
        self.assertEqual(compliance['status'], 'REVIEW')
        self.assertEqual(compliance['scope'], 'LOCAL_SCREENING')
        self.assertEqual(compliance['customs_code_status'], 'UNVERIFIED')

    def test_estimated_package_rounds_up_before_the_postage_band(self):
        _, dimensions, weight, rate = m._shipping_quote(0.131, 5.01, 4.0, 3.0)
        self.assertEqual((weight, dimensions), (0.26, (9.1, 8.0, 7.0)))
        self.assertEqual(rate['maximum_band_g'], 500)
        self.assertEqual(rate['base_usd'], 12)

    def test_live_reference_weight_detected(self):
        draft = {"measurement_source": {"reference_weight_shipping_only": True}}
        product = {"description": "The watch weighs 200 g.", "aspects": {"Item Weight": ["200 g"]}}
        failures = m._p062_live_public_failures(draft, product)
        self.assertIn("Description: unverified weight in public text", failures)
        self.assertIn("Item Weight: unverified weight", failures)

    def test_new_price_evidence_must_match_web_search_metadata(self):
        url = "https://market.example/item/1"
        result = {"output": [
            None,
            {"type": "web_search_call", "action": {"sources": [{"url": url}, {"url": ["invalid"]}]}},
            {"type": "message", "content": [None, {"annotations": [
                None,
                {"type": "url_citation", "url": url},
                {"type": "url_citation", "url": "file:///private"},
            ]}]},
        ]}
        sources = m._web_search_source_urls(result)
        self.assertEqual(sources, [url])
        price = 'PRICE_JSON {"normal":25}'
        footer = '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({"urls": sources})
        comp = {"url": url, "status": "ACTIVE", "price_usd": 23,
                "match_evidence": "Same visible case and mechanism"}
        def estimate(comparable):
            return m._market_bin_price(price + '\nMARKET_COMPS_JSON '
                                       + json.dumps({"comparables": [comparable]}) + footer)
        with patch.dict(m.os.environ, {"EBAY_SALES_MODE": "BALANCED"}):
            self.assertEqual(estimate(comp), "25")
            self.assertEqual(estimate(dict(comp, url="https://market.example/item/2")), "")
            self.assertEqual(estimate(dict(comp, status="SOLD")), "")
            self.assertEqual(estimate(dict(comp, price_usd=-1)), "")
            self.assertEqual(m._market_bin_price(price), "25")

    def test_sold_comparables_require_a_date_and_conflicts_do_not_count(self):
        url = 'https://market.example/item/1'
        comp = {'url': url, 'status': 'SOLD', 'price_usd': 23, 'currency': 'USD',
                'match_evidence': 'Same stamped model number', 'sale_evidence': 'Completed sale'}
        def comparables(*rows):
            report = ('PRICE_JSON {"normal":25}\nMARKET_COMPS_JSON ' + json.dumps({'comparables': rows})
                      + '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': [url]}))
            return m._market_price_quality(report)['comparables']

        dated = dict(comp, sold_date='2025-05-01')
        self.assertEqual(comparables(dated, dated), {'ACTIVE': 0, 'SOLD': 1})
        for unusable in (comp, dict(dated, sold_date='9999-01-01'), dict(dated, currency='PLN')):
            self.assertEqual(comparables(unusable), {'ACTIVE': 0, 'SOLD': 0})
        self.assertEqual(comparables(dated, dict(dated, status='ACTIVE')), {'ACTIVE': 0, 'SOLD': 0})

    def test_market_price_checks_anchor_and_deduplicates_ebay_item_links(self):
        product = 'https://www.ebay.com/itm/Replacement-part/123456789012?utm_source=test'
        mirror = 'https://www.ebay.de/itm/123456789012?mkcid=23'
        row = {'url': product, 'status': 'SOLD', 'currency': 'USD', 'price_usd': 25,
               'match_evidence': 'Same model and package', 'sale_evidence': 'Completed',
               'sold_date': '2026-09-10'}

        def quality(proposed, other):
            return m._market_price_quality(
                'PRICE_JSON ' + json.dumps({'normal': proposed}) + '\nMARKET_COMPS_JSON '
                + json.dumps({'comparables': [row, other]}) + '\nWEB_TOOL_SOURCES_JSON '
                + json.dumps({'urls': [product, mirror]}),
            )

        duplicate = dict(row, url=mirror)
        normal = quality(49, duplicate)
        self.assertTrue(normal['ok'])
        self.assertEqual(normal['comparables'], {'ACTIVE': 0, 'SOLD': 1})
        self.assertEqual(normal['anchor'], {'type': 'SOLD', 'median_usd': 25.0})
        inflated = quality(100, duplicate)
        self.assertFalse(inflated['ok'])
        self.assertIn('Normal price is far outside the observed comparable prices', inflated['reasons'])
        self.assertEqual(quality(49, dict(duplicate, price_usd=99))['comparables']['SOLD'], 0)

    def test_market_break_even_accounts_for_shipping_fees_and_currency(self):
        values = {'fx_uah_per_usd': 40, 'cost_uah': 125, 'us_price_usd': 25,
                  'us_buyer_shipping_usd': 5, 'us_fee_pct': 10, 'us_fixed_fee_usd': 0.3,
                  'us_postage_uah': 140, 'us_packing_uah': 20, 'us_other_uah': 15,
                  'ua_price_uah': 450, 'ua_buyer_shipping_uah': 40, 'ua_fee_pct': 5,
                  'ua_fixed_fee_uah': 5, 'ua_postage_uah': 60, 'ua_packing_uah': 20, 'ua_other_uah': 10}
        report = m.compare_ua_us_net_profit(values)
        self.assertEqual(report['us_break_even_usd'], '3.67')
        self.assertEqual(report['ua_break_even_uah'], '191.58')
        self.assertIsNone(m.compare_ua_us_net_profit(dict(values, us_fee_pct=100))['us_break_even_usd'])

    def test_hold_restores_draft_and_photo_without_ai(self):
        picker = "hold-test"
        draft = {"title": "Pocket watch with inspected exterior", "price": "25"}
        photos = [{"mediaFile": {"filename": "pocket-watch.jpg"}}]
        approved = {1: {"bytes": b"original approved bytes"}}
        with (
            patch.dict(m.PHOTO_CACHE, {picker: photos}),
            patch.dict(m.LOT002_DRAFT_CACHE, {picker: draft}),
            patch.dict(m.APPROVED_PHOTO_CACHE, {picker: approved}),
            patch.object(m, "_p058_preview", return_value=("", "")),
            patch.object(m, "call_openai", side_effect=AssertionError("AI forbidden")),
        ):
            with m.app.test_request_context():
                m.session.update(picker_session_id=picker, current_lot_number=501,
                                 main_photo_index=1, photo_order=[1], photo_editor_approved=True)
                rid = m._p058_capture_current()
                self.assertEqual(m._p058_capture_current(), rid)
                earlier = json.loads(m._p058_row(rid)["data"])["snapshot"]
                draft["price"] = "29"
                self.assertTrue(m._save_active_lot_checkpoint())
                m._p058_refresh_registered_hold(earlier)
            for cache in (m.PHOTO_CACHE, m.LOT002_DRAFT_CACHE, m.APPROVED_PHOTO_CACHE):
                cache.pop(picker)
            with m.app.test_request_context():
                self.assertEqual(m._p058_open_record(rid), "/picker/review-final-draft")
                self.assertEqual(m.LOT002_DRAFT_CACHE[picker]["price"], "29")
                self.assertEqual(m.session["photo_order"], [1])
                self.assertEqual(m.APPROVED_PHOTO_CACHE[picker][1]["bytes"], approved[1]["bytes"])

    def test_final_gate_checks_actual_aspects_and_last_read(self):
        sku, picker = "LOT-502", "final-test"
        image = "https://images.example/photo.jpg"
        draft = {
            "title": "Pocket watch with inspected exterior and metal case",
            "description": "A pocket watch for a collection. The case has visible scratches and wear around the rim. The photographs show this item from several angles. The sale includes one watch.",
            "item_specifics": {"Type": "Pocket Watch"},
            "seller_reviewed": True, "price_confirmed": True, "seller_tested_status": "Untested",
            "price": "25", "shipping_usd": "10", "category_id": "1", "category_path_category_id": "1",
            "category_guard_version": "P057", "category_name": "Pocket Watches",
            "category_path": "Jewelry & Watches > Watches > Pocket Watches",
        }
        offer = {
            "offerId": "mock-offer", "sku": sku, "status": "UNPUBLISHED", "marketplaceId": "EBAY_US",
            "categoryId": "1", "listingDuration": "GTC", "availableQuantity": 1,
            "pricingSummary": {"price": {"value": "25.00", "currency": "USD"}},
            "listingPolicies": {"fulfillmentPolicyId": "mock-policy"},
        }
        policy = {
            "handlingTime": {"value": 3},
            "shippingOptions": [{"shippingServices": [{
                "shippingServiceCode": "StandardShippingFromOutsideUS", "shippingCost": {"value": "10"},
            }]}],
        }
        client = m.app.test_client()
        with (
            patch.dict(m.os.environ, {"ENABLE_LIVE_PUBLISH": "1"}),
            patch.dict(m.PHOTO_CACHE, {picker: [{}]}),
            patch.object(m, "_current_draft", return_value=draft),
            patch.object(m, "_p088_sales_block", return_value=""),
            patch.object(m, "_p093_verified_eps", return_value=[image]),
            patch.object(m, "_current_get_offers", return_value=[offer]),
            patch.object(m, "get_ebay_access_token", return_value="mock"),
            patch.object(m, "_current_aspect_metadata", return_value=[
                {"localizedAspectName": "Type", "aspectConstraint": {"aspectRequired": True}},
                {"localizedAspectName": "Color", "aspectConstraint": {"aspectMode": "SELECTION_ONLY"},
                 "aspectValues": [{"localizedValue": "Silver"}]},
            ]),
            patch.object(m, "_p058_capture_current"),
            patch.object(m, "_lot002_request") as read,
            patch.object(m.requests, "post", return_value=response(400)) as publish,
        ):
            with client.session_transaction() as active:
                active.update(picker_session_id=picker, current_lot_number=502, photo_order=[1], hold_csrf="csrf")
            with m.app.test_request_context():
                m.session.update(picker_session_id=picker, photo_order=[1])
                form = {"hold_csrf": "csrf", "review_digest": m._final_publish_digest(draft, sku)}
            self.assertEqual(client.post("/ebay/current-publish", data=dict(form, review_digest="stale")).status_code, 409)
            read.assert_not_called()
            product = {"title": draft["title"], "description": draft["description"], "imageUrls": [image]}
            read.side_effect = [response(200, {"product": product}), response(200, policy)]
            result = client.post("/ebay/current-publish", data=form)
            self.assertEqual(result.status_code, 409)
            self.assertIn(b"Required aspects", result.data)
            publish.assert_not_called()
            product["aspects"] = {"Type": ["Pocket Watch"], "Color": ["Mismatch"]}
            read.side_effect = [response(200, {"product": product}), response(200, policy)]
            result = client.post("/ebay/current-publish", data=form)
            self.assertEqual(result.status_code, 409)
            self.assertIn(b"Aspect values and dependencies", result.data)
            publish.assert_not_called()
            product["aspects"] = {"Type": ["Pocket Watch"]}
            read.side_effect = [response(200, {"product": product}), response(200, policy),
                                response(200, dict(offer, categoryId="2"))]
            result = client.post("/ebay/current-publish", data=form)
            self.assertEqual(result.status_code, 409)
            self.assertIn(b"Final re-check failed", result.data)
            publish.assert_not_called()
            read.side_effect = [response(200, {"product": product}), response(200, policy), response(200, offer)]
            self.assertEqual(client.post("/ebay/current-publish", data=form).status_code, 502)
            publish.assert_called_once()
            self.assertTrue(publish.call_args.args[0].endswith("/mock-offer/publish"))

    def test_mobile_24_photos_can_be_edited_without_ai(self):
        from io import BytesIO
        from PIL import Image

        photo = BytesIO()
        Image.new("RGB", (80, 60), "red").save(photo, "JPEG")
        client = m.app.test_client()
        with patch.object(m, "_sync_current_lot_once", return_value=503):
            client.get("/mobile")
            with client.session_transaction() as active:
                csrf = active["hold_csrf"]
                active["current_lot_number"] = 503
            uploaded = client.post("/mobile/upload", data={
                "hold_csrf": csrf,
                "photos": [(BytesIO(photo.getvalue()), f"{i}.jpg") for i in range(1, 25)],
            })
            self.assertEqual(uploaded.status_code, 303)
            roles_page = client.get("/picker/editor").get_data(as_text=True)
            self.assertIn('name="product_photo"', roles_page)
            selection = re.search(r'name="selection" value="([^"]+)"', roles_page)[1]
            self.assertEqual(client.post("/picker/editor", data={"product_photo": "1"}).status_code, 403)
            selected = client.post("/picker/editor", data={
                "hold_csrf": csrf, "selection": selection, "product_photo": [str(i) for i in range(1, 25)],
            })
            self.assertEqual(selected.status_code, 303)
            self.assertEqual(client.get("/picker/editor").status_code, 200)
            approved = client.post("/picker/editor/approve", data={"main_photo": 24, "b_24": 1.1})
            self.assertEqual(approved.status_code, 302)
            with client.session_transaction() as active:
                self.assertEqual(active["photo_order"], [24, *range(1, 24)])
                self.assertTrue(active["photo_editor_approved"])
            self.assertEqual(client.get("/picker/approved/24").status_code, 200)
