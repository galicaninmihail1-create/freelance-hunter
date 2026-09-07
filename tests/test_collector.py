import httpx

from app.collectors.fl_rss import FLRSSCollector


async def test_fl_rss_collector_parses_entries_and_skips_malformed() -> None:
    xml = b"""<?xml version='1.0'?><rss version='2.0'><channel><item><guid>42</guid><title>Telegram bot + CRM</title><link>https://www.fl.ru/projects/42/</link><description>API integration, 40 000 RUB</description><pubDate>Mon, 01 Jan 2026 12:00:00 +0000</pubDate></item><item><title></title></item></channel></rss>"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=xml)))
    collector = FLRSSCollector(("https://rss.example.test",), client)
    try:
        jobs = await collector.collect()
    finally:
        await client.aclose()
    assert len(jobs) == 1
    assert jobs[0].source_job_id == "42"
    assert jobs[0].budget_text == "API integration, 40 000 RUB"
    assert jobs[0].metadata["feed_url"] == "https://rss.example.test"
    assert collector.last_feed_stats[0].items_seen == 2
    assert collector.last_feed_stats[0].items_emitted == 1
    assert any(issue.stage == "rss_item" for issue in collector.last_errors)
