"""
Renderer-independent report presentation schema.

This module defines a strictly-validated, structured-data-only description of
*how* a report should be composed (cover usage, section ordering, layout
variants, content density, emphasis, block types such as metrics/tables/
charts/timelines/comparisons/risk-matrices/evidence callouts, and
source/appendix placement).

Design goals
------------
1. Renderer independence: nothing here knows about HTML, PDF, email, or any
   specific frontend component. Any renderer (HTML report, PDF, PPTX, email,
   frontend widget) can consume a ``ReportPresentationSpec`` and decide how to
   paint it.
2. Structured data only: no field ever accepts arbitrary HTML/CSS/JS/markup.
   Free-text fields are sanitized/validated to reject markup-looking content.
3. Strict validation with safe defaults: malformed or missing data never
   crashes the pipeline. ``ReportPresentationSpec.from_llm_output`` always
   returns a usable spec (falling back to sane defaults and collecting
   human-readable warnings). ``ReportPresentationSpec.validate_strict`` is
   available when callers *want* a hard failure (e.g. in tests/CI).
4. Domain flexibility: the same schema can express substantially different
   compositions for financial, regulatory, scientific, company-analysis,
   market/news, and comparison reports, purely by choosing different
   sections/blocks/layouts -- no schema changes needed per domain.

This module is intentionally standalone (no third-party dependencies, no
imports from the rest of the codebase) so it can be adopted incrementally.
It is NOT wired into the report planner or any renderer yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaValidationError(ValueError):
    """Raised when a spec fails strict validation."""

    def __init__(self, issues: List[str]):
        self.issues = issues
        super().__init__("Invalid ReportPresentationSpec:\n- " + "\n- ".join(issues))


# ---------------------------------------------------------------------------
# Markup / safety guard
# ---------------------------------------------------------------------------

# Anything that looks like an HTML/XML tag, an event handler, inline script,
# style block, or a markdown-embedded <script> is rejected from free-text
# fields. This keeps the schema strictly "structured data" -- renderers are
# responsible for escaping/painting, the LLM only supplies plain content.
_MARKUP_PATTERN = re.compile(
    r"<\s*/?\s*[a-zA-Z][^>]*>"          # <tag ...> or </tag>
    r"|javascript\s*:"                    # javascript: URIs
    r"|on\w+\s*=\s*['\"]"                 # onclick="...", onerror='...'
    r"|\{\{.*?\}\}"                       # template injection e.g. {{ x }}
    r"|\$\{.*?\}",                        # template injection e.g. ${x}
    re.IGNORECASE | re.DOTALL,
)

MAX_TEXT_LEN = 20000
MAX_SHORT_TEXT_LEN = 500


def contains_markup(text: str) -> bool:
    """Return True if the text looks like it contains HTML/CSS/JS markup."""
    if not isinstance(text, str):
        return False
    return bool(_MARKUP_PATTERN.search(text))


def clean_text(
    value: Any,
    *,
    default: str = "",
    max_len: int = MAX_TEXT_LEN,
    warnings: Optional[List[str]] = None,
    field_name: str = "text",
) -> str:
    """Coerce a value to a safe plain-text string.

    - Non-strings are stringified (or replaced with default if that fails).
    - Any value that contains markup-like content is stripped of the
      offending fragments; if nothing safe remains, falls back to default.
    - Length is capped to max_len.
    """
    if value is None:
        return default
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            if warnings is not None:
                warnings.append(f"{field_name}: could not coerce to string, using default")
            return default

    if contains_markup(value):
        cleaned = _MARKUP_PATTERN.sub(" ", value).strip()
        if warnings is not None:
            warnings.append(f"{field_name}: markup detected and stripped")
        value = cleaned

    value = value.strip()
    if len(value) > max_len:
        value = value[:max_len].rstrip()
        if warnings is not None:
            warnings.append(f"{field_name}: truncated to {max_len} characters")

    return value or default


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ReportDomain(str, Enum):
    FINANCIAL = "financial"
    REGULATORY = "regulatory"
    SCIENTIFIC = "scientific"
    COMPANY_ANALYSIS = "company_analysis"
    MARKET_NEWS = "market_news"
    COMPARISON = "comparison"
    GENERIC = "generic"


class TitleTreatment(str, Enum):
    """How the report title/subtitle block should be visually treated."""

    MINIMAL = "minimal"          # small title, no imagery
    BOLD_BANNER = "bold_banner"  # large banner-style title
    CLASSIC = "classic"          # centered classic report title page
    DATA_DRIVEN = "data_driven"  # title alongside a hero metric/stat


class ExecutiveSummaryPlacement(str, Enum):
    NONE = "none"
    AFTER_COVER = "after_cover"
    TOP_OF_BODY = "top_of_body"
    SIDEBAR = "sidebar"
    END_SUMMARY = "end_summary"


class SectionType(str, Enum):
    EXECUTIVE_SUMMARY = "executive_summary"
    OVERVIEW = "overview"
    METRICS_DASHBOARD = "metrics_dashboard"
    NARRATIVE = "narrative"
    FINANCIALS = "financials"
    COMPLIANCE = "compliance"
    METHODOLOGY = "methodology"
    FINDINGS = "findings"
    TIMELINE = "timeline"
    COMPARISON = "comparison"
    RISK_ASSESSMENT = "risk_assessment"
    MARKET_CONTEXT = "market_context"
    NEWS_DIGEST = "news_digest"
    RECOMMENDATIONS = "recommendations"
    APPENDIX = "appendix"
    SOURCES = "sources"
    CUSTOM = "custom"


class LayoutVariant(str, Enum):
    SINGLE_COLUMN = "single_column"
    TWO_COLUMN = "two_column"
    GRID = "grid"
    FULL_BLEED = "full_bleed"
    SIDEBAR_MAIN = "sidebar_main"


class ContentDensity(str, Enum):
    SPARSE = "sparse"
    STANDARD = "standard"
    DENSE = "dense"


class EmphasisLevel(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class SourcePlacement(str, Enum):
    INLINE_FOOTNOTES = "inline_footnotes"
    END_OF_SECTION = "end_of_section"
    END_OF_REPORT = "end_of_report"
    APPENDIX = "appendix"
    NONE = "none"


class TrendDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    SEVERE = "severe"


class EvidenceTone(str, Enum):
    INFO = "info"
    POSITIVE = "positive"
    WARNING = "warning"
    NEGATIVE = "negative"
    QUOTE = "quote"


# ---------------------------------------------------------------------------
# Helpers for enum coercion with safe defaults
# ---------------------------------------------------------------------------


def _coerce_enum(
    enum_cls,
    value: Any,
    default,
    warnings: List[str],
    field_name: str,
):
    if isinstance(value, enum_cls):
        return value
    if value is None:
        return default
    try:
        return enum_cls(str(value).strip().lower())
    except Exception:
        warnings.append(
            f"{field_name}: invalid value {value!r} for {enum_cls.__name__}, "
            f"defaulting to {default.value if hasattr(default, 'value') else default!r}"
        )
        return default


def _coerce_float(value: Any, default: Optional[float], warnings: List[str], field_name: str) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        warnings.append(f"{field_name}: invalid numeric value {value!r}, using default")
        return default


def _coerce_bool(value: Any, default: bool, warnings: List[str], field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "y"):
            return True
        if low in ("false", "0", "no", "n"):
            return False
    warnings.append(f"{field_name}: invalid boolean value {value!r}, defaulting to {default}")
    return default


def _coerce_list_of_str(value: Any, warnings: List[str], field_name: str, max_items: int = 100) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        warnings.append(f"{field_name}: expected a list, got {type(value).__name__}; ignoring")
        return []
    out = []
    for i, item in enumerate(value[:max_items]):
        out.append(clean_text(item, warnings=warnings, field_name=f"{field_name}[{i}]", max_len=MAX_SHORT_TEXT_LEN))
    return [o for o in out if o]


# ---------------------------------------------------------------------------
# Content blocks (structured data only)
# ---------------------------------------------------------------------------


@dataclass
class MetricItem:
    """A single KPI/metric value, e.g. Revenue: $1.2B, +4% YoY."""

    label: str = ""
    value: str = ""
    unit: str = ""
    trend: TrendDirection = TrendDirection.UNKNOWN
    change_pct: Optional[float] = None
    emphasis: EmphasisLevel = EmphasisLevel.NORMAL

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "MetricItem":
        data = data or {}
        return MetricItem(
            label=clean_text(data.get("label"), warnings=warnings, field_name="metric.label", max_len=MAX_SHORT_TEXT_LEN),
            value=clean_text(data.get("value"), warnings=warnings, field_name="metric.value", max_len=MAX_SHORT_TEXT_LEN),
            unit=clean_text(data.get("unit"), warnings=warnings, field_name="metric.unit", max_len=50),
            trend=_coerce_enum(TrendDirection, data.get("trend"), TrendDirection.UNKNOWN, warnings, "metric.trend"),
            change_pct=_coerce_float(data.get("change_pct"), None, warnings, "metric.change_pct"),
            emphasis=_coerce_enum(EmphasisLevel, data.get("emphasis"), EmphasisLevel.NORMAL, warnings, "metric.emphasis"),
        )


@dataclass
class MetricsBlock:
    kind: str = "metrics"
    title: str = ""
    items: List[MetricItem] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "MetricsBlock":
        data = data or {}
        items_raw = data.get("items") or []
        if not isinstance(items_raw, list):
            warnings.append("metrics.items: expected a list; ignoring")
            items_raw = []
        items = [MetricItem.from_dict(it, warnings) for it in items_raw[:50] if isinstance(it, dict)]
        return MetricsBlock(
            title=clean_text(data.get("title"), warnings=warnings, field_name="metrics.title", max_len=MAX_SHORT_TEXT_LEN),
            items=items,
        )


@dataclass
class ProseBlock:
    kind: str = "prose"
    heading: str = ""
    body: str = ""
    emphasis: EmphasisLevel = EmphasisLevel.NORMAL

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "ProseBlock":
        data = data or {}
        return ProseBlock(
            heading=clean_text(data.get("heading"), warnings=warnings, field_name="prose.heading", max_len=MAX_SHORT_TEXT_LEN),
            body=clean_text(data.get("body"), warnings=warnings, field_name="prose.body"),
            emphasis=_coerce_enum(EmphasisLevel, data.get("emphasis"), EmphasisLevel.NORMAL, warnings, "prose.emphasis"),
        )


@dataclass
class BulletsBlock:
    kind: str = "bullets"
    title: str = ""
    items: List[str] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "BulletsBlock":
        data = data or {}
        return BulletsBlock(
            title=clean_text(data.get("title"), warnings=warnings, field_name="bullets.title", max_len=MAX_SHORT_TEXT_LEN),
            items=_coerce_list_of_str(data.get("items"), warnings, "bullets.items", max_items=100),
        )


@dataclass
class TableBlock:
    kind: str = "table"
    title: str = ""
    columns: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "TableBlock":
        data = data or {}
        columns = _coerce_list_of_str(data.get("columns"), warnings, "table.columns", max_items=30)
        raw_rows = data.get("rows") or []
        rows: List[List[str]] = []
        if isinstance(raw_rows, list):
            for i, row in enumerate(raw_rows[:500]):
                if isinstance(row, list):
                    rows.append(_coerce_list_of_str(row, warnings, f"table.rows[{i}]", max_items=30))
                else:
                    warnings.append(f"table.rows[{i}]: expected a list of cells; skipping row")
        else:
            warnings.append("table.rows: expected a list; ignoring")
        return TableBlock(
            title=clean_text(data.get("title"), warnings=warnings, field_name="table.title", max_len=MAX_SHORT_TEXT_LEN),
            columns=columns,
            rows=rows,
        )


@dataclass
class ChartSeries:
    name: str = ""
    values: List[float] = field(default_factory=list)


@dataclass
class ChartBlock:
    kind: str = "chart"
    title: str = ""
    chart_type: str = "line"  # line | bar | pie | area | scatter
    x_labels: List[str] = field(default_factory=list)
    series: List[ChartSeries] = field(default_factory=list)

    _ALLOWED_TYPES = {"line", "bar", "pie", "area", "scatter"}

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "ChartBlock":
        data = data or {}
        chart_type = clean_text(data.get("chart_type"), default="line", warnings=warnings, field_name="chart.chart_type", max_len=20).lower()
        if chart_type not in ChartBlock._ALLOWED_TYPES:
            warnings.append(f"chart.chart_type: invalid value {chart_type!r}, defaulting to 'line'")
            chart_type = "line"
        x_labels = _coerce_list_of_str(data.get("x_labels"), warnings, "chart.x_labels", max_items=200)
        series_raw = data.get("series") or []
        series: List[ChartSeries] = []
        if isinstance(series_raw, list):
            for i, s in enumerate(series_raw[:20]):
                if not isinstance(s, dict):
                    warnings.append(f"chart.series[{i}]: expected an object; skipping")
                    continue
                name = clean_text(s.get("name"), warnings=warnings, field_name=f"chart.series[{i}].name", max_len=MAX_SHORT_TEXT_LEN)
                values_raw = s.get("values") or []
                values: List[float] = []
                if isinstance(values_raw, list):
                    for v in values_raw[:2000]:
                        fv = _coerce_float(v, None, warnings, f"chart.series[{i}].values")
                        if fv is not None:
                            values.append(fv)
                series.append(ChartSeries(name=name, values=values))
        else:
            warnings.append("chart.series: expected a list; ignoring")
        return ChartBlock(
            title=clean_text(data.get("title"), warnings=warnings, field_name="chart.title", max_len=MAX_SHORT_TEXT_LEN),
            chart_type=chart_type,
            x_labels=x_labels,
            series=series,
        )


@dataclass
class TimelineEvent:
    date_label: str = ""
    title: str = ""
    description: str = ""
    emphasis: EmphasisLevel = EmphasisLevel.NORMAL

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "TimelineEvent":
        data = data or {}
        return TimelineEvent(
            date_label=clean_text(data.get("date_label"), warnings=warnings, field_name="timeline_event.date_label", max_len=50),
            title=clean_text(data.get("title"), warnings=warnings, field_name="timeline_event.title", max_len=MAX_SHORT_TEXT_LEN),
            description=clean_text(data.get("description"), warnings=warnings, field_name="timeline_event.description"),
            emphasis=_coerce_enum(EmphasisLevel, data.get("emphasis"), EmphasisLevel.NORMAL, warnings, "timeline_event.emphasis"),
        )


@dataclass
class TimelineBlock:
    kind: str = "timeline"
    title: str = ""
    events: List[TimelineEvent] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "TimelineBlock":
        data = data or {}
        events_raw = data.get("events") or []
        events = []
        if isinstance(events_raw, list):
            events = [TimelineEvent.from_dict(e, warnings) for e in events_raw[:200] if isinstance(e, dict)]
        else:
            warnings.append("timeline.events: expected a list; ignoring")
        return TimelineBlock(
            title=clean_text(data.get("title"), warnings=warnings, field_name="timeline.title", max_len=MAX_SHORT_TEXT_LEN),
            events=events,
        )


@dataclass
class ComparisonItem:
    name: str = ""
    values: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "ComparisonItem":
        data = data or {}
        raw_values = data.get("values") or {}
        values: Dict[str, str] = {}
        if isinstance(raw_values, dict):
            for k, v in list(raw_values.items())[:50]:
                key = clean_text(k, warnings=warnings, field_name="comparison.values.key", max_len=100)
                val = clean_text(v, warnings=warnings, field_name="comparison.values.value", max_len=MAX_SHORT_TEXT_LEN)
                if key:
                    values[key] = val
        else:
            warnings.append("comparison_item.values: expected an object; ignoring")
        return ComparisonItem(
            name=clean_text(data.get("name"), warnings=warnings, field_name="comparison_item.name", max_len=MAX_SHORT_TEXT_LEN),
            values=values,
        )


@dataclass
class ComparisonBlock:
    kind: str = "comparison"
    title: str = ""
    dimensions: List[str] = field(default_factory=list)
    items: List[ComparisonItem] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "ComparisonBlock":
        data = data or {}
        dimensions = _coerce_list_of_str(data.get("dimensions"), warnings, "comparison.dimensions", max_items=30)
        items_raw = data.get("items") or []
        items = []
        if isinstance(items_raw, list):
            items = [ComparisonItem.from_dict(it, warnings) for it in items_raw[:50] if isinstance(it, dict)]
        else:
            warnings.append("comparison.items: expected a list; ignoring")
        return ComparisonBlock(
            title=clean_text(data.get("title"), warnings=warnings, field_name="comparison.title", max_len=MAX_SHORT_TEXT_LEN),
            dimensions=dimensions,
            items=items,
        )


@dataclass
class RiskItem:
    name: str = ""
    likelihood: RiskLevel = RiskLevel.MEDIUM
    impact: RiskLevel = RiskLevel.MEDIUM
    mitigation: str = ""

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "RiskItem":
        data = data or {}
        return RiskItem(
            name=clean_text(data.get("name"), warnings=warnings, field_name="risk.name", max_len=MAX_SHORT_TEXT_LEN),
            likelihood=_coerce_enum(RiskLevel, data.get("likelihood"), RiskLevel.MEDIUM, warnings, "risk.likelihood"),
            impact=_coerce_enum(RiskLevel, data.get("impact"), RiskLevel.MEDIUM, warnings, "risk.impact"),
            mitigation=clean_text(data.get("mitigation"), warnings=warnings, field_name="risk.mitigation"),
        )


@dataclass
class RiskMatrixBlock:
    kind: str = "risk_matrix"
    title: str = ""
    risks: List[RiskItem] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "RiskMatrixBlock":
        data = data or {}
        risks_raw = data.get("risks") or []
        risks = []
        if isinstance(risks_raw, list):
            risks = [RiskItem.from_dict(r, warnings) for r in risks_raw[:200] if isinstance(r, dict)]
        else:
            warnings.append("risk_matrix.risks: expected a list; ignoring")
        return RiskMatrixBlock(
            title=clean_text(data.get("title"), warnings=warnings, field_name="risk_matrix.title", max_len=MAX_SHORT_TEXT_LEN),
            risks=risks,
        )


@dataclass
class EvidenceBlock:
    """A callout / evidence box, e.g. a highlighted quote, warning, or citation."""

    kind: str = "evidence"
    tone: EvidenceTone = EvidenceTone.INFO
    heading: str = ""
    body: str = ""
    source_label: str = ""

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str]) -> "EvidenceBlock":
        data = data or {}
        return EvidenceBlock(
            tone=_coerce_enum(EvidenceTone, data.get("tone"), EvidenceTone.INFO, warnings, "evidence.tone"),
            heading=clean_text(data.get("heading"), warnings=warnings, field_name="evidence.heading", max_len=MAX_SHORT_TEXT_LEN),
            body=clean_text(data.get("body"), warnings=warnings, field_name="evidence.body"),
            source_label=clean_text(data.get("source_label"), warnings=warnings, field_name="evidence.source_label", max_len=MAX_SHORT_TEXT_LEN),
        )


# Discriminated union of all content block types.
ContentBlock = Union[
    MetricsBlock,
    BulletsBlock,
    ProseBlock,
    TableBlock,
    ChartBlock,
    TimelineBlock,
    ComparisonBlock,
    RiskMatrixBlock,
    EvidenceBlock,
]

_BLOCK_KIND_MAP = {
    "metrics": MetricsBlock,
    "bullets": BulletsBlock,
    "prose": ProseBlock,
    "table": TableBlock,
    "chart": ChartBlock,
    "timeline": TimelineBlock,
    "comparison": ComparisonBlock,
    "risk_matrix": RiskMatrixBlock,
    "evidence": EvidenceBlock,
}


def _block_from_dict(data: Any, warnings: List[str]) -> Optional[ContentBlock]:
    if not isinstance(data, dict):
        warnings.append(f"content block: expected an object, got {type(data).__name__}; skipping")
        return None
    kind = str(data.get("kind", "")).strip().lower()
    cls = _BLOCK_KIND_MAP.get(kind)
    if cls is None:
        warnings.append(f"content block: unknown kind {kind!r}; skipping")
        return None
    return cls.from_dict(data, warnings)


def block_to_dict(block: ContentBlock) -> Dict[str, Any]:
    """Serialize a content block back to a plain dict (JSON-safe)."""
    return _dataclass_to_jsonable(block)


def _dataclass_to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_dataclass_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dataclass_to_jsonable(v) for k, v in asdict(obj).items()} if False else {
            k: _dataclass_to_jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__
        }
    return obj


# ---------------------------------------------------------------------------
# Section / Cover / Executive Summary / Sources
# ---------------------------------------------------------------------------


@dataclass
class CoverSpec:
    enabled: bool = True
    title: str = ""
    subtitle: str = ""
    treatment: TitleTreatment = TitleTreatment.CLASSIC
    show_date: bool = True
    show_author: bool = False
    author: str = ""

    @staticmethod
    def from_dict(data: Optional[Dict[str, Any]], warnings: List[str]) -> "CoverSpec":
        data = data or {}
        return CoverSpec(
            enabled=_coerce_bool(data.get("enabled"), True, warnings, "cover.enabled"),
            title=clean_text(data.get("title"), warnings=warnings, field_name="cover.title", max_len=MAX_SHORT_TEXT_LEN),
            subtitle=clean_text(data.get("subtitle"), warnings=warnings, field_name="cover.subtitle", max_len=MAX_SHORT_TEXT_LEN),
            treatment=_coerce_enum(TitleTreatment, data.get("treatment"), TitleTreatment.CLASSIC, warnings, "cover.treatment"),
            show_date=_coerce_bool(data.get("show_date"), True, warnings, "cover.show_date"),
            show_author=_coerce_bool(data.get("show_author"), False, warnings, "cover.show_author"),
            author=clean_text(data.get("author"), warnings=warnings, field_name="cover.author", max_len=MAX_SHORT_TEXT_LEN),
        )


@dataclass
class ExecutiveSummarySpec:
    placement: ExecutiveSummaryPlacement = ExecutiveSummaryPlacement.AFTER_COVER
    heading: str = "Executive Summary"
    body: str = ""
    key_metrics: List[MetricItem] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Optional[Dict[str, Any]], warnings: List[str]) -> "ExecutiveSummarySpec":
        data = data or {}
        metrics_raw = data.get("key_metrics") or []
        metrics = []
        if isinstance(metrics_raw, list):
            metrics = [MetricItem.from_dict(m, warnings) for m in metrics_raw[:20] if isinstance(m, dict)]
        else:
            warnings.append("executive_summary.key_metrics: expected a list; ignoring")
        return ExecutiveSummarySpec(
            placement=_coerce_enum(
                ExecutiveSummaryPlacement,
                data.get("placement"),
                ExecutiveSummaryPlacement.AFTER_COVER,
                warnings,
                "executive_summary.placement",
            ),
            heading=clean_text(
                data.get("heading"), default="Executive Summary", warnings=warnings,
                field_name="executive_summary.heading", max_len=MAX_SHORT_TEXT_LEN,
            ),
            body=clean_text(data.get("body"), warnings=warnings, field_name="executive_summary.body"),
            key_metrics=metrics,
        )


@dataclass
class SourceAppendixSpec:
    placement: SourcePlacement = SourcePlacement.END_OF_REPORT
    group_by_section: bool = False
    include_appendix: bool = False

    @staticmethod
    def from_dict(data: Optional[Dict[str, Any]], warnings: List[str]) -> "SourceAppendixSpec":
        data = data or {}
        return SourceAppendixSpec(
            placement=_coerce_enum(
                SourcePlacement, data.get("placement"), SourcePlacement.END_OF_REPORT, warnings, "sources.placement"
            ),
            group_by_section=_coerce_bool(data.get("group_by_section"), False, warnings, "sources.group_by_section"),
            include_appendix=_coerce_bool(data.get("include_appendix"), False, warnings, "sources.include_appendix"),
        )


@dataclass
class ReportSection:
    id: str = ""
    title: str = ""
    section_type: SectionType = SectionType.CUSTOM
    layout: LayoutVariant = LayoutVariant.SINGLE_COLUMN
    density: ContentDensity = ContentDensity.STANDARD
    emphasis: EmphasisLevel = EmphasisLevel.NORMAL
    order: int = 0
    blocks: List[ContentBlock] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any], warnings: List[str], fallback_order: int = 0) -> "ReportSection":
        data = data or {}
        blocks_raw = data.get("blocks") or []
        blocks: List[ContentBlock] = []
        if isinstance(blocks_raw, list):
            for b in blocks_raw[:100]:
                parsed = _block_from_dict(b, warnings)
                if parsed is not None:
                    blocks.append(parsed)
        else:
            warnings.append("section.blocks: expected a list; ignoring")

        sec_id = clean_text(data.get("id"), warnings=warnings, field_name="section.id", max_len=100)
        title = clean_text(data.get("title"), warnings=warnings, field_name="section.title", max_len=MAX_SHORT_TEXT_LEN)
        if not sec_id:
            sec_id = re.sub(r"[^a-z0-9_-]+", "-", title.lower()).strip("-") or f"section-{fallback_order}"

        order = data.get("order", fallback_order)
        try:
            order = int(order)
        except Exception:
            warnings.append(f"section.order: invalid value {order!r}, defaulting to {fallback_order}")
            order = fallback_order

        return ReportSection(
            id=sec_id,
            title=title,
            section_type=_coerce_enum(SectionType, data.get("section_type"), SectionType.CUSTOM, warnings, "section.section_type"),
            layout=_coerce_enum(LayoutVariant, data.get("layout"), LayoutVariant.SINGLE_COLUMN, warnings, "section.layout"),
            density=_coerce_enum(ContentDensity, data.get("density"), ContentDensity.STANDARD, warnings, "section.density"),
            emphasis=_coerce_enum(EmphasisLevel, data.get("emphasis"), EmphasisLevel.NORMAL, warnings, "section.emphasis"),
            order=order,
            blocks=blocks,
        )


# ---------------------------------------------------------------------------
# Top-level spec
# ---------------------------------------------------------------------------


@dataclass
class ReportPresentationSpec:
    """Renderer-independent description of how a report should be composed."""

    domain: ReportDomain = ReportDomain.GENERIC
    cover: CoverSpec = field(default_factory=CoverSpec)
    executive_summary: ExecutiveSummarySpec = field(default_factory=ExecutiveSummarySpec)
    sections: List[ReportSection] = field(default_factory=list)
    source_appendix: SourceAppendixSpec = field(default_factory=SourceAppendixSpec)
    default_layout: LayoutVariant = LayoutVariant.SINGLE_COLUMN
    default_density: ContentDensity = ContentDensity.STANDARD

    # -- Construction -------------------------------------------------

    @staticmethod
    def from_llm_output(data: Any) -> Tuple["ReportPresentationSpec", List[str]]:
        """Lenient constructor: never raises. Returns (spec, warnings).

        Any missing or invalid field falls back to a safe default and a
        human-readable warning is recorded, rather than failing the whole
        report generation pipeline over a single bad field.
        """
        warnings: List[str] = []

        if not isinstance(data, dict):
            warnings.append("root: expected an object; using an entirely default spec")
            data = {}

        domain = _coerce_enum(ReportDomain, data.get("domain"), ReportDomain.GENERIC, warnings, "domain")
        cover = CoverSpec.from_dict(data.get("cover"), warnings)
        executive_summary = ExecutiveSummarySpec.from_dict(data.get("executive_summary"), warnings)
        source_appendix = SourceAppendixSpec.from_dict(data.get("source_appendix"), warnings)
        default_layout = _coerce_enum(
            LayoutVariant, data.get("default_layout"), LayoutVariant.SINGLE_COLUMN, warnings, "default_layout"
        )
        default_density = _coerce_enum(
            ContentDensity, data.get("default_density"), ContentDensity.STANDARD, warnings, "default_density"
        )

        sections_raw = data.get("sections") or []
        sections: List[ReportSection] = []
        if isinstance(sections_raw, list):
            for i, s in enumerate(sections_raw[:100]):
                if isinstance(s, dict):
                    sections.append(ReportSection.from_dict(s, warnings, fallback_order=i))
                else:
                    warnings.append(f"sections[{i}]: expected an object; skipping")
        else:
            warnings.append("sections: expected a list; using no sections")

        # Ensure deterministic ordering and no duplicate ids.
        sections.sort(key=lambda s: s.order)
        seen_ids = set()
        for s in sections:
            if s.id in seen_ids:
                original = s.id
                suffix = 2
                while f"{original}-{suffix}" in seen_ids:
                    suffix += 1
                s.id = f"{original}-{suffix}"
                warnings.append(f"section id {original!r} duplicated; renamed to {s.id!r}")
            seen_ids.add(s.id)

        if not sections:
            warnings.append("sections: none provided/valid; report will render with only cover/executive summary")

        spec = ReportPresentationSpec(
            domain=domain,
            cover=cover,
            executive_summary=executive_summary,
            sections=sections,
            source_appendix=source_appendix,
            default_layout=default_layout,
            default_density=default_density,
        )
        return spec, warnings

    # -- Strict validation ---------------------------------------------

    def validate_strict(self) -> None:
        """Raise SchemaValidationError if the spec is not fully well-formed.

        Useful for tests/CI or callers that want a hard failure instead of
        silent fallback. ``from_llm_output`` should be preferred for
        untrusted LLM input in production code paths.
        """
        issues: List[str] = []

        if not isinstance(self.domain, ReportDomain):
            issues.append("domain must be a ReportDomain")
        if not isinstance(self.cover, CoverSpec):
            issues.append("cover must be a CoverSpec")
        if not isinstance(self.executive_summary, ExecutiveSummarySpec):
            issues.append("executive_summary must be an ExecutiveSummarySpec")
        if not isinstance(self.source_appendix, SourceAppendixSpec):
            issues.append("source_appendix must be a SourceAppendixSpec")
        if not isinstance(self.sections, list) or not self.sections:
            issues.append("sections must be a non-empty list")
        else:
            seen_ids = set()
            for idx, s in enumerate(self.sections):
                if not isinstance(s, ReportSection):
                    issues.append(f"sections[{idx}] must be a ReportSection")
                    continue
                if not s.id:
                    issues.append(f"sections[{idx}].id must be non-empty")
                if s.id in seen_ids:
                    issues.append(f"sections[{idx}].id {s.id!r} is duplicated")
                seen_ids.add(s.id)
                if not s.title:
                    issues.append(f"sections[{idx}].title must be non-empty")
                if not isinstance(s.section_type, SectionType):
                    issues.append(f"sections[{idx}].section_type must be a SectionType")
                if not isinstance(s.layout, LayoutVariant):
                    issues.append(f"sections[{idx}].layout must be a LayoutVariant")
                if not isinstance(s.density, ContentDensity):
                    issues.append(f"sections[{idx}].density must be a ContentDensity")
                if not isinstance(s.emphasis, EmphasisLevel):
                    issues.append(f"sections[{idx}].emphasis must be an EmphasisLevel")
                for b_idx, b in enumerate(s.blocks):
                    if not hasattr(b, "kind") or getattr(b, "kind") not in _BLOCK_KIND_MAP:
                        issues.append(f"sections[{idx}].blocks[{b_idx}] has an unrecognized kind")
                    text_fields_ok = _validate_block_no_markup(b)
                    if not text_fields_ok:
                        issues.append(f"sections[{idx}].blocks[{b_idx}] contains disallowed markup")

        if issues:
            raise SchemaValidationError(issues)

    # -- Serialization ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return _dataclass_to_jsonable(self)


def _validate_block_no_markup(block: Any) -> bool:
    """Recursively confirm no string field on a block contains markup."""
    for name in getattr(block, "__dataclass_fields__", {}):
        value = getattr(block, name)
        if isinstance(value, str) and contains_markup(value):
            return False
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and contains_markup(item):
                    return False
                if hasattr(item, "__dataclass_fields__") and not _validate_block_no_markup(item):
                    return False
        if isinstance(value, dict):
            for v in value.values():
                if isinstance(v, str) and contains_markup(v):
                    return False
    return True


# ---------------------------------------------------------------------------
# Convenience factory presets (illustrative starting points per domain;
# callers/LLM output are free to diverge substantially from these).
# ---------------------------------------------------------------------------


def default_spec_for_domain(domain: Union[ReportDomain, str]) -> ReportPresentationSpec:
    """Return a minimal, valid starting-point spec for a given domain.

    This is a convenience helper only -- it is NOT used by any planner or
    renderer in this step, and callers are free to build entirely custom
    specs via ``ReportPresentationSpec.from_llm_output``.
    """
    warnings: List[str] = []
    domain_enum = _coerce_enum(ReportDomain, domain, ReportDomain.GENERIC, warnings, "domain")

    presets: Dict[ReportDomain, Dict[str, Any]] = {
        ReportDomain.FINANCIAL: {
            "cover": {"treatment": TitleTreatment.DATA_DRIVEN.value},
            "executive_summary": {"placement": ExecutiveSummaryPlacement.AFTER_COVER.value},
            "sections": [
                {"id": "metrics", "title": "Key Metrics", "section_type": SectionType.METRICS_DASHBOARD.value,
                 "layout": LayoutVariant.GRID.value, "order": 0},
                {"id": "financials", "title": "Financial Performance", "section_type": SectionType.FINANCIALS.value,
                 "layout": LayoutVariant.TWO_COLUMN.value, "order": 1},
                {"id": "risks", "title": "Risk Factors", "section_type": SectionType.RISK_ASSESSMENT.value, "order": 2},
            ],
            "source_appendix": {"placement": SourcePlacement.END_OF_REPORT.value},
        },
        ReportDomain.REGULATORY: {
            "cover": {"treatment": TitleTreatment.CLASSIC.value},
            "executive_summary": {"placement": ExecutiveSummaryPlacement.TOP_OF_BODY.value},
            "sections": [
                {"id": "compliance", "title": "Compliance Overview", "section_type": SectionType.COMPLIANCE.value, "order": 0},
                {"id": "findings", "title": "Findings", "section_type": SectionType.FINDINGS.value, "order": 1},
                {"id": "appendix", "title": "Appendix", "section_type": SectionType.APPENDIX.value, "order": 2},
            ],
            "source_appendix": {"placement": SourcePlacement.APPENDIX.value, "include_appendix": True},
        },
        ReportDomain.SCIENTIFIC: {
            "cover": {"treatment": TitleTreatment.MINIMAL.value},
            "executive_summary": {"placement": ExecutiveSummaryPlacement.TOP_OF_BODY.value},
            "sections": [
                {"id": "methodology", "title": "Methodology", "section_type": SectionType.METHODOLOGY.value, "order": 0},
                {"id": "findings", "title": "Findings", "section_type": SectionType.FINDINGS.value, "order": 1},
                {"id": "sources", "title": "References", "section_type": SectionType.SOURCES.value, "order": 2},
            ],
            "source_appendix": {"placement": SourcePlacement.INLINE_FOOTNOTES.value},
        },
        ReportDomain.COMPANY_ANALYSIS: {
            "cover": {"treatment": TitleTreatment.BOLD_BANNER.value},
            "sections": [
                {"id": "overview", "title": "Company Overview", "section_type": SectionType.OVERVIEW.value, "order": 0},
                {"id": "metrics", "title": "Key Metrics", "section_type": SectionType.METRICS_DASHBOARD.value,
                 "layout": LayoutVariant.GRID.value, "order": 1},
                {"id": "narrative", "title": "Analysis", "section_type": SectionType.NARRATIVE.value, "order": 2},
                {"id": "recommendations", "title": "Recommendations", "section_type": SectionType.RECOMMENDATIONS.value, "order": 3},
            ],
        },
        ReportDomain.MARKET_NEWS: {
            "cover": {"treatment": TitleTreatment.BOLD_BANNER.value},
            "executive_summary": {"placement": ExecutiveSummaryPlacement.TOP_OF_BODY.value},
            "sections": [
                {"id": "market_context", "title": "Market Context", "section_type": SectionType.MARKET_CONTEXT.value, "order": 0},
                {"id": "news", "title": "News Digest", "section_type": SectionType.NEWS_DIGEST.value, "order": 1},
                {"id": "timeline", "title": "Timeline", "section_type": SectionType.TIMELINE.value, "order": 2},
            ],
        },
        ReportDomain.COMPARISON: {
            "cover": {"treatment": TitleTreatment.CLASSIC.value},
            "sections": [
                {"id": "comparison", "title": "Comparison", "section_type": SectionType.COMPARISON.value,
                 "layout": LayoutVariant.TWO_COLUMN.value, "order": 0},
                {"id": "recommendations", "title": "Recommendation", "section_type": SectionType.RECOMMENDATIONS.value, "order": 1},
            ],
        },
        ReportDomain.GENERIC: {
            "sections": [
                {"id": "overview", "title": "Overview", "section_type": SectionType.OVERVIEW.value, "order": 0},
            ],
        },
    }

    preset = presets.get(domain_enum, presets[ReportDomain.GENERIC])
    preset = dict(preset)
    preset["domain"] = domain_enum.value
    spec, _ = ReportPresentationSpec.from_llm_output(preset)
    return spec
