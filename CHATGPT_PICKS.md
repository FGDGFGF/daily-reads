# ChatGPT Picks integration

The feed carries two curators, clearly labelled:

- `[Gemini] …` — category **Gemini Picks** (picked by `curate.py` from the publications in `sources.txt`)
- `[ChatGPT] …` — category **ChatGPT Picks** (selected by ChatGPT and committed to `state/chatgpt_picks.json`)
- `[Backup] …` — category **Backup Picks**, only on days Gemini is unavailable (no AI involved)

## How a ChatGPT batch gets in

1. ChatGPT commits a batch to `state/chatgpt_picks.json` (a JSON array; you can keep older batches in it).
   Each object: `batch_date` (Mauritius date `YYYY-MM-DD`), `title`, `url` (direct article link), `publisher`,
   `date` (if known), `hook`, `category` (`weird_history`, `fraud_investigation`, `science_tech`,
   `culture_ideas`, `islamic`, `wildcard`, `rotating`), `long` (true/false), and `theme` for the rotating article.
2. **That push starts the workflow immediately** (ChatGPT-only mode — no extra Gemini batch).
   The 05:40 daily run and the 📰 button also import any batch that's waiting.
3. Each batch date is handled **once** and recorded in `state/chatgpt_processed.json`, so repeated runs,
   pushes or button presses never republish it. Batches older than 2 days are ignored.

## Checks

- **Per batch:** exactly 12 articles, exactly 2 long reads, exactly 1 Islamic and 1 rotating article with one of the
  six themes. A malformed batch is reported in `state/last-run.log` and *not* marked as handled, so a corrected
  version can be pushed for the same date.
- **Per article:** direct link opens the actual article (not a homepage), the page title matches the headline, not
  already in the feed (same link or near-identical headline), not from a blocked publisher (STAT).
  Sites that block automated checks are accepted only if they're a known publication from `sources.txt` *and* the
  link itself names the story. One bad article no longer sinks the other eleven; each rejection is logged with its reason.
- ChatGPT's batch is imported **before** Gemini picks, and Gemini is told to avoid those articles, so the two curators
  never overlap and Gemini can't displace a ChatGPT pick.

## Not automated by this repository

Whether ChatGPT's scheduled task (04:50) can commit to GitHub on its own is outside this repo. Check
`state/last-run.log` after a morning run: "no batch for today" means nothing arrived from ChatGPT.
