"""Renders the insider briefing as a PDF.

House style is inherited from the dip / surge / stable-growth reports so the
four sit together as one family: US Letter, Helvetica throughout, a dark
accent header on every table, a shared neutral set (#333 body, #555 subtitle,
#666 captions, #d0d5dd grid, #f4f6f9 zebra), and light charts. Only the accent
hue is new — indigo, because blue, green and near-black navy are already taken
by the three existing report families.

The information design is a deliberate departure from what this report used to
be. The old version opened with database metadata, then a section that was
usually empty, then twenty-two chronological cluster dumps with a full-page
chart each. This one leads with the finding:

    page 1     what happened, in numbers and four sentences
    page 2     the purchases themselves
    pages 3+   the strongest clusters, with evidence and a track record
    then       the league table of every cluster, flows, people
    appendix   the full tape

Emoji are absent on purpose. The previous version used them for headings and
sentiment labels; Helvetica has no glyphs for them, so in a PDF they render as
blank space. Sentiment is carried by cell colour and a plain word instead.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from reports.analysis import (
    Cluster,
    Findings,
    _shorten_role,
    fmt_date,
    fmt_money,
    fmt_shares,
    narrative,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Palette — neutrals and semantics quoted from the reference reports
# ═══════════════════════════════════════════════════════════════════════════

INK = HexColor("#1a1a2e")        # masthead title
ACCENT = HexColor("#2b3a67")     # primary table headers and rules
ACCENT_MID = HexColor("#3d5a80") # secondary headers
ACCENT_DK = HexColor("#2c3e50")  # tertiary headers
BAND = HexColor("#e9edf5")       # pale label bands
ZEBRA = HexColor("#f4f6f9")      # alternating row
GRID = HexColor("#d0d5dd")       # gridlines
RULE = HexColor("#cccccc")       # thin horizontal rules
BODY = HexColor("#333333")
MUTED = HexColor("#555555")
SMALL = HexColor("#666666")

BUY = HexColor("#2e7d32")
SELL = HexColor("#c62828")
WARN = HexColor("#ef6c00")
BUY_BG = HexColor("#e8f5e9")
SELL_BG = HexColor("#fdecea")

PAGE_W, PAGE_H = letter
MARGIN = 0.55 * inch
USABLE_W = PAGE_W - 2 * MARGIN   # 7.40in

# ═══════════════════════════════════════════════════════════════════════════
# Styles
# ═══════════════════════════════════════════════════════════════════════════

_base = getSampleStyleSheet()

TITLE_S = ParagraphStyle("T", parent=_base["Title"], fontSize=25, leading=29,
                         spaceAfter=2, textColor=INK, fontName="Helvetica-Bold",
                         alignment=TA_LEFT)
SUB_S = ParagraphStyle("Sub", parent=_base["Normal"], fontSize=10.5, leading=15,
                       textColor=MUTED, spaceAfter=14)
H2_S = ParagraphStyle("H2", parent=_base["Heading2"], fontSize=14, leading=17,
                      textColor=INK, spaceBefore=12, spaceAfter=7,
                      fontName="Helvetica-Bold")
H3_S = ParagraphStyle("H3", parent=_base["Heading3"], fontSize=11.5, leading=14,
                      textColor=ACCENT, spaceBefore=9, spaceAfter=3,
                      fontName="Helvetica-Bold")
LABEL_S = ParagraphStyle("Lbl", parent=_base["Normal"], fontSize=9.5, leading=12,
                         textColor=ACCENT, spaceBefore=11, spaceAfter=5,
                         fontName="Helvetica-Bold")
BODY_S = ParagraphStyle("B", parent=_base["Normal"], fontSize=9, leading=13.5,
                        textColor=BODY, spaceAfter=5)
SMALL_S = ParagraphStyle("Sm", parent=_base["Normal"], fontSize=7.5, leading=10,
                         textColor=SMALL, spaceAfter=3)
CELL_S = ParagraphStyle("Cell", parent=_base["Normal"], fontSize=6.8, leading=8.2,
                        textColor=BODY)
CELL_B = ParagraphStyle("CellB", parent=CELL_S, fontName="Helvetica-Bold")
# A Paragraph's alignment comes from its style; the table's ALIGN command
# only positions plain-string cells. Ticker cells are Paragraphs (so they
# escape safely) and sit in centred columns, hence a centred variant.
CELL_C = ParagraphStyle("CellC", parent=CELL_B, alignment=TA_CENTER)
KPI_N = ParagraphStyle("KpiN", parent=_base["Normal"], fontSize=16, leading=19,
                       alignment=TA_CENTER, fontName="Helvetica-Bold", textColor=INK)
KPI_L = ParagraphStyle("KpiL", parent=_base["Normal"], fontSize=6.5, leading=8,
                       alignment=TA_CENTER, textColor=SMALL)

DISCLAIMER = (
    "This report is a screening tool built from publicly filed SEC Forms 3, 4 "
    "and 5, read directly from EDGAR. It is not investment advice, not a "
    "recommendation to buy or sell any security, and makes no claim to be "
    "complete. Insider purchases are one input among many and are frequently "
    "wrong. Filings are self-reported and can be amended or restated. Verify "
    "anything you intend to act on against the original filing on EDGAR."
)


# ═══════════════════════════════════════════════════════════════════════════
# Primitives
# ═══════════════════════════════════════════════════════════════════════════


def table_style(
    bg: colors.Color = ACCENT,
    header_size: float = 7.0,
    body_size: float = 6.8,
    extra: Optional[Sequence[tuple]] = None,
) -> TableStyle:
    """The one table recipe, used by every table in the report.

    The reference reports retype this twelve-property block per table; having
    it in one place is what keeps eight tables visually identical for free.
    """
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), header_size),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), body_size),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, ZEBRA]),
            ("GRID", (0, 0), (-1, -1), 0.4, GRID),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
        ]
        + list(extra or [])
    )


def _flat_table(data: List[List[Any]], widths: List[float], **kwargs) -> Table:
    """A Table with padding zeroed — used for nested in-cell graphics.

    reportlab adds 6pt on every side of a nested table by default, which
    turns a 5pt swatch into a 17pt block and wrecks the row height.
    """
    table = Table(data, colWidths=widths, **kwargs)
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def bar_cell(fraction: float, width: float = 1.15 * inch,
             colour: colors.Color = BUY) -> Table:
    """A proportional bar, for leaderboard rows.

    Encodes magnitude next to the number so a column of dollar figures can be
    read as a shape rather than compared digit by digit.
    """
    fraction = max(0.015, min(1.0, float(fraction or 0.0)))
    bar = _flat_table([[""]], [width * fraction], rowHeights=[5.5])
    bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colour)]))
    return bar


def swatch(colour: colors.Color, size: float = 5.0) -> Table:
    """A small filled square, standing in for the emoji markers."""
    box = _flat_table([[""]], [size], rowHeights=[size])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colour)]))
    return box


def kpi_row(tiles: Sequence[Tuple[str, str, Optional[colors.Color]]]) -> Table:
    """A band of headline figures, each with a coloured cap.

    Values and labels are escaped: reportlab's Paragraph runs a mini-HTML
    parser, so a bare "&" in a label like "VS S&P 500" is read as the start
    of an entity and comes out as "VS S&P; 500".
    """
    count = len(tiles)
    data = [
        [Paragraph(_escape(value), KPI_N) for value, _, _ in tiles],
        [Paragraph(_escape(label), KPI_L) for _, label, _ in tiles],
    ]
    table = Table(data, colWidths=[USABLE_W / count] * count, rowHeights=[23, 12])

    style: List[tuple] = [
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("BOX", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
        ("LINEBEFORE", (1, 0), (-1, -1), 0.5, GRID),
    ]
    for index, (_, _, cap) in enumerate(tiles):
        if cap is not None:
            style.append(("LINEABOVE", (index, 0), (index, 0), 2.2, cap))

    table.setStyle(TableStyle(style))
    return table


def _cell(text: Any, style: ParagraphStyle = CELL_S) -> Paragraph:
    """Wrap cell text so long names wrap instead of overflowing.

    Necessary rather than decorative: "Beneficial Owner of more than 10% of a
    Class of Security" is 55 characters and appears constantly in this data.
    """
    return Paragraph(_escape(text), style)


def _escape(value: Any) -> str:
    """Make a value safe for reportlab's mini-HTML parser."""
    text = "—" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pct(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None or pd.isna(value) else f"{value:+.{digits}f}%"


def _price(value: Any) -> str:
    try:
        if value is None or pd.isna(value):
            return "—"
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _ownership_mark(flag: Any) -> Table:
    """Direct holdings get a filled marker, indirect a hollow one."""
    return swatch(BUY if str(flag or "").strip().upper() == "D" else GRID)


# ═══════════════════════════════════════════════════════════════════════════
# Page furniture
# ═══════════════════════════════════════════════════════════════════════════


def _make_furniture(run_date_long: str):
    """Build the running head and footer painter for every page."""

    def draw(canv, _doc) -> None:
        canv.saveState()

        canv.setStrokeColor(ACCENT)
        canv.setLineWidth(2)
        canv.line(MARGIN, PAGE_H - 0.46 * inch, PAGE_W - MARGIN, PAGE_H - 0.46 * inch)

        canv.setFont("Helvetica-Bold", 7)
        canv.setFillColor(ACCENT)
        canv.drawString(MARGIN, PAGE_H - 0.42 * inch, "INSIDER TRADING BRIEFING")

        canv.setFont("Helvetica", 7)
        canv.setFillColor(SMALL)
        canv.drawRightString(PAGE_W - MARGIN, PAGE_H - 0.42 * inch, run_date_long)

        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        canv.line(MARGIN, 0.52 * inch, PAGE_W - MARGIN, 0.52 * inch)

        canv.setFont("Helvetica", 7)
        canv.setFillColor(SMALL)
        canv.drawString(MARGIN, 0.38 * inch, "Screening tool only — not investment advice.")
        canv.drawRightString(PAGE_W - MARGIN, 0.38 * inch, f"Page {canv.getPageNumber()}")

        canv.restoreState()

    return draw


# ═══════════════════════════════════════════════════════════════════════════
# Sections
# ═══════════════════════════════════════════════════════════════════════════


def _flow_cover(findings: Findings) -> List[Any]:
    """Page one: the answer, before any of the evidence."""
    story: List[Any] = [Spacer(1, 4)]

    story.append(Paragraph("Insider Trading Briefing", TITLE_S))
    story.append(
        Paragraph(
            f"S&amp;P 500 open-market transactions of $1M or more &mdash; "
            f"{fmt_date(findings.as_of)}<br/>"
            f"Filings {fmt_date(findings.history_start)} to "
            f"{fmt_date(findings.history_end)} &nbsp;|&nbsp; "
            f"{findings.total_trades:,} transactions across "
            f"{findings.total_tickers} companies",
            SUB_S,
        )
    )
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT))
    story.append(Spacer(1, 11))

    net = findings.purchase_value - findings.sale_value
    bought = len(findings.purchases)
    new_clusters = len(findings.recent_clusters)

    # A zero gets a neutral cap. Colouring "0 purchases" green would read as a
    # positive reading of a window in which nothing happened.
    story.append(
        kpi_row(
            [
                (f"{bought}", f"PURCHASES / {findings.window_days}D", BUY if bought else None),
                (fmt_money(findings.purchase_value), "BOUGHT", BUY if bought else None),
                (fmt_money(findings.sale_value), "SOLD", SELL if findings.sale_value else None),
                (fmt_money(net), "NET FLOW", BUY if net > 0 else SELL if net < 0 else None),
                (f"{new_clusters}", "NEW CLUSTERS", ACCENT if new_clusters else None),
            ]
        )
    )
    story.append(Spacer(1, 13))

    story.append(Paragraph("What changed", LABEL_S))
    for line in narrative(findings):
        story.append(Paragraph(_escape(line), BODY_S))

    ranked = findings.by_conviction(5)
    if ranked:
        story.append(Paragraph("Strongest conviction signals on record", LABEL_S))
        story.append(_cluster_league_table(ranked))
        story.append(
            Paragraph(
                "Ranked by a composite of participant count, dollar size, direct "
                "ownership, seniority and recency. Return is the total return of "
                "the stock since the cluster date; alpha is that return less the "
                "S&amp;P 500 over the same window, in percentage points.",
                SMALL_S,
            )
        )

    if not findings.type_breakdown.empty:
        story.append(Paragraph("What the window was made of", LABEL_S))
        story.append(_type_mix_table(findings))

    # Kept together: when the type table runs long this block would otherwise
    # split mid-sentence across the page break, and a cover page that ends
    # in the middle of a paragraph reads as an accident.
    method = [Paragraph("Method", LABEL_S)]
    method.append(
        Paragraph(
            "Built from SEC Forms 3, 4 and 5 read directly from EDGAR, covering "
            "every S&amp;P 500 constituent and every transaction of $1,000,000 or "
            "more. Each trade is categorised by the <b>transaction code the filer "
            "themselves reported</b> &mdash; P for an open-market purchase, S for "
            "a sale, M for an option exercise, A for a grant &mdash; so nothing "
            "here is inferred from wording. Only code P counts as buying: "
            "exercises, grants and tax withholding are compensation mechanics and "
            "say nothing about what an insider thinks of the price. Sales made "
            "under a pre-arranged Rule 10b5-1 plan are identified and reported "
            "separately, because a disposal scheduled months in advance is not a "
            "decision about today. A <b>cluster</b> is two or more distinct "
            "insiders at the same company each buying $1M or more on the same day "
            "&mdash; the pattern hardest to explain as scheduled or "
            "personal-liquidity activity.",
            BODY_S,
        )
    )
    story.append(KeepTogether(method))

    return story


