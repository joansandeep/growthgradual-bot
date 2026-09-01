"""
Enhanced PDF generation module for Growth Gradual reports.

Features:
  - Platypus-based document flow (automatic page breaks, no orphaned headers)
  - KeepTogether elements prevent section headers from splitting from content
  - Improved scatterplots with clear axis labels and data point identification
  - Professional typography with navbar/gold color scheme
  - Proper spacing and visual hierarchy
  - Supports charts, tables, and complex layouts

Usage:
  from routes.pdf_enhanced import build_pdf_enhanced
  
  pdf_bytes = build_pdf_enhanced(
    report="Section 1: ...\nSection 2: ...",
    title="TCS Stock Analysis",
    summary="Summary text",
    charts=[{...}],
    key_stats=[{...}],
    theme={"primaryColor": "#1a472a", "accentColor": "#FFA502"}
  )
"""

import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
        Image, KeepTogether
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
except ImportError as e:
    raise ImportError(f"ReportLab required for pdf_enhanced: {e}")

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
except ImportError:
    matplotlib = None
    plt = None

log = logging.getLogger("pdf_enhanced")

# ─── Color Palette (Growth Gradual brand) ───────────────────────────────────
# EXACTLY matching pdf.py to maintain consistency across Canvas and Platypus PDFs
# Matches the Growth Gradual house style: deep navy + gold with restrained accent colours
NAVY    = colors.Color(26/255,  31/255,  78/255)
GOLD    = colors.Color(200/255, 134/255, 10/255)
GREEN   = colors.Color(22/255,  128/255, 88/255)     # muted teal-green
RED     = colors.Color(185/255, 60/255,  55/255)    # muted brick-red
TEAL    = colors.Color(33/255,  118/255, 122/255)   # cool teal
SLATE   = colors.Color(74/255,  92/255,  138/255)   # muted slate blue
OLIVE   = colors.Color(128/255, 110/255, 40/255)    # muted olive/bronze
BURGUNDY = colors.Color(110/255, 47/255, 58/255)   # muted burgundy
AMBER   = colors.Color(194/255, 140/255, 40/255)    # close to GOLD
WHITE   = colors.Color(1.0, 1.0, 1.0)
LIGHT   = colors.Color(240/255, 243/255, 255/255)
GREY    = colors.Color(139/255, 147/255, 181/255)
BODY_TXT = colors.Color(0.18, 0.21, 0.38)

# Aliases for consistency with pdf.py
BLUE    = SLATE
PURPLE  = BURGUNDY
CYAN    = TEAL
PINK    = BURGUNDY

CHART_COLORS = [NAVY, GOLD, TEAL, RED, SLATE, OLIVE, BURGUNDY, GREEN]
SECTION_ACCENTS = [GOLD, TEAL, GREEN, OLIVE, RED, SLATE]


def _hex_to_reportlab_color(hex_str: str) -> colors.Color:
    """Convert #RRGGBB to ReportLab Color object, fallback to navy."""
    try:
        if hex_str.startswith('#'):
            hex_str = hex_str[1:]
        if len(hex_str) == 6:
            return colors.HexColor(f'#{hex_str}')
    except:
        pass
    return NAVY


def create_efficiency_scatterplot(
    title: str = "Efficiency Metrics: ROE vs ROCE",
    output_path: str = "/tmp/scatter_efficiency.png"
) -> Optional[str]:
    """
    Create an interpretable efficiency scatterplot (ROE vs ROCE).
    
    Args:
      title: Chart title
      output_path: Where to save the PNG
      
    Returns:
      Path to the PNG file, or None if matplotlib unavailable
    """
    if not plt:
        log.warning("matplotlib not available, skipping scatterplot generation")
        return None
    
    try:
        fig, ax = plt.subplots(figsize=(9, 6), dpi=100)
        
        # Sample data: company benchmarks
        companies = ['Small Cap', 'Mid Cap', 'Large Cap', 'Your Company']
        roe = [3, 28, 51, 51.8]
        roce = [5, 25, 55, 63.0]
        dot_colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#FFA502']
        
        # Scatter plot
        ax.scatter(roe, roce, s=400, c=dot_colors, alpha=0.7, 
                   edgecolors='black', linewidth=2)
        
        # Label each point
        for i, company in enumerate(companies):
            ax.annotate(company, (roe[i], roce[i]), 
                       xytext=(8, 8), textcoords='offset points',
                       fontsize=10, fontweight='bold',
                       bbox=dict(boxstyle='round,pad=0.3', 
                                 facecolor='yellow', alpha=0.3))
        
        ax.set_xlabel('Return on Equity - ROE (%)', fontsize=12, fontweight='bold')
        ax.set_ylabel('Return on Capital Employed - ROCE (%)', 
                     fontsize=12, fontweight='bold')
        ax.set_title(title, fontsize=13, fontweight='bold', pad=15)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_xlim(-5, 70)
        ax.set_ylim(0, 70)
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        
        log.info(f"Scatterplot saved: {output_path}")
        return output_path
    except Exception as e:
        log.error(f"Failed to create scatterplot: {e}")
        return None


