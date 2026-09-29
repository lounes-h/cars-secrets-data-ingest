import copy
import dataclasses
import io
import json
import re
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from ingest import ai
from ingest import vehicle_analysis as va
from ingest.api.app import create_app
from ingest.pipeline import process_file
from ingest.providers import COPART, PROVIDERS, LotImage, LotImages

from .conftest import read_sample


def jpeg(size=(2000, 1500), color="gray") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


def answer(**changes) -> dict:
    """A valid analysis of image_01 and image_02."""
    box = {"x": 0.1, "y": 0.5, "width": 0.9, "height": 0.4}
    data = {
        "schema_version": "1.0", "language": "en",
        "general_summary": {"summary": "2013 Acura TSX with front-end collision damage.",
                            "key_findings": ["Front bumper torn"],
                            "inspection_priorities": ["Radiator and its support"]},
        "damage_analysis": {
            "summary": "Front impact centred on the bumper.",
            "observed_damage": [{
                "damage_id": "damage_01", "part_name": "Front Bumper", "location": "front",
                "severity": "severe", "description": "Bumper cover torn and pushed in.",
                "confidence": 0.94, "evidence_image_ids": ["image_01", "image_02"]}],
            "possible_hidden_damage": [{
                "damage_id": "possible_damage_01", "part_name": "radiator",
                "description": "The radiator may be damaged.",
                "reason": "It sits right behind the crushed bumper.",
                "based_on_observed_damage_ids": ["damage_01"], "confidence": 0.62,
                "inspection_recommended": True}],
        },
        "damaged_parts": [{"part_name": "front_bumper", "severity": "severe",
                           "suggested_action": "replace", "confidence": 0.9,
                           "observed_damage_ids": ["damage_01"]}],
        "image_annotations": [{"image_id": "image_01", "regions": [{
            "region_id": "region_01", "damage_id": "damage_01", "part_name": "front_bumper",
            "label": "Front bumper damage", "explanation": "Torn cover.", "confidence": 0.9,
            "bounding_box": box}]}],
        "limitations": ["No photo of the underside."],
    }
    data.update(changes)
    return data


IMAGES = {"image_01", "image_02", "image_03"}


def test_a_valid_answer_is_stored_with_part_names_normalized():
    result = va.validate(answer(), "1.0", IMAGES, "en")
    assert result["damage_analysis"]["observed_damage"][0]["part_name"] == "front_bumper"
    assert result["image_annotations"][0]["regions"][0]["bounding_box"]["width"] == 0.9


def _set(path, value):
    def mutate(data):
        *keys, last = path
        target = data
        for k in keys:
            target = target[k]
        target[last] = value
    return mutate


OBSERVED = ("damage_analysis", "observed_damage", 0)
HIDDEN = ("damage_analysis", "possible_hidden_damage", 0)
REGION = ("image_annotations", 0, "regions", 0)


