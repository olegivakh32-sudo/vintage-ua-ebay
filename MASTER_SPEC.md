# Vintage UA eBay — Master Specification v4

## Goal
A fast seller workflow for publishing vintage/collectible lots on eBay.com from Ukraine, designed to scale toward high daily volume without repeatedly re-entering information.

## Core workflow
1. Determine the next LOT/SKU automatically.
2. Connect to Google Photos Picker.
3. Select 1–24 photos of one item.
4. Preserve selection identity; clear stale state only when the actual selected photos change.
5. Show safe edited previews, choose MAIN PHOTO, approve photos.
6. Fast visual AI pass on a representative subset of photos.
7. Web-assisted identification on a smaller representative subset.
8. eBay market research with strict separation of confirmed sold evidence and active asking prices.
9. Automatic size/weight estimate from all photos; use visible ruler/scale references when available.
10. Auto-prefill material/status/completeness; seller only corrects what is wrong.
11. Generate a structured final eBay draft as JSON, not brittle free-text parsing.
12. Seller can correct final price/material immediately before Preflight.
13. eBay Preflight checks SKU, category, condition, shipping, Best Offer support, photo count/order/main photo and duplicate Offers.
14. Create Inventory Item + one unpublished Offer.
15. Re-read authoritative eBay state and verify every critical field.
16. Explicit final Publish button.
17. Show live listing link and START NEXT LOT.

## Non-negotiable rules learned from LOT 001–004
- LOT 001 must never be modified by the generic current-lot pipeline.
- Do not reuse/update LOT 001 shipping policy for new lots.
- Photos: minimum 1, maximum 24; never require exactly 24 for new lots.
- Selected MAIN PHOTO must be first on eBay.
- Photo order must be deterministic and verified after eBay upload.
- Edited photos, not original previews, are used for eBay upload.
- Do not invent manufacturer, model, country, production year, sold status, sold price or material.
- A USSR quality mark is not automatically a manufacturer mark.
- Seller-confirmed facts override AI/web guesses.
- Known data should be prefilled automatically; manual entry is correction/fallback, not the default workflow.
- If a material is unknown, omit it rather than block the entire listing.
- If price extraction fails, allow seller correction before Preflight rather than dead-ending.
- Fixed-price Offers use GTC.
- Use category-specific eBay condition policy; non-working/parts items should map conservatively.
- Enable Best Offer only when eBay metadata says the category supports it.
- Retry safe transient OpenAI/eBay reads; never create duplicate Offers after uncertain POST results.
- Safety stops remain before consequential eBay writes/publication.

## Performance decisions
- Step 1 visual analysis uses up to 10 representative images at reduced resolution.
- Step 2 web identification uses up to 7 representative images.
- Size/weight estimation remains separate and can inspect all photos because ruler/measurement photos may be anywhere in the set.
- AI outputs are shorter and cached in Render memory for the active Picker session, avoiding repeated expensive calls when revisiting pages.
- Full AI reports are placed in collapsible sections to keep the seller UI compact.

## Regression prevention
- Base: app_stable_v3_2.py, because its photo editor and Google Photos flow had already worked in testing.
- The SAFE PHOTO EDITOR block is kept unchanged from that stable base.
- New-lot/selection changes clear stale main-photo/order/AI state.
- No duplicate Flask routes.
- Python syntax compilation must pass before deployment.
- Current generic workflow no longer contains hard-coded "Study ALL 24 photographs" or "original 24 photographs" prompts.

## Deployment assumptions
Environment variables already used by the working site:
- APP_SECRET
- GOOGLE_CLIENT_ID
- GOOGLE_CLIENT_SECRET
- OPENAI_API_KEY
- EBAY_REFRESH_TOKEN
- EBAY_CLIENT_ID
- EBAY_CLIENT_SECRET
- EBAY_RUNAME
- optional GOOGLE_REDIRECT_URI (defaults to the current Render callback URL)

Python dependencies used by the app:
- Flask
- requests
- Pillow
- gunicorn (for Render production serving)

## Acceptance test for LOT 004
- Home shows LOT 004.
- 19 photos accepted.
- Edited previews render correctly.
- Selected MAIN PHOTO is preserved.
- Step 1 completes faster than prior all-photo pass.
- Step 2 completes faster and does not mention "24 photographs".
- Size/weight fields auto-populate from ruler/reference photos when possible.
- Material is prefilled automatically and remains editable.
- Market research outputs a numeric Normal Buy It Now recommendation whenever enough evidence exists.
- Final draft has a usable title and price without fragile section parsing.
- Preflight shows LOT-004 and correct SKU/category/condition/shipping/photo order.
- eBay final verification passes before Publish.
- Live listing has all photos, correct first photo, Buy It Now, correct shipping, and Best Offer only if supported.
- Final page shows START LOT 005.