def build_pdf_enhanced(
    report: str,
    title: str,
    summary: str,
    question: str = "",
    key_stats: Optional[list] = None,
    charts: Optional[list] = None,
    theme: Optional[dict] = None,
    logo_b64: str = ""
) -> bytes:
    """
    Generate a professional PDF report using Platypus (flow-based layout).
    
    Args:
      report: Markdown/HTML-like report text
      title: Report title
      summary: Executive summary
      question: Original query (for context)
      key_stats: List of metric dicts [{label, value, unit}, ...]
      charts: List of chart info [{title, image_path}, ...]
      theme: Color theme {primaryColor, accentColor}
      logo_b64: Base64-encoded logo (optional)
      
    Returns:
      PDF file as bytes
    """
    
    # Apply theme colors
    primary = _hex_to_reportlab_color(
        (theme or {}).get("primaryColor", "#1a472a")
    )
    accent = _hex_to_reportlab_color(
        (theme or {}).get("accentColor", "#F39C12")
    )
    
    key_stats = key_stats or []
    charts = charts or []
    
    # Create in-memory PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=0.75*inch,
        bottomMargin=0.75*inch,
        title=title
    )
    
    # Define custom styles
    styles = getSampleStyleSheet()
    
    # Use original palette colors (from pdf.py) as defaults, override with theme if provided
    title_color = primary or NAVY
    accent_color = accent or GOLD
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=28,
        textColor=title_color,
        spaceAfter=6,
        fontName='Helvetica-Bold',
        alignment=TA_CENTER
    )
    
    subtitle_style = ParagraphStyle(
        'CustomSubtitle',
        parent=styles['Normal'],
        fontSize=14,
        textColor=accent_color,
        spaceAfter=12,
        fontName='Helvetica-Bold',
        alignment=TA_CENTER
    )
    
    section_heading_style = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=accent_color,
        spaceAfter=12,
        spaceBefore=12,
        fontName='Helvetica-Bold'
    )
    
    heading2_style = ParagraphStyle(
        'Heading2Custom',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=title_color,
        spaceAfter=8,
        spaceBefore=8,
        fontName='Helvetica-Bold'
    )
    
    body_style = ParagraphStyle(
        'BodyCustom',
        parent=styles['Normal'],
        fontSize=10,
        alignment=TA_JUSTIFY,
        spaceAfter=8,
        leading=12,
        textColor=BODY_TXT
    )
    
    # Build story (document content)
    story = []
    
    # ========== COVER PAGE ==========
    story.append(Spacer(1, 0.5*inch))
    story.append(Paragraph(title, title_style))
    story.append(Paragraph("GROWTH GRADUAL", ParagraphStyle(
        'Tagline', parent=styles['Normal'], fontSize=11,
        alignment=TA_CENTER, textColor=GREY
    )))
    story.append(Spacer(1, 0.3*inch))
    
    # Date
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%d %B %Y")
    story.append(Paragraph(date_str, ParagraphStyle(
        'Date', parent=styles['Normal'], fontSize=12,
        alignment=TA_CENTER, textColor=title_color or NAVY
    )))
    story.append(Spacer(1, 0.4*inch))
    
    # Key metrics on cover
    if key_stats:
        metrics_rows = [[stat.get('label', '') for stat in key_stats]]
        metrics_rows.append([str(stat.get('value', '')) for stat in key_stats])
        if any(stat.get('unit') for stat in key_stats):
            metrics_rows.append([stat.get('unit', '') for stat in key_stats])
        
        col_width = (6.5*inch) / len(key_stats) if key_stats else 1*inch
        metrics_table = Table(metrics_rows, colWidths=[col_width]*len(key_stats))
        metrics_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), title_color or NAVY),
            ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('TOPPADDING', (0, 0), (-1, 0), 12),
            ('GRID', (0, 0), (-1, -1), 0.5, GREY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [LIGHT, WHITE])
        ]))
        story.append(metrics_table)
    
    story.append(PageBreak())
    
    # ========== EXECUTIVE SUMMARY ==========
    story.append(Paragraph("Executive Summary", section_heading_style))
    story.append(Spacer(1, 0.1*inch))
    story.append(Paragraph(summary, body_style))
    story.append(PageBreak())
    
    # ========== REPORT SECTIONS (parse markdown/HTML) ==========
    # Split report by section headers (##, ###)
    sections = re.split(r'^(#+\s+.+)$', report, flags=re.MULTILINE)
    
    i = 0
    while i < len(sections):
        section = sections[i].strip()
        content = sections[i+1].strip() if i+1 < len(sections) else ""
        
        if section.startswith('#'):
            # Extract heading level and text
            match = re.match(r'^(#+)\s+(.+)$', section)
            if match:
                level = len(match.group(1))
                heading_text = match.group(2)
                
                # Choose style based on heading level
                if level == 2:
                    heading_style = section_heading_style
                elif level == 3:
                    heading_style = heading2_style
                else:
                    heading_style = heading2_style
                
                # Keep heading + content together when possible
                section_elements = [
                    Paragraph(heading_text, heading_style),
                    Spacer(1, 0.1*inch)
                ]
                
                # Parse content paragraphs
                if content:
                    for para in content.split('\n\n'):
                        para = para.strip()
                        if para:
                            # Handle bullet points
                            if para.startswith('•') or para.startswith('-'):
                                section_elements.append(
                                    Paragraph(para, body_style)
                                )
                            else:
                                section_elements.append(
                                    Paragraph(para, body_style)
                                )
                
                # Use KeepTogether for short sections
                if len(section_elements) <= 5:
                    story.append(KeepTogether(section_elements))
                else:
                    for elem in section_elements:
                        story.append(elem)
                
                story.append(PageBreak())
        
        i += 2
    
    # ========== CHARTS/IMAGES ==========
    if charts:
        story.append(Paragraph("Charts & Visualizations", section_heading_style))
        story.append(Spacer(1, 0.1*inch))
        
        for chart in charts:
            chart_title = chart.get('title', 'Chart')
            image_path = chart.get('image_path') or chart.get('image')
            
            if image_path:
                try:
                    img = Image(image_path, width=6*inch, height=4.5*inch)
                    img_container = KeepTogether([
                        Paragraph(f"<b>{chart_title}</b>", heading2_style),
                        img,
                        Spacer(1, 0.1*inch)
                    ])
                    story.append(img_container)
                except Exception as e:
                    log.warning(f"Could not embed chart image {image_path}: {e}")
                    story.append(Paragraph(f"[Chart: {chart_title}]", body_style))
            
            story.append(Spacer(1, 0.15*inch))
    
    # ========== FOOTER ==========
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph(
        f"<i><font size=9>Report Generated: {date_str} | Source: Growth Gradual Research</font></i>",
        ParagraphStyle('Footer', parent=styles['Normal'], fontSize=9,
                      alignment=TA_CENTER, textColor=GREY)
    ))
    
    # Build PDF
    doc.build(story)
    
    pdf_bytes = buf.getvalue()
    buf.close()
    
    log.info(f"Enhanced PDF generated: {len(pdf_bytes)} bytes")
    return pdf_bytes


# Optional: Export function for use in FastAPI routes
async def generate_pdf_enhanced_async(**kwargs) -> bytes:
    """Async wrapper for PDF generation."""
    return build_pdf_enhanced(**kwargs)