@pytest.mark.parametrize(("mutate", "problem"), [
    (_set((*OBSERVED, "confidence"), 1.2), "less than or equal to 1"),
    (_set((*OBSERVED, "severity"), "catastrophic"), "'minor', 'moderate'"),
    (_set((*OBSERVED, "location"), "somewhere"), "'front_left'"),
    (_set((*OBSERVED, "evidence_image_ids"), []), "at least 1 item"),
    (_set((*OBSERVED, "evidence_image_ids"), ["image_09"]), "image_09, which was not supplied"),
    (_set(("damaged_parts", 0, "suggested_action"), "total"), "'repair_or_replace'"),
    (_set(("damaged_parts", 0, "observed_damage_ids"), ["damage_07"]),
     "cites damage_07, which is not observed damage"),
    (_set((*HIDDEN, "based_on_observed_damage_ids"), ["damage_07"]),
     "based on damage_07, which is not observed damage"),
    (_set((*HIDDEN, "inspection_recommended"), False), "Input should be True"),
    (_set((*HIDDEN, "damage_id"), "damage_01"), "damage_id damage_01 is used twice"),
    (_set((*REGION, "bounding_box", "x"), 0.5), "extends beyond the photo"),
    (_set((*REGION, "bounding_box", "height"), 0.6), "extends beyond the photo"),
    (_set((*REGION, "bounding_box", "y"), -0.1), "greater than or equal to 0"),
    (_set((*REGION, "bounding_box", "width"), 0), "greater than 0"),
    (_set((*REGION, "damage_id"), "possible_damage_01"),
     "region_01 marks possible_damage_01, which is not observed damage"),
    (_set(("image_annotations", 0, "image_id"), "image_03"),
     "region_01 is on image_03, which is not evidence of damage_01"),
    (_set(("image_annotations", 0, "image_id"), "image_09"),
     "annotations of image_09, which was not supplied"),
    (_set(("language",), "fr"), "language is 'fr', not 'en'"),
    (_set(("schema_version",), "2.0"), "Input should be '1.0'"),
    (_set(("repair_cost",), 1200), "Extra inputs are not permitted"),
    (lambda d: d["damage_analysis"]["observed_damage"].append(
        d["damage_analysis"]["observed_damage"][0]), "damage_id damage_01 is used twice"),
    (lambda d: d["damaged_parts"].append(d["damaged_parts"][0]),
     "damaged part front_bumper is listed twice"),
    (lambda d: d["image_annotations"].append(d["image_annotations"][0]),
     "image_01 is annotated twice; region_id region_01 is used twice"),
])
def test_an_inconsistent_or_out_of_range_answer_is_rejected(mutate, problem):
    data = copy.deepcopy(answer())
    mutate(data)
    with pytest.raises(ValidationError, match=re.escape(problem)):
        va.validate(data, "1.0", IMAGES, "en")


def assess(n, view="close_up", damage=True, relevance=0.9, dup=None):
    return va.PhotoAssessment(photo_id=f"photo_{n:02d}", view=view, shows_damage=damage,
                              relevance=relevance, near_duplicate_of=dup)


IDS = [f"photo_{n:02d}" for n in range(1, 9)]


def test_choose_sends_the_most_relevant_damage_photos_in_listing_order():
    photos = [assess(2, relevance=0.9), assess(5, relevance=0.6), assess(7, relevance=0.8),
              assess(3, relevance=0.3), assess(1, view="exterior", damage=False, relevance=0.7)]
    assert va.choose(va.Selection(photos=photos), IDS, 2) == ["photo_02", "photo_07"]
    # only as many as are useful: a weak damage photo is not sent to reach the maximum
    assert va.choose(va.Selection(photos=photos), IDS, 6) == ["photo_02", "photo_05", "photo_07"]


def test_choose_skips_near_duplicates_whichever_way_they_are_declared():
    photos = [assess(2, relevance=0.9), assess(7, relevance=0.8, dup="photo_02"),
              assess(5, relevance=0.6), assess(6, relevance=0.95, dup=None),
              assess(4, relevance=0.7, dup="photo_06")]
    assert va.choose(va.Selection(photos=photos), IDS, 3) == ["photo_02", "photo_05", "photo_06"]


def test_choose_adds_overviews_when_little_damage_is_visible():
    photos = [assess(4, relevance=0.9), assess(1, view="exterior", damage=False, relevance=0.8),
              assess(2, view="exterior", damage=False, relevance=0.7),
              assess(3, view="exterior", damage=False, relevance=0.6),
              assess(8, view="interior", damage=False, relevance=0.9)]
    assert va.choose(va.Selection(photos=photos), IDS, 5) == ["photo_01", "photo_02", "photo_04"]
    assert va.choose(va.Selection(photos=photos), IDS, 2) == ["photo_01", "photo_04"]


def test_choose_falls_back_to_the_first_photos_and_ignores_unknown_ids():
    photos = [assess(9, relevance=1.0), assess(8, view="interior", damage=False)]
    assert va.choose(va.Selection(photos=photos), IDS, 3) == ["photo_01", "photo_02", "photo_03"]


