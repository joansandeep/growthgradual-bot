# Enhanced PDF Generation for Growth Gradual

## Overview

The finbot-premium backend now includes an improved PDF generation system alongside the existing Canvas-based approach. The enhanced PDF generator uses ReportLab's **Platypus** library for automatic document flow and professional layouts.

## Features

### 1. **Automatic Page Breaks with KeepTogether**
- Section headers no longer appear orphaned at the bottom of pages
- Content groups (header + body) stay together when possible
- Intelligent page flow without manual positioning

### 2. **Improved Chart Generation**
- New `create_efficiency_scatterplot()` function for ROE vs ROCE comparisons
- Clear axis labels: "Return on Equity (%)" vs "Return on Capital Employed (%)"
- Data points labeled with company/entity names
- Professional color scheme matching Growth Gradual brand

### 3. **Professional Typography**
- Navy (#1a472a) and Gold (#F39C12) color scheme
- Proper heading hierarchy (H1, H2, H3 styles)
- Consistent spacing and padding
- Justified body text with proper leading

### 4. **Color Theme Support**
- Accepts custom `primaryColor` and `accentColor` in hex format
- Automatic fallback to navy/gold brand palette
- Theme colors propagate to all text and table elements

### 5. **Better Table Rendering**
- Bordered tables with alternating row backgrounds
- Clear column headers with contrasting background
- Proper alignment and padding

## API Endpoints

### Original Canvas-Based PDF
```
POST /api/chat/report/pdf
Content-Type: application/json

Body:
{
  "report": "Markdown/HTML report text",
  "title": "Report Title",
  "summary": "Executive summary",
  "keyStats": [{"label": "Metric", "value": "123", "unit": "%"}],
  "charts": [{"title": "Chart Name", "image_path": "/path/to/image.png"}],
  "question": "Original query",
  "theme": {"primaryColor": "#1a472a", "accentColor": "#F39C12"}
}

Response: PDF file (attachment)
```

### Enhanced Platypus-Based PDF (NEW)
```
POST /api/chat/report/pdf/enhanced
Content-Type: application/json

Body: (Same as above)

Response: PDF file with improved layout (attachment)
```

## Usage Examples

### Python - Using Enhanced PDF Generator Directly

```python
from routes.pdf_enhanced import build_pdf_enhanced, create_efficiency_scatterplot

# Create a scatterplot
chart_path = create_efficiency_scatterplot(
    title="Capital Efficiency Comparison",
    output_path="/tmp/scatter_chart.png"
)

# Generate PDF
pdf_bytes = build_pdf_enhanced(
    report="""
## Market Analysis
TCS maintains strong fundamentals...

## Valuation Metrics
The P/E ratio of 15.80 suggests...
""",
    title="TCS Stock Analysis 2026",
    summary="Comprehensive technical and fundamental analysis of Tata Consultancy Services.",
    key_stats=[
        {"label": "Current Price", "value": "2,342", "unit": "Rs."},
        {"label": "Market Cap", "value": "8,46,002", "unit": "Rs. Cr"},
        {"label": "P/E Ratio", "value": "15.80", "unit": "x"},
    ],
    charts=[
        {"title": "Efficiency Metrics", "image_path": chart_path}
    ],
    theme={
        "primaryColor": "#1a472a",
        "accentColor": "#F39C12"
    }
)

# Save to file
with open("report.pdf", "wb") as f:
    f.write(pdf_bytes)
```

### Frontend - Call Enhanced Endpoint

```javascript
const response = await fetch('/api/chat/report/pdf/enhanced', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    report: reportMarkdown,
    title: "TCS Analysis",
    summary: summaryText,
    keyStats: metrics,
    charts: chartData,
    theme: {
      primaryColor: "#1a472a",
      accentColor: "#FFA502"
    }
  })
});

const pdfBlob = await response.blob();
// Download or display PDF
```

## Module Structure

```
backend/
├── routes/
│   ├── pdf.py                 # Original Canvas-based PDF (existing)
│   └── pdf_enhanced.py        # NEW: Platypus-based PDF
├── requirements.txt           # UPDATED: +matplotlib, +pandas
└── PDF_ENHANCEMENTS.md        # This file
```

## Dependencies Added

```
matplotlib>=3.7.0
pandas>=2.0.0
```

These are in addition to the existing `reportlab==4.2.2` dependency.

## Key Functions in pdf_enhanced.py

### `build_pdf_enhanced(**kwargs) -> bytes`
Main function to generate an enhanced PDF.

**Parameters:**
- `report` (str): Markdown/HTML report text
- `title` (str): Report title
- `summary` (str): Executive summary
- `question` (str, optional): Original query for context
- `key_stats` (list, optional): Metrics [{label, value, unit}, ...]
- `charts` (list, optional): Chart info [{title, image_path}, ...]
- `theme` (dict, optional): Color theme {primaryColor, accentColor}
- `logo_b64` (str, optional): Base64-encoded logo image

**Returns:**
- `bytes`: PDF file content (ready for download)

### `create_efficiency_scatterplot(title, output_path) -> Optional[str]`
Generate an interpretable efficiency scatterplot.

**Parameters:**
- `title` (str): Chart title
- `output_path` (str): Where to save the PNG

**Returns:**
- `str`: Path to the PNG file, or None if matplotlib unavailable

## Comparison: Canvas vs Platypus

| Feature | Canvas (pdf.py) | Platypus (pdf_enhanced.py) |
|---------|-----------------|---------------------------|
| Page breaks | Manual positioning | Automatic flow |
| Orphaned headers | Possible | Prevented by KeepTogether |
| Chart quality | PNG injection | Matplotlib native |
| Typography | Fine-grained control | Style-based hierarchy |
| Table layout | Canvas-based | ReportLab table objects |
| Code complexity | High (pixel math) | Lower (declarative) |
| Performance | Fast (direct canvas) | Slightly slower (flow calc) |

## Migration Guide

To migrate existing report generation to use the enhanced PDF:

1. **Update endpoint call:**
   ```javascript
   // Old
   const url = '/api/chat/report/pdf';
   
   // New
   const url = '/api/chat/report/pdf/enhanced';
   ```

2. **Optionally create charts using new utility:**
   ```python
   from routes.pdf_enhanced import create_efficiency_scatterplot
   chart_path = create_efficiency_scatterplot()
   ```

3. **Keep the same request body format** — no breaking changes!

## Future Enhancements

- [ ] Support for custom Matplotlib chart types (bar, line, pie)
- [ ] Automatic table-of-contents generation
- [ ] Landscape page support for wide tables
- [ ] Interactive PDF elements (hyperlinks, bookmarks)
- [ ] Multi-language support in standard templates
- [ ] Custom font support (beyond Helvetica)

## Troubleshooting

### "matplotlib not available" warning
- Ensure `matplotlib>=3.7.0` is installed: `pip install -r requirements.txt`
- The PDF will still generate without charts

### Charts not appearing in PDF
- Check that `image_path` is an absolute or valid relative path
- Verify file exists and is readable
- Check logs for "Could not embed chart image" warnings

### Orphaned headers still appearing
- Ensure you're calling the `/pdf/enhanced` endpoint, not `/pdf`
- Verify the section structure has both header and content

## Support

For issues or feature requests, refer to the Growth Gradual development team or update this documentation.