def _type_mix_table(findings: Findings) -> Table:
    """Composition of the window by transaction type.

    Earns its place on page one: it is what stops "no purchases" being read as
    "nothing happened". Most windows are dominated by sales and option
    exercises, and seeing that is the context for the zero above it.
    """
    header = ["Transaction type", "Count", "Total value", "Average", "Share of value"]
    widths = [1.95 * inch, 0.75 * inch, 1.20 * inch, 1.15 * inch, 2.35 * inch]

    total = float(findings.type_breakdown["total_value"].sum()) or 1.0
    data: List[List[Any]] = [header]
    extra: List[tuple] = [("ALIGN", (1, 0), (3, -1), "RIGHT")]

    for index, (_, row) in enumerate(findings.type_breakdown.iterrows(), 1):
        kind = str(row["trade_type"])
        colour = BUY if kind == "Buy" else SELL if kind == "Sell" else ACCENT_MID
        share = float(row["total_value"]) / total

        data.append(
            [
                _cell(kind),
                str(int(row["count"])),
                fmt_money(row["total_value"]),
                fmt_money(row["avg_value"]),
                _flat_table(
                    [[bar_cell(share, 1.62 * inch, colour),
                      Paragraph(f" {share:.0%}", CELL_S)]],
                    [1.66 * inch, 0.50 * inch],
                ),
            ]
        )

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(table_style(ACCENT_DK, extra=extra))
    return table