def test_shrink_keeps_the_aspect_ratio_and_never_enlarges():
    data, original, size = va.shrink(jpeg((2000, 1500)), 1024)
    assert (original, size) == ((2000, 1500), (1024, 768))
    assert Image.open(io.BytesIO(data)).size == (1024, 768)
    assert va.shrink(jpeg((300, 200)), 1024)[1:] == ((300, 200), (300, 200))
    with pytest.raises(Exception, match="cannot identify image"):
        va.shrink(b"<html>not found</html>", 1024)


def test_download_reads_https_only_and_bounded(monkeypatch):
    with pytest.raises(ValueError, match="not an HTTPS URL"):
        va.download("http://cs.copart.com/a.jpg")
    body = [b"jpeg"]

    class Response(io.BytesIO):
        def __init__(self, request, timeout):
            assert request.full_url == "https://cs.copart.com/a.jpg" and timeout == 20
            super().__init__(body[0])

    monkeypatch.setattr(va.urllib.request, "urlopen", Response)
    assert va.download("https://cs.copart.com/a.jpg") == b"jpeg"
    body[0] = b"x" * (va.MAX_PHOTO_BYTES + 1)
    with pytest.raises(ValueError, match="larger than 15 MB"):
        va.download("https://cs.copart.com/a.jpg")


def test_metadata_keeps_known_values_and_condition_notes():
    from decimal import Decimal
    lot = {"source": "copart", "lot_number": "1", "year": 2013, "odometer": Decimal("81190.0"),
           "trim": "", "vin": "SECRET", "repair_cost": Decimal(1),
           "attributes": {"special_note": "FLOOD", "announcements": "", "grid_row": "A1"}}
    assert va.metadata(lot) == {"source": "copart", "lot_number": "1", "year": 2013,
                                "odometer": 81190.0,
                                "other_condition_notes": {"special_note": "FLOOD"}}


# --- running analyses ----------------------------------------------------------------------------

@pytest.fixture
def lot(conn, settings, make_export, monkeypatch):
    """A lot in the database whose provider lists `photos` (URLs -> JPEG bytes, or an error)."""
    header, rows = read_sample()
    process_file(conn, make_export(rows[:3]), COPART, settings)
    photos: dict[str, bytes | Exception] = {}

    def fetch(_lot):
        return LotImages([LotImage(thumb=None, full=url, hd=None) for url in photos], True)

    def download(url):
        if isinstance(photos[url], Exception):
            raise photos[url]
        return photos[url]

    monkeypatch.setitem(PROVIDERS, "copart", dataclasses.replace(COPART, fetch_images=fetch))
    monkeypatch.setattr(va, "download", download)
    return SimpleNamespace(number=rows[0][header.index("Lot number")], photos=photos,
                           damage=rows[0][header.index("Damage Description")])


class FakeAI:
    """Answers the selection step with `selection` and the analysis with `analysis`."""

    def __init__(self, monkeypatch, analysis=None, selection=None):
        self.analysis, self.selection, self.calls = analysis or answer(), selection, []
        monkeypatch.setattr(ai, "ask", self)

    def __call__(self, settings, system, parts, schema):
        self.calls.append(SimpleNamespace(system=system, parts=parts, schema=schema))
        data = self.selection if schema["title"] == "Selection" else self.analysis
        if isinstance(data, Exception):
            raise data
        return ai.Reply(data, json.dumps(data), f"req_{len(self.calls)}",
                        {"model": settings.ai_model, "input_tokens": 10, "output_tokens": 5})


def queue(conn, number, language="en"):
    return conn.execute(
        """INSERT INTO vehicle_analyses (source, lot_number, language, requested_by)
           VALUES ('copart', %s, %s, 'admin') RETURNING id""", (number, language),
    ).fetchone()["id"]


def stored(conn, analysis_id):
    return conn.execute("SELECT * FROM vehicle_analyses WHERE id = %s",
                        (analysis_id,)).fetchone()


