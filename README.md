# Daily Reads

Every morning (ready by 06:00 Mauritius time) GitHub runs `curate.py`, which:

1. asks **Gemini** (with Google Search) for 16 candidate articles using `prompt.txt`,
2. opens every link and keeps only real, direct article pages,
3. skips anything already sent,
4. publishes **10 short reads + 2 long reads** to `docs/feed.xml` (RSS), keeping the last 60 days.

If Gemini fails it retries, then tries Gemini's lite model, then falls back to the
RSS feeds in `sources.txt` (marked "backup pick"). If nothing at all can be published,
the old feed is kept and GitHub emails you about the failed run.

- **Change the topics/taste:** edit `prompt.txt`.
- **Change the backup sources:** edit `sources.txt`.
- **Run it now:** Actions → Daily Reads → Run workflow.
- **The API key** lives in Settings → Secrets → Actions → `GEMINI_API_KEY` — never in the files.