def _cluster_league_table(clusters: Sequence[Cluster], start: int = 1) -> Table:
    """Every cluster as one ranked row: who, how much, and what happened next.

    ``start`` is the rank of the first row. The table is drawn twice from one
    global ranking — the top few on the cover, the remainder under Cluster
    history — and the second must continue the numbering, not restart it.
    """
    header = ["#", "Date", "Ticker", "Insiders", "Cluster $", "Since", "S&P 500",
              "Alpha", "Conviction"]
    widths = [0.30 * inch, 0.80 * inch, 0.62 * inch, 0.62 * inch, 0.86 * inch,
              0.72 * inch, 0.72 * inch, 0.72 * inch, 2.04 * inch]

    data: List[List[Any]] = [header]
    extra: List[tuple] = [
        ("ALIGN", (0, 0), (3, -1), "CENTER"),
        ("ALIGN", (4, 0), (7, -1), "RIGHT"),
    ]

    best = max((c.conviction for c in clusters), default=1.0) or 1.0

    for row_no, cluster in enumerate(clusters, 1):  # header occupies row 0
        index = start + row_no - 1

        if cluster.alpha is not None:
            extra.append(
                (
                    "BACKGROUND",
                    (7, row_no),
                    (7, row_no),
                    BUY_BG if cluster.alpha >= 0 else SELL_BG,
                )
            )
            extra.append(
                (
                    "TEXTCOLOR",
                    (7, row_no),
                    (7, row_no),
                    BUY if cluster.alpha >= 0 else SELL,
                )
            )

        conviction_cell = _flat_table(
            [[bar_cell(cluster.conviction / best, 1.22 * inch, ACCENT_MID),
              Paragraph(f" {cluster.conviction:.2f}", CELL_S)]],
            [1.26 * inch, 0.60 * inch],
        )

        data.append(
            [
                str(index),
                fmt_date(cluster.date),
                Paragraph(_escape(cluster.ticker), CELL_C),
                str(cluster.insiders),
                fmt_money(cluster.total_value),
                _pct(cluster.since_return),
                _pct(cluster.bench_return),
                _pct(cluster.alpha),
                conviction_cell,
            ]
        )

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(table_style(ACCENT, extra=extra))
    return table