def test_photos_are_selected_shrunk_and_analyzed(conn, settings, lot, monkeypatch):
    urls = [f"https://cs.copart.com/p{n}_ful.jpg" for n in range(1, 9)]
    lot.photos.update({url: jpeg() for url in urls})
    lot.photos[urls[1]] = OSError("404 Not Found")
    # photo_02 of the selection step is the listing's 3rd photo: the 2nd could not be had
    fake = FakeAI(monkeypatch, selection={"photos": [
        {"photo_id": "photo_02", "view": "close_up", "shows_damage": True, "relevance": 0.9,
         "near_duplicate_of": None},
        {"photo_id": "photo_03", "view": "close_up", "shows_damage": True, "relevance": 0.8,
         "near_duplicate_of": "photo_02"},
        {"photo_id": "photo_05", "view": "exterior", "shows_damage": True, "relevance": 0.7,
         "near_duplicate_of": None}]})
    analysis_id = queue(conn, lot.number)

    assert va.process_next(conn, settings) is True
    assert va.process_next(conn, settings) is False  # nothing left

    row = stored(conn, analysis_id)
    assert row["status"] == "completed" and row["error"] is None
    assert (row["provider"], row["model"], row["prompt_version"], row["schema_version"]) == (
        "anthropic", "claude-opus-5-5", "vehicle-damage-v1", "1.0")
    assert row["started_at"] <= row["completed_at"]
    assert row["result"]["damage_analysis"]["observed_damage"][0]["part_name"] == "front_bumper"
    assert [(i["image_id"], i["position"], i["full"]) for i in row["images"]] == [
        ("image_01", 3, urls[2]), ("image_02", 6, urls[5])]
    assert row["image_count"] == 2
    assert {(i["original_width"], i["width"], i["height"]) for i in row["images"]} == {
        (2000, 1024, 768)}
    assert row["selection"]["listed"] == 8 and row["selection"]["chosen"] == [
        "photo_02", "photo_05"]
    assert row["selection"]["unavailable"][0]["position"] == 2
    assert row["input_snapshot"]["damage_primary"] == lot.damage
    assert row["provider_request_id"] == "req_2"
    assert row["usage"] == {step: {"model": "claude-opus-5-5", "input_tokens": 10,
                                   "output_tokens": 5} for step in ("selection", "analysis")}
    assert row["selection"]["request_id"] == "req_1" and "usage" not in row["selection"]
    assert json.loads(row["raw_response"]) == answer()

    select, analyze = fake.calls
    assert f"primary damage {lot.damage}" in select.parts[0]
    assert select.parts[1] == "photo_01"
    assert max(Image.open(io.BytesIO(select.parts[2])).size) == va.SELECTION_PX
    assert len(select.parts) == 1 + 2 * 7
    assert "Write every text field in English" in analyze.system
    assert '"damage_primary"' in analyze.parts[0]
    assert analyze.parts[2::2] == ["image_01", "image_02"]
    assert Image.open(io.BytesIO(analyze.parts[3])).size == (1024, 768)
    assert analyze.schema["title"] == "AnalysisV1"


def test_a_lot_with_few_photos_sends_them_all_without_a_selection_step(
        conn, settings, lot, monkeypatch):
    lot.photos["https://cs.copart.com/a_ful.jpg"] = jpeg((640, 480))
    lot.photos["https://cs.copart.com/b_ful.jpg"] = jpeg((640, 480))
    fake = FakeAI(monkeypatch, selection=AssertionError("no selection step"))
    analysis_id = queue(conn, lot.number)
    va.process_next(conn, settings)
    row = stored(conn, analysis_id)
    assert row["status"] == "completed"
    assert [(i["image_id"], i["width"]) for i in row["images"]] == [
        ("image_01", 640), ("image_02", 640)]
    assert len(fake.calls) == 1 and "chosen" not in row["selection"]


