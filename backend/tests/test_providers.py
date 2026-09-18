import pytest

from ingest.converters import to_text
from ingest.providers import (
    COPART,
    PROVIDERS,
    FieldMapping,
    Provider,
    copart,
    get_provider,
)
from ingest.schema import COLUMN_NAMES


def test_registry():
    assert get_provider("copart") is COPART
    assert get_provider("iaai") is None
    assert set(PROVIDERS) == {"copart"}


def test_copart_maps_headers_to_canonical_columns():
    assert COPART.mapped_to("lot_number") == "lot_number"
    assert COPART.mapped_to("high_bid_non_vix_sealed_vix") == "high_bid"
    assert COPART.mapped_to("sale_time_hhmm") == "sale_at"
    assert COPART.mapped_to("time_zone") == "sale_at"
    assert COPART.mapped_to("grid_row") is None
    assert COPART.removal_scope == "yard_code"


def test_every_copart_mapping_targets_a_canonical_column():
    assert {m.column for m in COPART.fields} <= set(COLUMN_NAMES)


def test_mapping_to_unknown_column_is_refused():
    with pytest.raises(ValueError, match="unknown canonical column"):
        FieldMapping("colour", ("color",), to_text)


def test_unknown_removal_scope_is_refused():
    with pytest.raises(ValueError, match="removal scope"):
        Provider(name="x", display_name="X", key_aliases=("lot",), key_convert=str, fields=(),
                 removal_scope="branch")


def test_lots_table_has_every_canonical_column(conn):
    """The migration and schema.CANONICAL_COLUMNS must not drift apart."""
    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'lots'"
    )}
    assert set(COLUMN_NAMES) <= cols


THUMB = "https://cs.copart.com/v1/AUTH_svc.pdoc00001/lpp/0726/abc_thb.jpg"
LOT = {"lot_number": "41633106", "thumbnail_url": THUMB,
       "image_url": "http://inventoryv2.copart.io/v1/lotImages/41633106?country=us&yardNumber=1"}


def link(url, thumb=False, hd=False, sound=False):
    return {"url": url, "isThumbNail": thumb, "isHdImage": hd, "isEngineSound": sound}


def test_copart_images_lists_every_photo_in_sequence_order():
    calls = []

    def get_json(url):
        calls.append(url)
        return {"imgCount": 2, "lotImages": [
            {"sequence": 2, "link": [link("https://cs.copart.com/b_ful.jpg "),
                                     link("https://cs.copart.com/b_thb.jpg", thumb=True)]},
            {"sequence": 1, "link": [link("https://cs.copart.com/a_ful.jpg"),
                                     link("https://cs.copart.com/a_hrs.jpg", hd=True),
                                     link("https://cs.copart.com/a_thb.jpg", thumb=True)]},
        ]}

    result = copart.images(LOT, get_json)
    images = result.images
    assert result.complete is True
    # always HTTPS, even though the export stores an http:// link
    assert calls == ["https://inventoryv2.copart.io/v1/lotImages/41633106?country=us&yardNumber=1"]
    assert [i.full for i in images] == ["https://cs.copart.com/a_ful.jpg",
                                        "https://cs.copart.com/b_ful.jpg"]
    assert images[0].hd == "https://cs.copart.com/a_hrs.jpg"
    assert images[1].thumb == "https://cs.copart.com/b_thb.jpg" and images[1].hd is None


def test_copart_images_drops_foreign_hosts_and_engine_sounds():
    def get_json(url):
        return {"lotImages": [
            {"sequence": 1, "link": [link("https://evil.example/x.jpg")]},
            {"sequence": 2, "link": [link("https://cs.copart.com/s.mp3", sound=True)]},
            {"sequence": 3, "link": [link("https://cs.copart.com/ok_ful.jpg")]},
        ]}

    assert [i.full for i in copart.images(LOT, get_json).images] == [
        "https://cs.copart.com/ok_ful.jpg"]


def test_copart_images_falls_back_to_export_thumbnail():
    def failing(url):
        raise OSError("timeout")

    result = copart.images(LOT, failing)
    (image,) = result.images
    assert image.thumb == THUMB
    assert image.full == THUMB.replace("_thb.", "_ful.")
    assert result.complete is False  # the UI then links to the lot page for all photos


def test_copart_image_api_404_counts_as_incomplete():
    """Real case, lot 63265186: the API answers a 404 body although copart.com shows 12
    photos."""
    def not_found(url):
        return {"status": 404, "message": "No message available"}

    result = copart.images(LOT, not_found)
    assert [i.thumb for i in result.images] == [THUMB] and result.complete is False


def test_copart_listing_url():
    assert copart.listing_url("63265186") == "https://www.copart.com/lot/63265186"
    assert copart.listing_url("1/../x") == "https://www.copart.com/lot/1%2F..%2Fx"
    assert COPART.listing_url is copart.listing_url


def test_copart_images_never_calls_an_unexpected_host():
    def get_json(url):
        raise AssertionError("must not be called")

    lot = {**LOT, "image_url": "http://169.254.169.254/v1/lotImages/1"}
    assert [i.thumb for i in copart.images(lot, get_json).images] == [THUMB]
    assert copart.images({"lot_number": "1"}, get_json).images == []
    assert copart.images({"thumbnail_url": "https://elsewhere.com/a_thb.jpg"},
                         get_json).images == []