def _flow_purchases(findings: Findings) -> List[Any]:
    """The window's open-market purchases, largest first."""
    story: List[Any] = [
        Paragraph(f"Purchases in the last {findings.window_days} days", H2_S)
    ]

    if findings.purchases.empty:
        story.append(
            Paragraph(
                f"No open-market purchases of $1M or more were filed between "
                f"{fmt_date(findings.window_start)} and {fmt_date(findings.as_of)}. "
                f"This is the ordinary case rather than a failure: qualifying "
                f"insider purchases are genuinely rare, and a window containing "
                f"none is more common than one containing any.",
                BODY_S,
            )
        )
        return story

    story.append(
        Paragraph(
            f"{len(findings.purchases)} purchases totalling "
            f"{fmt_money(findings.purchase_value)} across "
            f"{findings.purchase_tickers} companies. Filled markers denote shares "
            f"held directly; hollow markers denote indirect holdings through a "
            f"trust, fund or partnership.",
            BODY_S,
        )
    )

    header = ["Date", "Ticker", "Insider", "Role", "Shares", "Price", "Value", "Own"]
    widths = [0.72 * inch, 0.55 * inch, 1.92 * inch, 1.62 * inch,
              0.72 * inch, 0.62 * inch, 0.87 * inch, 0.38 * inch]

    data: List[List[Any]] = [header]
    for _, row in findings.purchases.iterrows():
        data.append(
            [
                fmt_date(row["trade_date"]),
                _cell(row["ticker"], CELL_C),
                _cell(str(row["insider"]).title()),
                _cell(_shorten_role(row["position"])),
                fmt_shares(row["shares"]),
                _price(row.get("price")),
                fmt_money(row["abs_value"]),
                _ownership_mark(row["ownership"]),
            ]
        )

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        table_style(
            ACCENT,
            extra=[
                ("ALIGN", (0, 0), (1, -1), "CENTER"),
                ("ALIGN", (4, 0), (6, -1), "RIGHT"),
                ("ALIGN", (7, 0), (7, -1), "CENTER"),
                ("TEXTCOLOR", (6, 1), (6, -1), BUY),
                ("FONTNAME", (6, 1), (6, -1), "Helvetica-Bold"),
            ],
        )
    )
    story.append(table)
    return story


