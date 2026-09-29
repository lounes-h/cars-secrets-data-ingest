"""AI condition and damage analysis of one lot, requested from the lot page and run by the worker.

The API queues a request (`vehicle_analyses`, status `queued`); the worker claims it, lists the
lot's photos through its provider, downloads and shrinks them, and, when the lot has more photos
than `AI_MAX_IMAGES`, lets the model pick the ones that best show the reported damage from small
copies (the selection step). The chosen photos, renamed image_01, image_02, …, go to the model
with the lot's metadata; the answer must match the schema and be consistent (every reference
points at a supplied photo or an observed damage) to be stored as `completed`. Anything else is
`failed`, with the error and the raw answer. Each request is a new row: regenerating never
touches earlier analyses.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import structlog
from PIL import Image, ImageOps
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
)

from . import ai, db
from .config import Settings
from .providers import PROVIDERS, LotImage

log = structlog.get_logger()

LANGUAGES = {"en": "English"}  # output languages a request may ask for

# --- the answer's contract, schema 1.0 -----------------------------------------------------------

Severity = Literal["minor", "moderate", "severe", "unknown"]
Action = Literal["inspect", "repair", "replace", "repair_or_replace", "unknown"]
Location = Literal["front", "front_left", "front_right", "left", "right", "rear", "rear_left",
                   "rear_right", "roof", "underbody", "interior", "engine_bay", "unknown"]
Confidence = Annotated[float, Field(ge=0, le=1)]
Fraction = Annotated[float, Field(ge=0, le=1)]


def _snake_case(value: Any) -> Any:
    """`Left Front Fender` -> `left_front_fender`: the same part is always spelled the same."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") if isinstance(value, str) else value


PartName = Annotated[str, BeforeValidator(_snake_case), Field(min_length=1)]
IdList = Annotated[list[str], Field(min_length=1)]
EDGE = 1e-6  # float rounding: 0.1 + 0.9 is a hair over 1


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralSummary(Strict):
    summary: str
    key_findings: list[str]
    inspection_priorities: list[str]


class ObservedDamage(Strict):
    damage_id: str
    part_name: PartName
    location: Location
    severity: Severity
    description: str
    confidence: Confidence
    evidence_image_ids: IdList


class PossibleHiddenDamage(Strict):
    damage_id: str
    part_name: PartName
    description: str
    reason: str
    based_on_observed_damage_ids: IdList
    confidence: Confidence
    inspection_recommended: Literal[True]


class DamageAnalysis(Strict):
    summary: str
    observed_damage: list[ObservedDamage]
    possible_hidden_damage: list[PossibleHiddenDamage]


class DamagedPart(Strict):
    part_name: PartName
    severity: Severity
    suggested_action: Action
    confidence: Confidence
    observed_damage_ids: IdList


class BoundingBox(Strict):
    """Fractions of the photo's width and height, origin at its top-left corner."""

    x: Fraction
    y: Fraction
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def inside_the_photo(self) -> BoundingBox:
        if self.x + self.width > 1 + EDGE or self.y + self.height > 1 + EDGE:
            raise ValueError("the bounding box extends beyond the photo")
        return self


class Region(Strict):
    region_id: str
    damage_id: str
    part_name: PartName
    label: str
    explanation: str
    confidence: Confidence
    bounding_box: BoundingBox


class ImageAnnotation(Strict):
    image_id: str
    regions: Annotated[list[Region], Field(min_length=1)]


