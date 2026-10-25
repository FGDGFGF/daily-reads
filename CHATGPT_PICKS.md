# ChatGPT Picks integration

The feed displays `[Gemini]` and `[ChatGPT]` title prefixes and adds `Gemini Picks` / `ChatGPT Picks` RSS categories. Existing historical entries are identified as Gemini.

To add a ChatGPT batch, replace `state/chatgpt_picks.json` with a JSON array of **12 genuinely ChatGPT-curated articles**. Each object requires `batch_date` (Mauritius date, `YYYY-MM-DD`), `title`, `url` (direct original article), `publisher`, `date` if known, `hook`, `category`, `long` (boolean), and `theme` for the rotating article. Allowed categories: `weird_history`, `fraud_investigation`, `science_tech`, `culture_ideas`, `islamic`, `wildcard`, `rotating`. Exactly 2 must be `long`, exactly one `islamic`, and exactly one `rotating`.

The script checks each direct link and rejects the entire ChatGPT batch on failed verification, duplicate URLs or similar headlines against Gemini picks or history. It will not fabricate ChatGPT selections. The inbox **must be populated before the daily workflow runs**; there is currently no automatic ChatGPT-to-GitHub delivery or API integration. If missing or invalid, only Gemini articles are published. The previous Gemini curation remains intact.