def _flow_cluster_spotlight(findings: Findings, limit: int = 8) -> List[Any]:
    """The strongest clusters, each with its participants and a chart."""
    ranked = findings.by_conviction(limit)
    if not ranked:
        return []

    story: List[Any] = [
        PageBreak(),
        Paragraph("Conviction signals", H2_S),
        Paragraph(
            f"The {len(ranked)} highest-conviction clusters on record, each shown "
            f"with its participants and the stock's total return since the "
            f"purchase date against the S&amp;P 500. Shading marks where the stock "
            f"has run ahead of the index; the dashed vertical line is the cluster "
            f"date itself.",
            BODY_S,
        ),
    ]

    for cluster in ranked:
        story.extend(_cluster_block(cluster))

    return story


def _cluster_block(cluster: Cluster) -> List[Any]:
    """One cluster: heading, headline numbers, participants, chart."""
    block: List[Any] = [
        Paragraph(
            f"{_escape(cluster.ticker)} &mdash; {cluster.insiders} insiders, "
            f"{fmt_money(cluster.total_value)}, {cluster.date_long}",
            H3_S,
        )
    ]

    tiles: List[Tuple[str, str, Optional[colors.Color]]] = [
        (fmt_money(cluster.total_value), "TOTAL BOUGHT", ACCENT),
        (f"{cluster.insiders}", "INSIDERS", ACCENT),
        (f"{cluster.pct_direct:.0%}", "HELD DIRECTLY", ACCENT_MID),
    ]
    if cluster.since_return is not None:
        colour = BUY if cluster.since_return >= 0 else SELL
        tiles.append((_pct(cluster.since_return), "SINCE PURCHASE", colour))
    if cluster.alpha is not None:
        colour = BUY if cluster.alpha >= 0 else SELL
        tiles.append((f"{cluster.alpha:+.1f} pp", "VS S&P 500", colour))
    if cluster.days_held is not None:
        tiles.append((f"{cluster.days_held}", "DAYS ELAPSED", None))

    block.append(kpi_row(tiles))
    block.append(Spacer(1, 5))

    header = ["Insider", "Role", "Shares", "Price", "Value", "Own"]
    widths = [2.05 * inch, 2.00 * inch, 0.85 * inch, 0.75 * inch,
              1.10 * inch, 0.65 * inch]

    data: List[List[Any]] = [header]
    for _, row in cluster.trades.iterrows():
        data.append(
            [
                _cell(str(row["insider"]).title()),
                _cell(_shorten_role(row["position"])),
                fmt_shares(row["shares"]),
                _price(row.get("price", None)),
                fmt_money(row["abs_value"]),
                _ownership_mark(row["ownership"]),
            ]
        )

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        table_style(
            ACCENT_MID,
            extra=[
                ("ALIGN", (2, 0), (4, -1), "RIGHT"),
                ("ALIGN", (5, 0), (5, -1), "CENTER"),
                ("TEXTCOLOR", (4, 1), (4, -1), BUY),
                ("FONTNAME", (4, 1), (4, -1), "Helvetica-Bold"),
            ],
        )
    )
    block.append(table)

    if cluster.chart_path and Path(cluster.chart_path).exists():
        block.append(Spacer(1, 4))
        # Aspect must match the figure's figsize (6.6 x 2.4) or the chart is
        # visibly stretched next to the other reports' figures. Sized so two
        # complete cluster blocks fit on one page — at any larger size each
        # block claims a page of its own and the section reads as a slideshow.
        block.append(Image(cluster.chart_path, width=6.05 * inch, height=2.20 * inch))

    block.append(Spacer(1, 9))

    # KeepTogether stops a heading stranding itself at the foot of a page.
    return [KeepTogether(block)]