class AnalysisV1(Strict):
    schema_version: Literal["1.0"]
    language: str
    general_summary: GeneralSummary
    damage_analysis: DamageAnalysis
    damaged_parts: list[DamagedPart]
    image_annotations: list[ImageAnnotation]
    limitations: list[str]

    @model_validator(mode="after")
    def consistent(self, info: ValidationInfo) -> AnalysisV1:
        """Every ID is unique and every reference points at a supplied photo or at observed
        damage (`context`: the supplied `image_ids` and the requested `language`)."""
        context = info.context or {}
        images: set[str] = context.get("image_ids", set())
        problems = []
        if self.language != context.get("language", self.language):
            problems.append(f"language is {self.language!r}, not {context['language']!r}")
        observed: dict[str, ObservedDamage] = {}
        for d in self.damage_analysis.observed_damage:
            if d.damage_id in observed:
                problems.append(f"damage_id {d.damage_id} is used twice")
            observed[d.damage_id] = d
            problems += [f"{d.damage_id} cites {i}, which was not supplied"
                         for i in d.evidence_image_ids if i not in images]
        hidden: set[str] = set()
        for h in self.damage_analysis.possible_hidden_damage:
            if h.damage_id in hidden or h.damage_id in observed:
                problems.append(f"damage_id {h.damage_id} is used twice")
            hidden.add(h.damage_id)
            problems += [f"{h.damage_id} is based on {i}, which is not observed damage"
                         for i in h.based_on_observed_damage_ids if i not in observed]
        parts: set[str] = set()
        for p in self.damaged_parts:
            if p.part_name in parts:
                problems.append(f"damaged part {p.part_name} is listed twice")
            parts.add(p.part_name)
            problems += [f"damaged part {p.part_name} cites {i}, which is not observed damage"
                         for i in p.observed_damage_ids if i not in observed]
        annotated: set[str] = set()
        regions: set[str] = set()
        for a in self.image_annotations:
            if a.image_id not in images:
                problems.append(f"annotations of {a.image_id}, which was not supplied")
            if a.image_id in annotated:
                problems.append(f"{a.image_id} is annotated twice")
            annotated.add(a.image_id)
            for r in a.regions:
                if r.region_id in regions:
                    problems.append(f"region_id {r.region_id} is used twice")
                regions.add(r.region_id)
                damage = observed.get(r.damage_id)
                if damage is None:
                    problems.append(f"{r.region_id} marks {r.damage_id}, "
                                    "which is not observed damage")
                elif a.image_id not in damage.evidence_image_ids:
                    problems.append(f"{r.region_id} is on {a.image_id}, "
                                    f"which is not evidence of {r.damage_id}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


SCHEMAS: dict[str, type[AnalysisV1]] = {"1.0": AnalysisV1}


def validate(data: Any, schema_version: str, image_ids: set[str], language: str) -> dict:
    """The answer as stored, or pydantic's ValidationError listing every problem."""
    model = SCHEMAS[schema_version].model_validate(
        data, context={"image_ids": image_ids, "language": language})
    return model.model_dump(mode="json")


# --- photo selection -----------------------------------------------------------------------------

class PhotoAssessment(Strict):
    photo_id: str
    view: Literal["exterior", "close_up", "interior", "engine_bay", "undercarriage", "other"]
    shows_damage: bool
    relevance: Confidence
    near_duplicate_of: str | None


class Selection(Strict):
    photos: list[PhotoAssessment]


SELECTION_PX = 384  # the selection step only needs to tell what a photo shows
DAMAGE_RELEVANCE = 0.5  # a damage photo at least this relevant is sent
MIN_DAMAGE_PHOTOS = 2  # with fewer damage photos, overviews of the outside are added …
MAX_OVERVIEW_PHOTOS = 2  # … at most this many


def choose(selection: Selection, photo_ids: list[str], max_images: int) -> list[str]:
    """The photos to send, in listing order: the most relevant clear views of the damage, never
    two near-duplicates, and only as many as are useful. When fewer than MIN_DAMAGE_PHOTOS show
    damage, a few overviews of the outside give context; if nothing qualifies, the first
    photos of the listing are sent."""
    known = {p.photo_id: p for p in selection.photos if p.photo_id in photo_ids}
    ranked = sorted(known.values(), key=lambda p: -p.relevance)
    chosen: list[PhotoAssessment] = []

    def add(accept: Any, limit: int) -> None:
        added = 0
        for p in ranked:
            if len(chosen) >= max_images or added >= limit:
                return
            twin = any(p.near_duplicate_of == c.photo_id or c.near_duplicate_of == p.photo_id
                       for c in chosen)
            if accept(p) and p not in chosen and not twin:
                chosen.append(p)
                added += 1

    add(lambda p: p.shows_damage and p.relevance >= DAMAGE_RELEVANCE, max_images)
    if len(chosen) < MIN_DAMAGE_PHOTOS:
        add(lambda p: p.view == "exterior", MAX_OVERVIEW_PHOTOS)
    ids = {p.photo_id for p in chosen} or set(photo_ids[:max_images])
    return [i for i in photo_ids if i in ids]


# --- prompts -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Prompt:
    select: str  # system prompt of the photo selection step
    analyze: str  # system prompt of the analysis; format() fills the language and schema version


VEHICLE_DAMAGE_V1 = Prompt(
    select="""\
You choose which auction photos of a vehicle an appraiser should study to assess its damage.

Each photo is preceded by its ID (photo_01, photo_02, …); the photos are small copies, which is \
expected. For every photo return:
- view: exterior (most of the vehicle's outside), close_up (a close view of part of the outside), \
interior, engine_bay, undercarriage, or other (documents, labels, odometer, keys, …).
- shows_damage: whether collision or body damage is clearly visible.
- relevance: 0 to 1, how much the photo helps assess the damage: highest for clear views of the \
reported damage and of the area of the vehicle it concerns, low for photos that add nothing.
- near_duplicate_of: the ID of an earlier photo that shows nearly the same thing from nearly the \
same angle, or null.

Judge only what you can see.""",
    analyze="""\
You are a senior vehicle damage appraiser. A used-car dealer is considering a vehicle sold at a \
salvage auction and wants your assessment of its condition and damage from the auction's \
metadata and the photos supplied with this request.

Accuracy matters more than completeness: a short, correct analysis beats a long one with guesses. \
Do not estimate repair costs, values or prices.

Evidence
- There are two sources: the auction metadata (JSON) and the photos. The metadata comes from the \
auction or the seller; it may be incomplete, broad ("FRONT END") or contradict the photos. Treat \
it as a claim to check, not a fact, and say when the photos contradict it or cannot confirm it.
- Describe as observed only what you can see in the supplied photos. Never invent damage.
- The photos are a selection and do not show every area. An area no photo shows is unknown, \
never undamaged; name significant unseen areas in limitations.
- Exterior appearance alone never confirms mechanical, structural, frame, airbag or electrical \
damage: such damage can only be possible hidden damage.
- Each photo is preceded by its ID (image_01, image_02, …). Refer to photos only by these exact \
IDs.

Observed damage (damage_analysis.observed_damage)
- One record per distinct damage visible in the photos, with IDs damage_01, damage_02, … When the \
same damage appears in several photos, make one record that lists every photo showing it in \
evidence_image_ids.
- part_name: the most specific physical part, in snake_case: front_bumper, rear_bumper, hood, \
grille, left_front_fender, right_front_fender, left_headlight, right_headlight, windshield, \
left_front_door, right_front_door, left_quarter_panel, trunk_lid, … Left and right are the \
vehicle's own, as seen from the driver's seat. Use a broader label only when no specific part can \
be identified.
- location: where on the vehicle the damage is.
- severity: minor (cosmetic: scratches, scuffs, small dents), moderate (the part needs repair or \
replacement), severe (heavy deformation, parts torn off or crushed, damage reaching adjacent \
structure), or unknown when the photos do not allow a judgement.
- confidence: 0 to 1, how certain you are that the damage is real and correctly described.

Possible hidden damage (damage_analysis.possible_hidden_damage)
- Damage that cannot be seen but is plausible given observed damage: suspension, cooling system \
(radiator, condenser), crash structure, bumper reinforcement, sensors and cameras, airbags, \
mechanical parts near an impact area.
- IDs possible_damage_01, possible_damage_02, … Each cites the observed damage it is inferred \
from, explains why in reason, and sets inspection_recommended to true. It is never confirmed; \
list it only when observed damage supports it.

Damaged parts (damaged_parts)
- A deduplicated list: one entry per physical part with observed damage, citing that damage's \
IDs. Never list a part only because hidden damage is possible there.
- suggested_action: inspect, repair, replace, repair_or_replace or unknown. It is a visual \
assessment, not a repair instruction.

Image annotations (image_annotations)
- One entry per photo on which observed damage is visible, with the regions that show it: \
region_01, region_02, … numbered across the whole answer.
- bounding_box: x, y, width and height as fractions (0 to 1) of the photo's width and height, \
origin at its top-left corner. Keep the box inside the photo (x + width ≤ 1, y + height ≤ 1) and \
tight around the visible damage.
- A region marks observed damage (its damage_id) on a photo listed in that damage's \
evidence_image_ids. Never draw a region for possible hidden damage.

Summaries
- general_summary: the vehicle and its overall condition in a few sentences, the key findings, \
and what a buyer should inspect first.
- damage_analysis.summary: the damage in a few sentences.
- limitations: what limits this analysis: photo coverage, quality or lighting, missing or \
contradicting metadata.

Write every text field in {language}; keep IDs, part names and listed values as specified. \
schema_version is "{schema_version}" and language is "{language_code}".""",
)

PROMPTS: dict[str, Prompt] = {"vehicle-damage-v1": VEHICLE_DAMAGE_V1}

# --- input ---------------------------------------------------------------------------------------

# The lot columns the model sees, and source columns of the export about its condition.
METADATA = ("source", "lot_number", "vehicle_type", "year", "make", "model_group",
            "model_detail", "trim", "body_style", "color", "damage_primary", "damage_secondary",
            "title_type", "title_state", "has_keys", "odometer", "odometer_brand",
            "est_retail_value", "currency", "engine", "drive", "transmission", "fuel_type",
            "cylinders", "runs_drives", "sale_status", "location_city", "location_state",
            "location_country")
CONDITION_ATTRIBUTES = ("lot_cond_code", "special_note", "announcements", "autograde",
                        "sale_light")


def metadata(lot: dict[str, Any]) -> dict[str, Any]:
    """The lot's known metadata, as sent to the model and stored with the analysis."""
    snapshot = {k: float(v) if isinstance(v, Decimal) else v
                for k in METADATA if (v := lot.get(k)) not in (None, "")}
    other = {k: v for k in CONDITION_ATTRIBUTES if (v := (lot.get("attributes") or {}).get(k))}
    if other:
        snapshot["other_condition_notes"] = other
    return snapshot


MAX_PHOTO_BYTES = 15 * 1024 * 1024


def download(url: str) -> bytes:
    if urlsplit(url).scheme != "https":
        raise ValueError(f"not an HTTPS URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "cars-ingest/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - provider's host
        data = response.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError("the photo is larger than 15 MB")
    return data


def shrink(data: bytes, max_px: int) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """The photo as a JPEG whose longest side is at most `max_px` (aspect ratio kept, never
    enlarged), with its original and new size. Raises when the bytes are not an image."""
    with Image.open(io.BytesIO(data)) as opened:
        img = ImageOps.exif_transpose(opened).convert("RGB")  # upright, as browsers show it
    original = img.size
    img.thumbnail((max_px, max_px))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue(), original, img.size


@dataclass
class Photo:
    position: int  # 1-based, in the provider's photo list
    image: LotImage
    jpeg: bytes  # as sent to the model
    original: tuple[int, int]
    size: tuple[int, int]


def load_photos(images: list[LotImage], max_px: int) -> tuple[list[Photo], list[dict]]:
    """Each photo in its largest size, shrunk; photos that cannot be had are reported."""
    photos, unavailable = [], []
    for position, image in enumerate(images, 1):
        url = image.hd or image.full or image.thumb or ""
        try:
            photos.append(Photo(position, image, *shrink(download(url), max_px)))
        except Exception as exc:  # noqa: BLE001 - one missing photo does not stop the analysis
            log.warning("analysis.photo_unavailable", url=url, error=str(exc))
            unavailable.append({"position": position, "url": url, "error": str(exc)[:300]})
    return photos, unavailable


# --- running a request ---------------------------------------------------------------------------

class AnalysisError(Exception):
    """The analysis cannot be made (no photos, unknown configuration, …)."""


def ask(settings: Settings, record: dict[str, Any], step: str, system: str,
        parts: list[ai.Part], schema: dict[str, Any]) -> ai.Reply:
    """`ai.ask`, keeping what the call sent under `request[step]` and its tokens and cost under
    `usage[step]`, also when it fails (an answer that came back unusable is billed all the
    same)."""
    try:
        reply = ai.ask(settings, system, parts, schema)
    except ai.AIError as exc:
        if exc.request:
            record["request"][step] = exc.request
        if exc.usage:
            record["usage"][step] = exc.usage
        raise
    record["request"][step], record["usage"][step] = reply.request, reply.usage
    return reply


def select_photos(settings: Settings, prompt: Prompt, lot: dict[str, Any], photos: list[Photo],
                  record: dict[str, Any]) -> list[Photo]:
    if len(photos) <= settings.ai_max_images:
        return photos
    ids = [f"photo_{n:02d}" for n in range(1, len(photos) + 1)]
    parts: list[ai.Part] = [
        f"The auction reports primary damage {lot.get('damage_primary') or 'unknown'} and "
        f"secondary damage {lot.get('damage_secondary') or 'none'}. {len(photos)} photos:"]
    for photo_id, photo in zip(ids, photos, strict=True):
        parts += [photo_id, shrink(photo.jpeg, SELECTION_PX)[0]]
    reply = ask(settings, record, "selection", prompt.select, parts,
                Selection.model_json_schema())
    record["selection"]["request_id"] = reply.request_id
    try:
        selection = Selection.model_validate(reply.data)
    except ValueError as exc:
        raise ai.AIError(f"invalid photo selection: {exc}", reply.raw, reply.request_id) from exc
    chosen = choose(selection, ids, settings.ai_max_images)
    record["selection"].update(photos=[p.model_dump() for p in selection.photos], chosen=chosen)
    return [photo for photo_id, photo in zip(ids, photos, strict=True) if photo_id in chosen]


def analyze(conn: Connection, settings: Settings, analysis: dict[str, Any],
            record: dict[str, Any]) -> dict:
    """The validated analysis; fills `record` with what was sent and received on the way."""
    settings = settings.model_copy(update={"ai_model": analysis["model"]})  # the one requested
    prompt = PROMPTS.get(analysis["prompt_version"])
    if prompt is None:
        raise AnalysisError(f"unknown prompt version {analysis['prompt_version']!r}")
    if analysis["schema_version"] not in SCHEMAS:
        raise AnalysisError(f"unknown schema version {analysis['schema_version']!r}")
    if analysis["language"] not in LANGUAGES:
        raise AnalysisError(f"unsupported language {analysis['language']!r}")
    lot = conn.execute("SELECT * FROM lots WHERE source = %s AND lot_number = %s",
                       (analysis["source"], analysis["lot_number"])).fetchone()
    record["input_snapshot"] = snapshot = metadata(lot)
    record["usage"] = {}
    # Everything that shapes the analysis besides the lot, to compare prompts and models later.
    record["request"] = {"settings": {
        "provider": settings.ai_provider, "model": settings.ai_model,
        "prompt_version": analysis["prompt_version"],
        "schema_version": analysis["schema_version"], "language": analysis["language"],
        "max_images": settings.ai_max_images, "image_max_px": settings.ai_image_max_px,
        "selection_px": SELECTION_PX, "damage_relevance": DAMAGE_RELEVANCE,
        "min_damage_photos": MIN_DAMAGE_PHOTOS, "max_overview_photos": MAX_OVERVIEW_PHOTOS}}
    provider = PROVIDERS.get(analysis["source"])
    listing = provider.fetch_images(lot) if provider and provider.fetch_images else None
    images = listing.images if listing else []
    photos, unavailable = load_photos(images, settings.ai_image_max_px)
    record["selection"] = {"listed": len(images), "listing_complete": bool(listing and
                           listing.complete), "unavailable": unavailable}
    if not photos:
        raise AnalysisError("the lot has no usable photos" if not images else
                            f"none of the lot's {len(images)} photos could be used")
    photos = select_photos(settings, prompt, lot, photos, record)

    parts: list[ai.Part] = [
        "Auction metadata (reported by the auction or the seller; may be incomplete or wrong):\n"
        + json.dumps(snapshot, indent=1, default=str),
        f"{len(photos)} photos, chosen among the {len(images)} of the listing:"]
    sent = []
    for n, photo in enumerate(photos, 1):
        image_id = f"image_{n:02d}"
        parts += [image_id, photo.jpeg]
        sent.append({"image_id": image_id, "position": photo.position, **asdict(photo.image),
                     "original_width": photo.original[0], "original_height": photo.original[1],
                     "width": photo.size[0], "height": photo.size[1], "bytes": len(photo.jpeg),
                     "sha256": hashlib.sha256(photo.jpeg).hexdigest()})
    record["images"], record["image_count"] = sent, len(sent)

    language = analysis["language"]
    system = prompt.analyze.format(language=LANGUAGES[language], language_code=language,
                                   schema_version=analysis["schema_version"])
    schema = SCHEMAS[analysis["schema_version"]]
    reply = ask(settings, record, "analysis", system, parts, schema.model_json_schema())
    record.update(raw_response=reply.raw, provider_request_id=reply.request_id)
    return validate(reply.data, analysis["schema_version"], {s["image_id"] for s in sent},
                    language)


# The columns an analysis's outcome fills (JSON ones apart).
OUTCOME = ("status", "input_snapshot", "images", "image_count", "selection", "result",
           "raw_response", "provider_request_id", "usage", "request", "error")
OUTCOME_JSON = ("input_snapshot", "images", "selection", "result", "usage", "request")


def run(conn: Connection, settings: Settings, analysis: dict[str, Any]) -> dict[str, Any]:
    """Make a claimed analysis and store its outcome, whatever happens."""
    record: dict[str, Any] = dict.fromkeys(OUTCOME)
    try:
        record["result"] = analyze(conn, settings, analysis, record)
        record["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the analysis
        log.warning("analysis.failed", id=analysis["id"], error=str(exc), exc_info=True)
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, ai.AIError):
            record["raw_response"] = exc.raw or record["raw_response"]
            record["provider_request_id"] = exc.request_id or record["provider_request_id"]
    values = {k: Jsonb(record[k]) if k in OUTCOME_JSON and record[k] is not None else record[k]
              for k in OUTCOME}
    row = conn.execute(
        f"""UPDATE vehicle_analyses SET {", ".join(f"{k} = %({k})s" for k in OUTCOME)},
                completed_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE id = %(id)s RETURNING id, source, lot_number, status""",
        {**values, "id": analysis["id"]},
    ).fetchone()
    db.notify(conn, "analysis", **row)
    log.info("analysis.done", **row)
    return row


def claim(conn: Connection, settings: Settings) -> dict[str, Any] | None:
    """Take the oldest queued analysis and stamp it with what will run it: the model its
    request chose, else AI_MODEL."""
    row = conn.execute(
        """UPDATE vehicle_analyses
           SET status = 'processing', provider = %s, model = coalesce(model, %s),
               prompt_version = %s,
               schema_version = %s, started_at = clock_timestamp(), updated_at = clock_timestamp()
           WHERE id = (SELECT id FROM vehicle_analyses WHERE status = 'queued'
                       ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
           RETURNING *""",
        (settings.ai_provider, settings.ai_model, settings.ai_prompt_version,
         settings.ai_schema_version),
    ).fetchone()
    if row is not None:
        db.notify(conn, "analysis", id=row["id"], source=row["source"],
                  lot_number=row["lot_number"], status=row["status"])
    return row


def process_next(conn: Connection, settings: Settings) -> bool:
    """Run the oldest queued analysis, if any; returns whether there was one."""
    analysis = claim(conn, settings)
    if analysis is not None:
        run(conn, settings, analysis)
    return analysis is not None


def fail_interrupted(conn: Connection) -> int:
    """Analyses left running by a stopped worker (only one runs them) never finish."""
    return conn.execute(
        """UPDATE vehicle_analyses SET status = 'failed',
               error = 'interrupted: the worker stopped during the analysis',
               completed_at = clock_timestamp(), updated_at = clock_timestamp()
           WHERE status = 'processing'""").rowcount
