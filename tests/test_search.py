from datetime import datetime, timedelta, timezone

import pytest

from app.errors import DomainError
from app.search import search_changes


@pytest.fixture
def data(db, f, world):
    w = world
    t = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
    vlan = f.change(w.req, title="Web VLAN", purpose="新增 Web Service VLAN", change_items="VLAN, Trunk",
                    devices=[w.sw], modules=[w.vlan], window_start=t, window_end=t + timedelta(hours=2))
    linux = f.change(w.req, title="Linux 網卡", purpose="新增服務網段", devices=[w.web], modules=[w.nic],
                     window_start=t + timedelta(days=10), window_end=t + timedelta(days=10, hours=1))
    db.flush()
    return vlan, linux


def ids(db, **kw):
    return {c.title for c in search_changes(db, **kw)}


def test_search_by_device_and_ip(db, world, data):
    assert ids(db, hostname="core-sw") == {"Web VLAN"}
    assert ids(db, ip="10.30.1.10") == {"Linux 網卡"}
    assert ids(db, ip="10.0.0.0/8") == {"Web VLAN", "Linux 網卡"}
    # 設備改 IP 後，舊 IP（變更當下快照）與新 IP 都查得到
    world.sw.ip = "172.16.0.1"
    db.flush()
    assert ids(db, ip="10.10.1.1") == {"Web VLAN"}
    assert ids(db, ip="172.16.0.1") == {"Web VLAN"}
    with pytest.raises(DomainError):
        search_changes(db, ip="not-an-ip")


def test_search_by_item_purpose_keyword(db, data):
    assert ids(db, item="trunk") == {"Web VLAN"}
    assert ids(db, item="新增網卡") == {"Linux 網卡"}  # 也比對計畫步驟名稱
    assert ids(db, item="A03") == {"Web VLAN"}  # 也比對模組代碼
    assert ids(db, purpose="服務網段") == {"Linux 網卡"}
    assert ids(db, q=data[0].number) == {"Web VLAN"}
    assert ids(db, q="100%") == set()  # LIKE 萬用字元已跳脫


def test_search_by_time_range(db, data):
    d = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    assert ids(db, date_from=d(2026, 9, 10), date_to=d(2026, 9, 11)) == {"Web VLAN"}
    assert ids(db, date_from=d(2026, 9, 15)) == {"Linux 網卡"}
    assert ids(db, date_to=d(2026, 9, 1)) == set()
