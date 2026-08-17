"""Template schema.

Templates are validated on every load. A template file is untrusted input the
moment someone shares one by email, so it is parsed with json only and checked
against this schema before any part of it reaches the extraction engine.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1

FieldKind = Literal["single", "table", "repeated", "header", "footer"]
DataType = Literal["string", "decimal", "date", "int"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class VendorIdentifiers(_Base):
    gstin: str | None = None
    pan: str | None = None
    vat: str | None = None
    cin: str | None = None


class Vendor(_Base):
    display_name: str = ""
    slug: str = ""
    aliases: list[str] = Field(default_factory=list)
    identifiers: VendorIdentifiers = Field(default_factory=VendorIdentifiers)


class SearchScope(_Base):
    page: Any = "any"
    region_norm: list[float] | None = None


class Anchor(_Base):
    text: str
    match: Literal["exact", "fuzzy", "contains"] = "fuzzy"
    min_ratio: float = 0.85
    occurrence: Literal["first", "last", "all"] = "first"
    search_scope: SearchScope = Field(default_factory=SearchScope)


class ValueWindow(_Base):
    direction: Literal["right", "left", "below", "above", "same_line"] = "right"
    max_distance_pt: float = 220.0
    vertical_tolerance_pt: float = 6.0
    max_below_pt: float = 40.0
    stop_at_anchor: list[str] = Field(default_factory=list)


class Strategy(_Base):
    type: str
    anchor: Anchor | None = None
    value_window: ValueWindow | None = None
    pattern: str | None = None
    ignore_case: bool = False
    page: Any = "any"
    bbox_norm: list[float] | None = None
    tolerance_pt: float = 8.0
    label: str | None = None
    value: Any = None
    post: list[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def known_type(cls, v: str) -> str:
        allowed = {"anchor_relative", "regex_page", "region_absolute",
                   "label_pair", "constant", "computed", "table_cell"}
        if v not in allowed:
            raise ValueError(f"unknown strategy type: {v}")
        return v


class Validator(_Base):
    type: str
    pattern: str | None = None
    algorithm: str | None = None
    min: Any = None
    max: Any = None
    values: list[Any] = Field(default_factory=list)
    ignore_case: bool = False


class FieldDef(_Base):
    name: str
    label: str = ""
    kind: FieldKind = "single"
    data_type: DataType = "string"
    required: bool = False
    unique_key: bool = False
    strategies: list[Strategy] = Field(default_factory=list)
    validators: list[Validator] = Field(default_factory=list)
    on_ambiguous: Literal["review", "first", "fail"] = "review"
    confidence_floor: float | None = None

    @field_validator("name")
    @classmethod
    def identifier_like(cls, v: str) -> str:
        if not v or not v.replace("_", "").isalnum():
            raise ValueError(f"field name must be alphanumeric/underscore: {v!r}")
        return v


class ColumnDef(_Base):
    name: str
    x_range_norm: list[float] | None = None
    type: DataType = "string"
    required: bool = False
    multiline: bool = False
    align: Literal["left", "right", "center"] = "left"
    pattern: str | None = None


class TableStart(_Base):
    type: str = "header_row"
    header_tokens: list[str] = Field(default_factory=list)
    min_tokens_matched: int = 3
    match: Literal["exact", "fuzzy"] = "fuzzy"


class TableEnd(_Base):
    type: str = "any_of"
    conditions: list[dict] = Field(default_factory=list)


class RowGrouping(_Base):
    strategy: str = "anchor_column"
    anchor_columns: list[str] = Field(default_factory=list)
    y_tolerance_pt: float = 3.0
    wrapped_lines: str = "append_to_previous"


class MultipageSpec(_Base):
    continues: bool = True
    repeat_header: bool = True
    skip_rows_matching: list[str] = Field(default_factory=list)


class RowFilters(_Base):
    drop_if_matches: list[str] = Field(default_factory=list)
    drop_if_all_numeric_columns_empty: bool = True


class TableDef(_Base):
    name: str = "line_items"
    start: TableStart = Field(default_factory=TableStart)
    end: TableEnd = Field(default_factory=TableEnd)
    columns: list[ColumnDef] = Field(default_factory=list)
    column_detection: str = "rulings_then_gaps"
    row_grouping: RowGrouping = Field(default_factory=RowGrouping)
    multipage: MultipageSpec = Field(default_factory=MultipageSpec)
    row_filters: RowFilters = Field(default_factory=RowFilters)
    validation: dict = Field(default_factory=dict)


class IdentifierRegex(_Base):
    field: str
    pattern: str
    weight: float = 0.4


class LayoutSignature(_Base):
    grid: list[int] = Field(default_factory=lambda: [8, 8])
    vector: list[float] = Field(default_factory=list)
    weight: float = 0.05


class TextProfile(_Base):
    top_terms: dict[str, float] = Field(default_factory=dict)
    weight: float = 0.10


class Fingerprint(_Base):
    required_tokens: list[str] = Field(default_factory=list)
    forbidden_tokens: list[str] = Field(default_factory=list)
    identifier_regexes: list[IdentifierRegex] = Field(default_factory=list)
    layout_signature: LayoutSignature = Field(default_factory=LayoutSignature)
    text_profile: TextProfile = Field(default_factory=TextProfile)
    min_score: float = 0.72
    review_below: float = 0.85


class CrossCheck(_Base):
    id: str
    expr: str
    on_fail: Literal["review", "warn"] = "review"
    requires: list[str] = Field(default_factory=list)


class OutputSpec(_Base):
    sheet_group: str = "documents"
    column_order: list[str] = Field(default_factory=list)


class PageGeometry(_Base):
    width_pt: float = 595.3
    height_pt: float = 841.9
    label: str = "A4"


class Template(_Base):
    schema_version: int = SCHEMA_VERSION
    template_id: str
    version: int = 1
    created_utc: str = ""
    modified_utc: str = ""
    vendor: Vendor = Field(default_factory=Vendor)
    document_type: str = "invoice"
    page_geometry: PageGeometry = Field(default_factory=PageGeometry)
    date_formats: list[str] = Field(default_factory=list)
    dayfirst: bool = True
    fingerprint: Fingerprint = Field(default_factory=Fingerprint)
    fields: list[FieldDef] = Field(default_factory=list)
    tables: list[TableDef] = Field(default_factory=list)
    cross_checks: list[CrossCheck] = Field(default_factory=list)
    output: OutputSpec = Field(default_factory=OutputSpec)

    @field_validator("template_id")
    @classmethod
    def id_safe(cls, v: str) -> str:
        if not v or any(c in v for c in "/\\:*?\"<>| "):
            raise ValueError(f"unsafe template_id: {v!r}")
        return v

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


def migrate(raw: dict) -> dict:
    """Bring an older template file up to the current schema version."""
    version = int(raw.get("schema_version", 0))
    if version == 0:
        raw["schema_version"] = 1
    return raw


def validate(raw: dict) -> Template:
    return Template.model_validate(migrate(dict(raw)))
