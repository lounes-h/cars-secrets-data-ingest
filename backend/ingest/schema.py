"""Provider-neutral typed columns of the `lots` table.

Every provider maps its own CSV headers onto (a subset of) these columns. Whatever a provider
exports is also kept verbatim in `lots.attributes`, so nothing is lost when a column is not
mapped. Adding a column here requires a migration; adding a provider does not.
"""

# Order matters: it is the column order of the staging table and of COPY.
CANONICAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("yard_code", "text"),
    ("yard_name", "text"),
    ("sale_date", "date"),
    ("sale_at", "timestamptz"),
    ("vehicle_type", "text"),
    ("year", "integer"),
    ("make", "text"),
    ("model_group", "text"),
    ("model_detail", "text"),
    ("trim", "text"),
    ("body_style", "text"),
    ("color", "text"),
    ("damage_primary", "text"),
    ("damage_secondary", "text"),
    ("title_state", "text"),
    ("title_type", "text"),
    ("has_keys", "text"),
    ("vin", "text"),
    ("odometer", "numeric"),
    ("odometer_brand", "text"),
    ("est_retail_value", "numeric"),
    ("repair_cost", "numeric"),
    ("engine", "text"),
    ("drive", "text"),
    ("transmission", "text"),
    ("fuel_type", "text"),
    ("cylinders", "text"),
    ("runs_drives", "text"),
    ("sale_status", "text"),
    ("high_bid", "numeric"),
    ("buy_it_now_price", "numeric"),
    ("currency", "text"),
    ("location_city", "text"),
    ("location_state", "text"),
    ("location_zip", "text"),
    ("location_country", "text"),
    ("seller_name", "text"),
    ("image_url", "text"),
    ("thumbnail_url", "text"),
    ("source_created_at", "timestamp"),
    ("source_updated_at", "timestamptz"),
)

COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in CANONICAL_COLUMNS)
COLUMN_INDEX: dict[str, int] = {name: i for i, name in enumerate(COLUMN_NAMES)}