def _flow_cluster_history(findings: Findings, skip: int = 8) -> List[Any]:
    """Every remaining cluster, ranked, without charts."""
    remaining = findings.by_conviction()[skip:]
    if not remaining:
        return []

    return [
        PageBreak(),
        Paragraph("Cluster history", H2_S),
        Paragraph(
            f"The remaining {len(remaining)} qualifying clusters, same ranking, "
            f"no charts. Nothing is omitted from this table &mdash; the spotlight "
            f"above simply expands the strongest few.",
            BODY_S,
        ),
        _cluster_league_table(remaining, start=skip + 1),
    ]


def _flow_flows_and_people(findings: Findings) -> List[Any]:
    """Where the money went and who moved it."""
    story: List[Any] = [PageBreak(), Paragraph("Flows and participants", H2_S)]

    # ── Net flow by ticker ──────────────────────────────────────────────
    if not findings.ticker_sentiment.empty:
        story.append(Paragraph("Net flow by company", LABEL_S))

        header = ["Ticker", "Buys", "Bought", "Sells", "Sold", "Net", "Signal"]
        widths = [0.72 * inch, 0.65 * inch, 1.12 * inch, 0.68 * inch,
                  1.12 * inch, 1.16 * inch, 1.95 * inch]

        data: List[List[Any]] = [header]
        extra: List[tuple] = [
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (1, 0), (5, -1), "RIGHT"),
        ]

        for index, (_, row) in enumerate(findings.ticker_sentiment.iterrows(), 1):
            if row["bought"] > 0 and row["net"] >= 0:
                label, colour, background = "Net buying", BUY, BUY_BG
            elif row["bought"] > 0:
                label, colour, background = "Mixed", WARN, BAND
            elif row["sold"] > 0:
                label, colour, background = "Net selling", SELL, SELL_BG
            else:
                # Only exercises, grants or withholding in the window — no
                # open-market view in either direction. Calling that "net
                # selling" in red beside a green $0 would be two lies at once.
                label, colour, background = "No open-market trades", SMALL, white

            extra.append(("BACKGROUND", (6, index), (6, index), background))
            extra.append(("TEXTCOLOR", (6, index), (6, index), colour))
            net_colour = BUY if row["net"] > 0 else SELL if row["net"] < 0 else SMALL
            extra.append(("TEXTCOLOR", (5, index), (5, index), net_colour))

            data.append(
                [
                    _cell(row["ticker"], CELL_C),
                    f"{int(row['buy_trades'])}",
                    fmt_money(row["bought"]) if row["bought"] else "—",
                    f"{int(row['sell_trades'])}",
                    fmt_money(row["sold"]) if row["sold"] else "—",
                    fmt_money(row["net"]),
                    label,
                ]
            )

        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(table_style(ACCENT, extra=extra))
        story.append(table)

    # ── Top buyers ──────────────────────────────────────────────────────
    if not findings.top_buyers.empty:
        story.append(Paragraph("Largest buyers in the window", LABEL_S))

        header = ["#", "Insider", "Role", "Ticker", "Trades", "Bought", ""]
        widths = [0.32 * inch, 1.86 * inch, 1.64 * inch, 0.55 * inch,
                  0.55 * inch, 0.98 * inch, 1.50 * inch]

        biggest = float(findings.top_buyers["total_value"].max())
        data = [header]
        for index, (_, row) in enumerate(findings.top_buyers.iterrows(), 1):
            data.append(
                [
                    str(index),
                    _cell(str(row["insider"]).title()),
                    _cell(row["position"]),
                    _cell(row["ticker"], CELL_B),
                    str(int(row["trades"])),
                    fmt_money(row["total_value"]),
                    # 1.35in inside a 1.50in cell: the cell's 3.5pt padding on
                    # each side leaves ~1.40in of content box, and a bar drawn
                    # to the full width would sit on the gridline.
                    bar_cell(row["total_value"] / biggest if biggest else 0, 1.35 * inch),
                ]
            )

        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(
            table_style(
                ACCENT_MID,
                extra=[
                    ("ALIGN", (0, 0), (0, -1), "CENTER"),
                    ("ALIGN", (3, 0), (5, -1), "RIGHT"),
                    ("TEXTCOLOR", (5, 1), (5, -1), BUY),
                    ("FONTNAME", (5, 1), (5, -1), "Helvetica-Bold"),
                ],
            )
        )
        story.append(table)

    # ── Repeat buyers ───────────────────────────────────────────────────
    if not findings.repeat_buyers.empty:
        story.append(Paragraph("Insiders adding on multiple days", LABEL_S))
        story.append(
            Paragraph(
                "Buying again is a stronger statement than buying once: the "
                "second purchase is made with the first already marked to market.",
                SMALL_S,
            )
        )

        header = ["Insider", "Ticker", "Days", "Trades", "Total", "Dates"]
        widths = [1.90 * inch, 0.60 * inch, 0.50 * inch, 0.55 * inch,
                  1.00 * inch, 2.85 * inch]

        data = [header]
        for _, row in findings.repeat_buyers.iterrows():
            data.append(
                [
                    _cell(str(row["insider"]).title()),
                    _cell(row["ticker"], CELL_B),
                    str(int(row["days"])),
                    str(int(row["trades"])),
                    fmt_money(row["total_value"]),
                    _cell(row["dates"]),
                ]
            )

        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(
            table_style(
                ACCENT_MID,
                extra=[
                    ("ALIGN", (1, 0), (4, -1), "CENTER"),
                    ("ALIGN", (4, 1), (4, -1), "RIGHT"),
                ],
            )
        )
        story.append(table)

    return story