@pytest.mark.parametrize(("setup", "error"), [
    (lambda lot, s: None, "AnalysisError: the lot has no usable photos"),
    (lambda lot, s: lot.photos.update({"https://cs.copart.com/a.jpg": OSError("timed out"),
                                       "https://cs.copart.com/b.jpg": b"<html>"}),
     "none of the lot's 2 photos could be used"),
    (lambda lot, s: setattr(s, "ai_prompt_version", "vehicle-damage-v0"),
     "unknown prompt version 'vehicle-damage-v0'"),
    (lambda lot, s: setattr(s, "ai_schema_version", "0.9"), "unknown schema version '0.9'"),
])
def test_an_analysis_that_cannot_be_made_fails_with_the_reason(
        conn, settings, lot, monkeypatch, setup, error):
    FakeAI(monkeypatch)
    setup(lot, settings)
    analysis_id = queue(conn, lot.number)
    va.process_next(conn, settings)
    row = stored(conn, analysis_id)
    assert row["status"] == "failed" and error in row["error"]
    assert row["result"] is None and row["completed_at"] is not None


def test_an_unsupported_language_fails(conn, settings, lot, monkeypatch):
    FakeAI(monkeypatch)
    analysis_id = queue(conn, lot.number, language="fr")
    va.process_next(conn, settings)
    assert stored(conn, analysis_id)["error"] == "AnalysisError: unsupported language 'fr'"


def test_a_provider_without_photos_fails(conn, settings, lot, monkeypatch):
    monkeypatch.setitem(PROVIDERS, "copart", dataclasses.replace(COPART, fetch_images=None))
    analysis_id = queue(conn, lot.number)
    va.process_next(conn, settings)
    assert stored(conn, analysis_id)["error"] == "AnalysisError: the lot has no usable photos"


def test_a_rejected_request_or_answer_keeps_what_the_provider_returned(
        conn, settings, lot, monkeypatch):
    lot.photos["https://cs.copart.com/a_ful.jpg"] = jpeg()
    spent = {"model": "claude-opus-5-5", "input_tokens": 3000, "output_tokens": 900,
             "cost_usd": 0.03}
    FakeAI(monkeypatch, analysis=ai.AIError("the answer is not valid JSON", "{oops", "req_9",
                                            spent))
    first = queue(conn, lot.number)
    va.process_next(conn, settings)
    row = stored(conn, first)
    assert row["status"] == "failed"
    assert row["error"] == "AIError: the answer is not valid JSON"
    assert (row["raw_response"], row["provider_request_id"]) == ("{oops", "req_9")
    assert row["usage"] == {"analysis": spent}  # an unusable answer is billed all the same
    assert row["images"][0]["image_id"] == "image_01"  # the input is kept for debugging

    bad = answer()
    bad["image_annotations"][0]["image_id"] = "image_02"  # not supplied
    FakeAI(monkeypatch, analysis=bad)
    second = queue(conn, lot.number)
    va.process_next(conn, settings)
    row = stored(conn, second)
    assert row["status"] == "failed" and "ValidationError" in row["error"]
    assert "image_02, which was not supplied" in row["error"]
    assert json.loads(row["raw_response"]) == bad and row["provider_request_id"] == "req_1"
    assert row["result"] is None


def test_an_invalid_photo_selection_fails(conn, settings, lot, monkeypatch):
    settings.ai_max_images = 1
    lot.photos.update({f"https://cs.copart.com/{n}.jpg": jpeg((64, 48)) for n in "ab"})
    FakeAI(monkeypatch, selection={"photos": [{"photo_id": "photo_01"}]})
    analysis_id = queue(conn, lot.number)
    va.process_next(conn, settings)
    row = stored(conn, analysis_id)
    assert row["error"].startswith("AIError: invalid photo selection")
    assert row["provider_request_id"] == "req_1"
    assert list(row["usage"]) == ["selection"]


def test_interrupted_analyses_fail(conn, lot):
    analysis_id = queue(conn, lot.number)
    conn.execute("UPDATE vehicle_analyses SET status = 'processing'")
    assert va.fail_interrupted(conn) == 1
    assert stored(conn, analysis_id)["error"].startswith("interrupted")


