# Vintage UA eBay Machine — специфікація v5

> Вимоги та приймання визначають [статут 1.0](Vintage_UA_MASTER_TASK.md) і [комерційне доповнення](MASTER_COMMERCIAL_SPEC.md). Історичний сценарій LOT004 із 19 фото збережено нижче. Для приймання E11 застосовується сценарій Android із 12 реальних фото.

## Мета

Універсальний багатокористувацький SaaS для автоматизації продажів нових і вживаних товарів у дозволених категоріях eBay. Перший ринок SaaS — Україна, перша мобільна платформа — Android. Власник приймає персональний потік на вінтажі й антикваріаті. Далі перевіряємо різні товарні напрями, міжнародні ринки та iOS. Чинний персональний маршрут працює з eBay US і доставкою з України. Перевірки для інших ринків додаються до підключення відповідних магазинів.

## Комерційна архітектура

### Товар, докази й маркетплейс

- Ядро зіставляє тип і призначення товару з повним шляхом до листової категорії, фото та джерелами. Характеристики, допустимі значення й залежності, стани, обов’язкові поля та обмеження беруться з актуальних метаданих eBay. Якщо різні гілки з однаковою назвою листка проходять перевірку, чернетка очікує доказу призначення. Версійовані правила товарних напрямів додають перевірки, наприклад для сумісності запчастин, розміру взуття, маркування електроніки чи датування антикваріату. Додавання правил не вимагає зміни сховища лотів і потоку публікації.
- Поле товару зберігає значення, одиницю, походження, посилання на доказ, спосіб підтвердження та версію. Вага товару відділена від ваги посилки, спостереження від припущення, одиночний товар від набору, справність від косметичного стану. Матеріал, період, сумісність, стан, ціна, SEO і доставка будуються з доступних доказів. Суперечність зупиняє допуск або запускає перевірене автоматичне виправлення. Коректні значення заповнюються автоматично, редагування доступне як виправлення.
- Ідентифікатор магазину, маркетплейс eBay, мова оголошення, мова інтерфейсу, країни відправлення й призначення, валюта лота та територія надання SaaS є окремими параметрами. Чернетка фіксує маркетплейс, дерево, його версію та шлях категорії. Перед увімкненням запису для кожного нового маркетплейсу звіряються поля, стан і обмеження. Метадані з облікових записів, приватні докази, результати AI та ціни кешуються в межах відповідної області доступу.
- Ціна використовує підтверджені зіставні продажі, валюту, курс і дату. Розрахунок доставки враховує напрямок, посилку, тарифи й дату. Compliance зберігає версію правила, його сферу, джерело та причину CLEAR, REVIEW чи BLOCK. Коди митної класифікації й регуляторні твердження фіксуються лише з підтвердженням. SEO й описи використовують доказові значення та обмеження вибраного маркетплейсу.

### Акаунти, магазини й дані

- Межа доступу для лота — `tenant_id`, `store_id`, `marketplace_id`, `lot_id` і ревізія. Сервер визначає акаунт і дозволені магазини з авторизованої сесії та ролі. У спільному PostgreSQL складені ключі й обмеження не дають прив’язувати об’єкти різних клієнтів. Політики доступу БД, файлові ключі й облік завдань застосовують ту саму межу. Короткоживучі підписані посилання видаються після перевірки права доступу.
- Підключення OAuth належить конкретному магазину й користувацькій ролі. Callback перевіряє одноразовий state і власника операції. PKCE застосовується за підтримки провайдера. Токени зберігаються зашифрованими окремо від БД лотів, ключі мають контроль доступу і ротацію. Відкликання зупиняє нові завдання магазину. Сторонній доступ, зміни ролей, згода, підключення, видалення, витрати й адміністративні дії потрапляють до журналу без секретів. Доступ служби підтримки обмежений роллю, строком і зафіксованою причиною.
- Кабінет надає керування дозволами й згодами, експорт, відкликання підключень і видалення акаунта. Процес видалення охоплює об’єктне сховище, кеші, завдання, процесорів і відновлені копії з урахуванням обґрунтованих строків зберігання. Призначення обробки та передачі даних документуються. Вторинне використання даних клієнтів потребує окремої належної правової підстави. Для інцидентів визначаються відповідальні, сповіщення й перевірка відновлення з резервної копії.
- AI, підготовка зображень і зовнішні інтеграції використовують надійну чергу з ідемпотентними завданнями та повтором після збою. Намір зовнішньої дії зберігається разом зі зміною лота. Перед платним завданням атомарно резервується ліміт клієнта, після нього звіряються фактичні витрати. Повтор зовнішнього запису можливий після звірки результату. Клієнт бачить власне використання AI та залишок ліміту. Ізоляція й відновлення перевіряються на кількох користувачах і процесах до масштабування.

### Тарифи, платежі й підтримка

- Ядро веде план, пробний період, місячний і річний строк, ліміти та доступ до функцій. Канали Web, Play і майбутній App Store підключають окремі адаптери за правилами країни. Підписка SaaS і продаж фізичного товару через eBay мають незалежні ідентифікатори, транзакції та документи.
- Доступ змінюється після перевіреної серверної події або звірки з провайдером. Платіжний журнал зберігає зовнішній ідентифікатор, суми в мінімальних одиницях валюти, статус, комісії, документи й повернення. Повторні або переставлені події не подвоюють списання і доступ. PAN та CVV не надходять до застосунку. Кабінет дає змогу змінити або скасувати підписку, переглянути історію, відкрити документ і звернутися за підтримкою чи поверненням.
- Адміністративна панель розмежовує підтримку, фінанси й безпеку. Аналітика вимірює собівартість і прибутковість за клієнтом, тарифом і каналом, зокрема AI, інфраструктуру, комісії, повернення й податки. Правила доступу й ціни версійовані. Юридичні тексти, модель оплати, відновлення та комерційний доступ приймаються за окремими критеріями. [Юридична матриця](LEGAL_COMPLIANCE.md) · [Готовність SaaS](COMMERCIAL_READINESS.md) · [Випуск](RELEASE_CRITERIA.md)

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