def _flow_appendix(findings: Findings, sell_floor: float = 5_000_000) -> List[Any]:
    """The full tape: every purchase, plus sales above a floor.

    Sales are ~90% of the rows and close to 0% of the signal, so listing all
    of them would triple the page count to little purpose. The floor keeps the
    genuinely large disposals — which do carry information — and drops the
    routine ones.
    """
    if findings.purchases.empty and findings.sales.empty:
        return []

    purchases = findings.purchases
    big_sales = findings.sales[findings.sales["abs_value"] >= sell_floor]

    rows = pd.concat([purchases, big_sales]).sort_values(
        "abs_value", ascending=False
    )
    if rows.empty:
        return []

    story: List[Any] = [
        PageBreak(),
        Paragraph("Appendix: the tape", H2_S),
        Paragraph(
            f"Every open-market purchase in the window, plus disposals of "
            f"{fmt_money(sell_floor)} or more. Smaller sales are omitted: they "
            f"are the bulk of the row count and are almost entirely scheduled "
            f"10b5-1 activity.",
            BODY_S,
        ),
    ]

    header = ["Date", "Ticker", "Insider", "Role", "Type", "Shares", "Price", "Value"]
    widths = [0.70 * inch, 0.52 * inch, 1.90 * inch, 1.46 * inch,
              0.62 * inch, 0.72 * inch, 0.62 * inch, 0.86 * inch]  # = 7.40in

    data: List[List[Any]] = [header]
    extra: List[tuple] = [
        ("ALIGN", (0, 0), (1, -1), "CENTER"),
        ("ALIGN", (4, 0), (4, -1), "CENTER"),
        ("ALIGN", (5, 0), (7, -1), "RIGHT"),
    ]

    for index, (_, row) in enumerate(rows.iterrows(), 1):
        is_buy = row["trade_type"] == "Buy"
        extra.append(
            ("TEXTCOLOR", (7, index), (7, index), BUY if is_buy else SELL)
        )
        data.append(
            [
                fmt_date(row["trade_date"]),
                _cell(row["ticker"], CELL_C),
                _cell(str(row["insider"]).title()),
                _cell(_shorten_role(row["position"])),
                _cell("Buy" if is_buy else "Sell"),
                fmt_shares(row["shares"]),
                _price(row.get("price")),
                fmt_money(row["abs_value"]),
            ]
        )

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(table_style(ACCENT, body_size=6.5, extra=extra))
    story.append(table)
    return story