# --- API -----------------------------------------------------------------------------------------

@pytest.fixture
def authed(conn, settings):
    with TestClient(create_app(settings, background=False)) as c:
        assert c.post("/api/auth/login", json={"username": "admin",
                                               "password": "test-password"}).status_code == 200
        yield c


def test_request_run_and_regenerate_through_the_api(authed, conn, settings, lot, monkeypatch):
    url = f"/api/lots/copart/{lot.number}/analyses"
    lot.photos["https://cs.copart.com/a_ful.jpg"] = jpeg()
    lot.photos["https://cs.copart.com/b_ful.jpg"] = jpeg()
    assert authed.get(url).json() == {"latest": None, "current": None}

    queued = authed.post(url)
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued" and queued.json()["requested_by"] == "admin"
    assert queued.json()["language"] == "en"
    again = authed.post(url, json={"language": "en"})
    assert (again.status_code, again.json()["detail"]) == (
        409, "an analysis of this lot is already in progress")
    assert authed.get(url).json()["current"]["status"] == "queued"

    FakeAI(monkeypatch)
    va.process_next(conn, settings)
    body = authed.get(url).json()
    assert body["latest"]["id"] == body["current"]["id"] == queued.json()["id"]
    assert body["latest"]["result"]["language"] == "en"
    assert body["latest"]["images"][0]["full"] == "https://cs.copart.com/a_ful.jpg"
    assert body["latest"]["usage"]["analysis"]["output_tokens"] == 5

    # a failed regeneration leaves the completed analysis in place
    assert authed.post(url).status_code == 202
    FakeAI(monkeypatch, analysis=ai.AIError("the AI request failed: APITimeoutError"))
    va.process_next(conn, settings)
    body = authed.get(url).json()
    assert body["latest"]["id"] == queued.json()["id"]
    assert body["current"]["status"] == "failed"
    assert "APITimeoutError" in body["current"]["error"]
    assert conn.execute("SELECT count(*) AS n FROM vehicle_analyses").fetchone()["n"] == 2


def test_the_model_chosen_for_a_request_runs_it(authed, conn, settings, lot, monkeypatch):
    url = f"/api/lots/copart/{lot.number}/analyses"
    lot.photos.update({f"https://cs.copart.com/{n}_ful.jpg": jpeg() for n in "ab"})
    offered = authed.get("/api/analysis-models").json()
    assert offered["default"] == "claude-opus-5-5"
    assert [m["id"] for m in offered["models"]] == [
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]
    assert offered["models"][1] == {
        "id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5",
        "note": "Faster, half the price of Opus 5.5", "input_usd": 2.0, "output_usd": 10.0}

    refused = authed.post(url, json={"model": "claude-opus-4-8"})
    assert (refused.status_code, refused.json()["detail"]) == (
        422, "unsupported model 'claude-opus-4-8'")
    assert authed.post(url, json={"model": "claude-sonnet-5-5"}).json()["model"] == (
        "claude-sonnet-5-5")
    FakeAI(monkeypatch)
    va.process_next(conn, settings)
    latest = authed.get(url).json()["latest"]
    assert latest["model"] == "claude-sonnet-5-5"
    assert latest["usage"]["analysis"]["model"] == "claude-sonnet-5-5"  # the model that ran
    assert settings.ai_model == "claude-opus-5-5"  # the worker's own settings are untouched


def test_analysis_requests_are_checked(authed, lot):
    assert authed.get("/api/lots/copart/0/analyses").status_code == 404
    assert authed.post("/api/lots/copart/0/analyses").status_code == 404
    refused = authed.post(f"/api/lots/copart/{lot.number}/analyses", json={"language": "xx"})
    assert (refused.status_code, refused.json()["detail"]) == (422, "unsupported language 'xx'")
    authed.post("/api/auth/logout")
    authed.cookies.clear()
    assert authed.get(f"/api/lots/copart/{lot.number}/analyses").status_code == 401
    assert authed.post(f"/api/lots/copart/{lot.number}/analyses").status_code == 401


