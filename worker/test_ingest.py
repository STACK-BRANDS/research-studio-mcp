from worker.ingest import dedup

def test_dedup_collapses_same_adid():
    ads = [{"ad_id": "1", "text": "a"}, {"ad_id": "1", "text": "a"}, {"ad_id": "2", "text": "b"}]
    out = dedup(ads)
    assert len(out) == 2
    assert {a["ad_id"] for a in out} == {"1", "2"}


import time

from worker.ingest import select_for_analysis


def test_select_returns_all_when_small():
    ads = [{"ad_id": str(i)} for i in range(5)]
    assert select_for_analysis(ads, cap=30) == ads


def test_select_stratifies_by_days_active_when_dated():
    now = int(time.time())
    # i=0 newest (fresh), i=99 oldest start = longest-running
    ads = [{"ad_id": str(i), "start_date": now - i * 86400} for i in range(100)]
    sel = select_for_analysis(ads, cap=10)
    ids = {a["ad_id"] for a in sel}
    assert len(sel) == 10 and "0" in ids and "99" in ids


def test_select_drops_inactive_ads():
    now = int(time.time())
    ads = [{"ad_id": "old", "end_date": now - 999999}] + [{"ad_id": str(i)} for i in range(40)]
    assert "old" not in {a["ad_id"] for a in select_for_analysis(ads, cap=30)}
