"""AI crawler / fetcher user-agent tokens, by app and role (checked November 2026).

Roles
  search  - builds the index the app's web search answers from. Blocking it can remove a site
            from that app's search answers.
  fetch   - fetches a page live when a user's question needs it. Blocking it stops that
            retrieval (some vendors say these fetches may ignore robots.txt).
  train   - collects content for model training. Per the vendors' docs, blocking it does not by
            itself remove a site from live search answers.
  control - a robots.txt-only token that governs use, not crawling (Google-Extended).

Edit this table as vendors change their docs. Entries with verified=False are reported as
informational only and never scored.
"""

BOTS = [
    # app, token, role, verified, source
    ("ChatGPT", "OAI-SearchBot", "search", True, "https://developers.openai.com/api/docs/bots"),
    ("ChatGPT", "ChatGPT-User", "fetch", True, "https://developers.openai.com/api/docs/bots"),
    ("ChatGPT", "GPTBot", "train", True, "https://developers.openai.com/api/docs/bots"),
    ("Claude", "Claude-SearchBot", "search", True, "https://support.claude.com/en/articles/8896518"),
    ("Claude", "Claude-User", "fetch", True, "https://support.claude.com/en/articles/8896518"),
    ("Claude", "ClaudeBot", "train", True, "https://support.claude.com/en/articles/8896518"),
    ("Perplexity", "PerplexityBot", "search", True, "https://docs.perplexity.ai/guides/bots"),
    ("Perplexity", "Perplexity-User", "fetch", True, "https://docs.perplexity.ai/guides/bots"),
    ("Meta AI (Muse)", "meta-externalagent", "search", True,
     "https://developers.facebook.com/docs/sharing/webmasters/web-crawlers"),  # Meta: training *and* indexing
    ("Meta AI (Muse)", "meta-externalfetcher", "fetch", True,
     "https://developers.facebook.com/docs/sharing/webmasters/web-crawlers"),
    ("Gemini", "Googlebot", "search", True, "https://developers.google.com/search/docs/crawling-indexing/google-common-crawlers"),
    ("Gemini", "Google-Extended", "control", True, "https://developers.google.com/search/docs/crawling-indexing/google-common-crawlers"),
    # xAI publishes no official crawler documentation; these tokens come from third-party directories.
    ("Grok", "GrokBot", "search", False, "unverified (no official xAI documentation)"),
    ("Grok", "xAI-SearchBot", "search", False, "unverified (no official xAI documentation)"),
]

ROLE_NOTES = {
    "search": "Blocking this can remove the page from {app}'s search answers.",
    "fetch": "Blocking this stops {app} fetching the page when a user's question needs it.",
    "train": "Training only. Per {app}'s docs this doesn't by itself remove you from live search answers.",
    "control": "Governs Gemini training and grounding in Gemini apps; Google Search / AI Overviews follow Googlebot.",
}

# Search bots used for the indicative "does the server treat bots differently" fetch.
PROBE_TOKENS = ["OAI-SearchBot", "Claude-SearchBot", "PerplexityBot"]


def as_dicts():
    return [{"app": a, "token": t, "role": r, "verified": v, "source": s} for a, t, r, v, s in BOTS]