# --- the Anthropic client ------------------------------------------------------------------------

class FakeStream:
    """Like the SDK's: the request id is on the stream, not on the final message."""

    request_id = "req_abc"

    def __init__(self, sent, message):
        self.sent, self.message = sent, message

    def __call__(self, **kwargs):
        self.sent.update(kwargs)
        if isinstance(self.message, Exception):
            raise self.message
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get_final_message(self):
        return self.message


def fake_anthropic(monkeypatch, message):
    sent, clients = {}, []

    def client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
            stream=FakeStream(sent, message))))

    monkeypatch.setattr(ai.anthropic, "Anthropic", client)
    return sent, clients


def message(text, stop_reason="end_turn", stop_details=None):
    reply = SimpleNamespace(
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=stop_details, model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=800))
    return reply


def test_anthropic_client_sends_photos_and_the_schema(settings, monkeypatch):
    settings.anthropic_api_key = "sk-test"
    sent, clients = fake_anthropic(monkeypatch, message('{"photos": []}'))
    reply = ai.ask(settings, "system prompt", ["photo_01", b"\xff\xd8"],
                   va.Selection.model_json_schema())
    assert reply == ai.Reply({"photos": []}, '{"photos": []}', "req_abc", {
        "model": "claude-opus-5-5", "input_tokens": 1200, "output_tokens": 800,
        "cost_usd": 0.0208})  # 1,200 x $4 + 800 x $20 per million
    assert clients == [{"api_key": "sk-test", "timeout": ai.TIMEOUT}]
    assert sent["model"] == "claude-opus-5-5" and sent["system"] == "system prompt"
    assert sent["messages"][0]["content"] == [
        {"type": "text", "text": "photo_01"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": "/9g="}}]
    schema = sent["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False and "photos" in schema["properties"]
    assert sent["fallbacks"] == "default"


@pytest.mark.parametrize(("reply", "error", "raw"), [
    (message("", "refusal", SimpleNamespace(category="cyber")),
     "the model declined the request (cyber)", ""),
    (message("", "refusal"), "the model declined the request (no category)", ""),
    (message('{"photos": [', "max_tokens"), "cut off at the token limit", '{"photos": ['),
    (message("Sure! Here it is"), "not valid JSON", "Sure! Here it is"),
])
def test_anthropic_answers_that_are_not_usable_fail_with_the_raw_text(
        settings, monkeypatch, reply, error, raw):
    fake_anthropic(monkeypatch, reply)
    with pytest.raises(ai.AIError, match=re.escape(error)) as caught:
        ai.ask(settings, "s", ["t"], {"type": "object"})
    assert (caught.value.raw, caught.value.request_id) == (raw, "req_abc")
    assert caught.value.usage["cost_usd"] == 0.0208  # the answer came back: it is billed


def test_anthropic_errors_become_ai_errors(settings, monkeypatch):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, request=request, headers={"request-id": "req_bad"})
    fake_anthropic(monkeypatch, anthropic.BadRequestError(
        "image too large", response=response, body=None))
    with pytest.raises(ai.AIError, match=r"rejected the request \(400\): image too large") as e:
        ai.ask(settings, "s", ["t"], {"type": "object"})
    assert e.value.request_id == "req_bad"

    fake_anthropic(monkeypatch, anthropic.APITimeoutError(request=request))
    with pytest.raises(ai.AIError, match="the AI request failed: APITimeoutError"):
        ai.ask(settings, "s", ["t"], {"type": "object"})


def test_cost_follows_the_model_price_and_is_unknown_for_other_models():
    assert ai.cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == 3.0
    assert ai.cost_usd("claude-opus-4-8", 1000, 1000) is None


def test_an_unknown_provider_is_an_error(settings):
    settings.ai_provider = "acme"
    with pytest.raises(ai.AIError, match="unknown AI provider 'acme' \\(known: anthropic\\)"):
        ai.ask(settings, "s", ["t"], {"type": "object"})