def _flow_definitions() -> List[Any]:
    """Glossary and disclaimer."""
    terms = [
        ("Open-market purchase",
         "An insider buying shares at the prevailing market price with their own "
         "money, under no obligation to do so — SEC transaction code P. The only "
         "transaction type this report treats as a buy signal."),
        ("Transaction code",
         "The one-letter classification the filer assigns to each trade on the "
         "Form 4 itself: P purchase, S sale, M or C option exercise, A grant from "
         "the issuer, D disposition back to the issuer, F shares withheld for tax, "
         "G gift. Every figure here is grouped on this rather than on wording."),
        ("Rule 10b5-1 plan",
         "A trading plan adopted in advance, at a time when the insider held no "
         "material non-public information. Sales made under one are scheduled "
         "rather than chosen, so they are reported separately and should not be "
         "read as a view on the current price."),
        ("Cluster",
         "Two or more distinct insiders at the same company each buying $1M or "
         "more on the same day."),
        ("Conviction",
         "A 0-1 composite of participant count (30%), dollar size on a log scale "
         "(25%), share held directly rather than through a vehicle (20%), "
         "seniority of the participants (15%) and recency (10%)."),
        ("Alpha",
         "Total return of the stock since the cluster date less the S&P 500 over "
         "the same window, in percentage points. Not risk-adjusted."),
        ("Direct / indirect",
         "Direct holdings are registered in the insider's own name. Indirect "
         "holdings sit in a trust, fund or partnership they are affiliated with. "
         "Direct purchases carry the clearer personal stake."),
        ("Exercise, grant, gift",
         "Compensation and estate mechanics. Excluded from every purchase figure "
         "because they say nothing about an insider's view of the price."),
    ]

    story: List[Any] = [PageBreak(), Paragraph("Definitions", H2_S)]

    data: List[List[Any]] = [["Term", "Meaning"]]
    for term, meaning in terms:
        data.append([_cell(term, CELL_B), _cell(meaning)])

    table = Table(data, colWidths=[1.55 * inch, 5.85 * inch], repeatRows=1)
    table.setStyle(table_style(ACCENT_DK))
    story.append(table)

    story.append(Spacer(1, 14))
    story.append(Paragraph("Disclaimer", LABEL_S))
    story.append(Paragraph(DISCLAIMER, SMALL_S))

    return story


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


def build_pdf(
    findings: Findings,
    output_path: str | Path,
    spotlight_limit: int = 8,
) -> Path:
    """Render the briefing. Returns the path written."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    run_date_long = fmt_date(findings.as_of)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=0.68 * inch,
        bottomMargin=0.68 * inch,
        title=f"Insider Trading Briefing — {findings.as_of}",
        author="NexGen Solutions",
        subject="S&P 500 insider transactions of $1M or more",
    )

    story: List[Any] = []
    story += _flow_cover(findings)
    story.append(PageBreak())
    story += _flow_purchases(findings)
    story += _flow_cluster_spotlight(findings, limit=spotlight_limit)
    story += _flow_cluster_history(findings, skip=spotlight_limit)
    story += _flow_flows_and_people(findings)
    story += _flow_appendix(findings)
    story += _flow_definitions()

    furniture = _make_furniture(run_date_long)
    doc.build(story, onFirstPage=furniture, onLaterPages=furniture)

    logger.info("Wrote %s (%.1f KB)", output_path.name, output_path.stat().st_size / 1024)
    return output_path
