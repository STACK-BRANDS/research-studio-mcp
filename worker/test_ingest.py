from worker.ingest import dedup

def test_dedup_collapses_same_adid():
    ads = [{"ad_id": "1", "text": "a"}, {"ad_id": "1", "text": "a"}, {"ad_id": "2", "text": "b"}]
    out = dedup(ads)
    assert len(out) == 2
    assert {a["ad_id"] for a in out} == {"1", "2"}
