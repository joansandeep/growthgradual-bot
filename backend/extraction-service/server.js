// ═══════════════════════════════════════════════════════════════
//  Paperly Extraction Service v3.1 — Robust error handling
//
//  PDF → mupdf (pure WASM) renders pages → Gemini/Groq vision
//  Image → Gemini/Groq vision
//  DOCX  → mammoth
//
//  FIX: Never returns 500 — always returns a valid extraction
//       result (possibly with hasText:false and error message).
// ═══════════════════════════════════════════════════════════════
require('dotenv').config();
const express  = require('express');
const path     = require('path');
const fs       = require('fs');
const axios    = require('axios');
const mammoth  = require('mammoth');
const pdfParse = require('pdf-parse');

const app  = express();
const PORT = process.env.PORT || process.env.EXTRACTION_PORT || 4003;
app.use(express.json({ limit: '50mb' }));

const PDF_SCALE      = 1.5;
const JPEG_QUALITY   = 85;
const MAX_PAGES      = 20;   // Only applies to vision path (scanned/image PDFs)
const VISION_TIMEOUT = 60000;

const VISION_PROMPT = `You are an expert data extractor and OCR system. Extract EVERY piece of text, data, and label from the image with 100% accuracy. NO hallucination.

Rules:
1. DO NOT SUMMARIZE. Write every word, number, and label exactly as it appears.
2. Process ALL charts, datasets, lists on the page. Do not skip any visual element.
3. Extract every label and its corresponding data point.

Format:
=== TEXT CONTENT ===
[All raw text: headings, paragraphs, footnotes]

=== VISUALS, CHARTS & LISTS ===
[For EVERY chart/graph/diagram:]
Section Name / Title: [exact title]
Data:
  - [Label]: [Value/Description]
  ...

=== TABLES & STRUCTURED DATA ===
[Tabular data row by row]`;

async function geminiVision(base64, mimeType) {
  const keysStr = process.env.GEMINI_API_KEY || process.env.GEMINI_API_KEYS || '';
  const keys = keysStr.split(',').map(k => k.trim()).filter(Boolean);
  if (!keys.length) return null;

  const models = ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-2.0-flash-lite'];

  for (const key of keys) {
    for (const model of models) {
      try {
        const res = await axios.post(
          `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`,
          {
            contents: [{ parts: [
              { text: VISION_PROMPT },
              { inline_data: { mime_type: mimeType, data: base64 } },
            ]}],
            generationConfig: { maxOutputTokens: 4096, temperature: 0.0 },
          },
          { headers: { 'Content-Type': 'application/json' }, timeout: VISION_TIMEOUT,
            maxContentLength: Infinity, maxBodyLength: Infinity }
        );
        const text = res.data.candidates?.[0]?.content?.parts?.[0]?.text || '';
        if (text.length > 20) {
          console.log(`    ✅ ${model} (Key ...${key.slice(-4)}): ${text.length} chars`);
          return text;
        }
      } catch (err) {
        const msg = err.response?.data?.error?.message || err.message;
        console.warn(`    ⚠️  ${model} (Key ...${key.slice(-4)}): ${msg.slice(0, 100)}`);
        if (msg.includes('429') || msg.includes('quota') || msg.includes('rate')) continue;
      }
    }
  }
  return null;
}

async function groqVision(base64, mimeType) {
  const keysStr = process.env.GROQ_API_KEY || process.env.GROQ_API_KEYS || '';
  const keys = keysStr.split(',').map(k => k.trim()).filter(Boolean);
  if (!keys.length) return null;

  for (const key of keys) {
    try {
      const res = await axios.post(
        'https://api.groq.com/openai/v1/chat/completions',
        {
          model: 'meta-llama/llama-4-scout-17b-16e-instruct',
          messages: [{
            role: 'user',
            content: [
              { type: 'text', text: VISION_PROMPT },
              { type: 'image_url', image_url: { url: `data:${mimeType};base64,${base64}` } },
            ],
          }],
          max_tokens: 4096,
          temperature: 0.0,
        },
        { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
          timeout: VISION_TIMEOUT }
      );
      const text = res.data.choices?.[0]?.message?.content || '';
      if (text.length > 20) {
        console.log(`    ✅ Groq llama-4-scout (Key ...${key.slice(-4)}): ${text.length} chars`);
        return text;
      }
    } catch (err) {
      const msg = err.response?.data?.error?.message || err.message;
      console.warn(`    ⚠️  Groq vision (Key ...${key.slice(-4)}): ${msg.slice(0, 100)}`);
    }
  }
  return null;
}

async function visionExtract(base64, mimeType) {
  return (await geminiVision(base64, mimeType))
      || (await groqVision(base64, mimeType))
      || null;
}

// Like visionExtract but appends a heading hint to the prompt so the model
// can correctly identify which section/chart it is looking at.
async function visionExtractWithContext(base64, mimeType, headingHint) {
  if (!headingHint) return visionExtract(base64, mimeType);

  const promptWithContext = VISION_PROMPT + headingHint;
  return (await geminiVisionWithPrompt(base64, mimeType, promptWithContext))
      || (await groqVisionWithPrompt(base64, mimeType, promptWithContext))
      || null;
}

async function geminiVisionWithPrompt(base64, mimeType, prompt) {
  const keysStr = process.env.GEMINI_API_KEY || process.env.GEMINI_API_KEYS || '';
  const keys = keysStr.split(',').map(k => k.trim()).filter(Boolean);
  if (!keys.length) return null;

  const models = ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-2.0-flash-lite'];

  for (const key of keys) {
    for (const model of models) {
      try {
        const res = await axios.post(
          `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`,
          {
            contents: [{ parts: [
              { text: prompt },
              { inline_data: { mime_type: mimeType, data: base64 } },
            ]}],
            generationConfig: { maxOutputTokens: 4096, temperature: 0.0 },
          },
          { headers: { 'Content-Type': 'application/json' }, timeout: VISION_TIMEOUT,
            maxContentLength: Infinity, maxBodyLength: Infinity }
        );
        const text = res.data.candidates?.[0]?.content?.parts?.[0]?.text || '';
        if (text.length > 20) return text;
      } catch (err) {
        const msg = err.response?.data?.error?.message || err.message;
        if (msg.includes('429') || msg.includes('quota') || msg.includes('rate')) continue;
      }
    }
  }
  return null;
}

async function groqVisionWithPrompt(base64, mimeType, prompt) {
  const keysStr = process.env.GROQ_API_KEY || process.env.GROQ_API_KEYS || '';
  const keys = keysStr.split(',').map(k => k.trim()).filter(Boolean);
  if (!keys.length) return null;

  for (const key of keys) {
    try {
      const res = await axios.post(
        'https://api.groq.com/openai/v1/chat/completions',
        {
          model: 'meta-llama/llama-4-scout-17b-16e-instruct',
          messages: [{
            role: 'user',
            content: [
              { type: 'text', text: prompt },
              { type: 'image_url', image_url: { url: `data:${mimeType};base64,${base64}` } },
            ],
          }],
          max_tokens: 4096,
          temperature: 0.0,
        },
        { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
          timeout: VISION_TIMEOUT }
      );
      const text = res.data.choices?.[0]?.message?.content || '';
      if (text.length > 20) return text;
    } catch (err) { /* non-fatal */ }
  }
  return null;
}

// ═══════════════════════════════════════════════════════════════
//  SMART PDF EXTRACTION
//
//  Strategy:
//  1. pdf-parse  → full text layer (fast, free, covers all pages)
//  2. mupdf      → per-page structured text to find visual regions
//  3. For each page: detect chart/image bounding boxes from layout
//  4. Crop ONLY those visual regions → send to vision API
//  5. Combine: text layer + visual descriptions per region
//
//  This means:
//  - Text pages     → text layer only (no API call)
//  - Chart pages    → text layer + cropped chart → vision
//  - Mixed pages    → text layer + only the chart crops → vision
//  - Blank pages    → skipped entirely
// ═══════════════════════════════════════════════════════════════

// ─── Line-level helpers ──────────────────────────────────────────────────────

// Extract every line on a page with its Y position, font size, and text.
// mupdf structured-text stores font size on the span level.
function extractPageLines(stJSON) {
  const lines = [];
  for (const block of stJSON.blocks || []) {
    if (block.type !== 'text') continue;
    for (const line of block.lines || []) {
      const spans   = line.spans || [];
      const text    = spans.map(s => s.text || '').join('').trim();
      if (!text) continue;
      const fontSize = spans.reduce((mx, s) => Math.max(mx, s.size || 0), 0);
      const y0 = line.bbox.y;
      const y1 = line.bbox.y + (line.bbox.h || 0);
      lines.push({ text, fontSize, y0, y1 });
    }
  }
  return lines.sort((a, b) => a.y0 - b.y0);
}

// Decide whether a line is a section heading.
// Heuristic: font size >= HEADING_MIN_SIZE AND the line is short (not a
// sentence) AND it is not purely numeric.
const HEADING_MIN_SIZE = 9; // points — tune if needed
function isHeadingLine(line) {
  if (line.fontSize < HEADING_MIN_SIZE) return false;
  if (line.text.length > 80) return false;          // too long → body text
  if (/^\d[\d.,\s%]*$/.test(line.text)) return false; // purely numeric
  return true;
}

// ─── Heading-aware visual region detection ───────────────────────────────────
//
// Strategy:
//  1. Collect all explicit image blocks (large enough to matter).
//  2. Walk every line top-to-bottom.  A line that looks like a heading starts
//     a new "section slot".  A large gap (no text) inside a slot means there
//     is a chart/image there.
//  3. Each slot that contains at least one visual gap becomes its own crop
//     region, labelled with its heading.  This prevents two charts that sit
//     under different headings from being merged into one crop.
//
// Returns array of { x0, y0, x1, y1, heading } — one entry per chart region.

function detectVisualRegions(stJSON, pageH, pageW) {
  const PAD        = 8;   // padding added around each crop (points)
  const MIN_GAP_PT = 40;  // minimum gap height to be considered a visual region
  const MIN_CROP_H = 30;  // ignore regions thinner than this

  // ── 1. Explicit image blocks ─────────────────────────────────────────────
  const imageBlocks = [];
  for (const block of stJSON.blocks || []) {
    if (block.type === 'image') {
      const b = block.bbox;
      const w = b.w || 0;
      const h = b.h || 0;
      if (w > 30 && h > 30) {
        imageBlocks.push({ y0: b.y, y1: b.y + h, x0: b.x, x1: b.x + w });
      }
    }
  }

  // ── 2. All text lines sorted by Y ────────────────────────────────────────
  const lines = extractPageLines(stJSON);

  // ── 3. Walk lines and split into sections at each heading ─────────────────
  // A "section" is: { heading: string|null, y0: number, lines: [...] }
  const sections = [];
  let current    = { heading: null, headingY0: 0, headingY1: 0, lines: [] };

  for (const line of lines) {
    if (isHeadingLine(line)) {
      // Push previous section before starting a new one
      sections.push(current);
      current = { heading: line.text, headingY0: line.y0, headingY1: line.y1, lines: [] };
    } else {
      current.lines.push(line);
    }
  }
  sections.push(current); // last section

  // ── 4. For each section, find visual gaps ─────────────────────────────────
  const results = [];

  for (let si = 0; si < sections.length; si++) {
    const sec = sections[si];

    // The section occupies from headingY1 (bottom of heading) to the start of
    // the next section's heading (or the page bottom).
    const secTop    = sec.headingY1;
    const secBottom = si + 1 < sections.length
      ? sections[si + 1].headingY0
      : pageH;

    if (secBottom - secTop < MIN_GAP_PT) continue;

    // Collect Y intervals covered by text lines within this section
    const coveredY = sec.lines.map(l => ({ y0: l.y0, y1: l.y1 }));

    // Also include any explicit image blocks that fall in this section
    for (const ib of imageBlocks) {
      if (ib.y0 >= secTop && ib.y1 <= secBottom) {
        coveredY.push({ y0: ib.y0, y1: ib.y1 });
        // Image blocks ARE the visual — mark the whole block as a visual region
        const crop = {
          x0: Math.max(0, ib.x0 - PAD),
          y0: Math.max(0, ib.y0 - PAD),
          x1: Math.min(pageW, ib.x1 + PAD),
          y1: Math.min(pageH, ib.y1 + PAD),
          heading: sec.heading,
          source:  'image_block',
        };
        results.push(crop);
      }
    }

    // Find gaps between text lines (and between heading bottom and first line,
    // and between last line and section bottom).
    const sortedCovered = coveredY.sort((a, b) => a.y0 - b.y0);

    // Merge covered intervals
    const merged = [];
    for (const iv of sortedCovered) {
      const last = merged[merged.length - 1];
      if (last && iv.y0 <= last.y1 + 2) {
        last.y1 = Math.max(last.y1, iv.y1);
      } else {
        merged.push({ ...iv });
      }
    }

    // Walk the merged intervals to find gaps
    let cursor = secTop;
    for (const iv of [...merged, { y0: secBottom, y1: secBottom }]) {
      const gapY0 = cursor;
      const gapY1 = iv.y0;
      const gapH  = gapY1 - gapY0;

      if (gapH >= MIN_GAP_PT) {
        const cropH = gapH;
        if (cropH >= MIN_CROP_H) {
          // Check if already covered by an image block we added above
          const alreadyAdded = results.some(
            r => r.source === 'image_block' && r.y0 <= gapY0 + gapH / 2 && r.y1 >= gapY0
          );
          if (!alreadyAdded) {
            results.push({
              x0:      0,
              y0:      Math.max(0,     gapY0 - PAD),
              x1:      pageW,
              y1:      Math.min(pageH, gapY1 + PAD),
              heading: sec.heading,
              source:  'text_gap',
            });
          }
        }
      }
      cursor = Math.max(cursor, iv.y1);
    }
  }

  return results;
}

// Find the nearest text block above a visual region — used as heading context.
// Returns the closest text line whose bottom edge (y1) is at or above regionY0.
// Prioritises larger/bolder text (longer lines tend to be headings when close).
function getNearestHeadingAbove(stJSON, regionY0) {
  const candidates = [];

  for (const block of stJSON.blocks || []) {
    if (block.type !== 'text') continue;
    const blockY1 = block.bbox.y + (block.bbox.h || 0);
    if (blockY1 > regionY0) continue; // block is below or overlaps the region

    for (const line of block.lines || []) {
      const lineText = (line.spans || []).map(s => s.text || '').join('').trim();
      if (!lineText || lineText.length < 3) continue;

      const lineY1 = line.bbox.y + (line.bbox.h || 0);
      if (lineY1 > regionY0) continue;

      const distancePt = regionY0 - lineY1;
      // Prefer lines within 120pt above the region (avoids picking up page headers)
      if (distancePt > 120) continue;

      candidates.push({ text: lineText, distance: distancePt, fontSize: line.spans?.[0]?.size || 10 });
    }
  }

  if (!candidates.length) return null;

  // Sort: closest first; break ties by larger font size (headings tend to be bigger)
  candidates.sort((a, b) => a.distance - b.distance || b.fontSize - a.fontSize);
  return candidates[0].text;
}

// Crop a specific region from a page using mupdf DrawDevice
function cropPageRegion(mupdf, doc, pageIdx, region, scale) {
  const page  = doc.loadPage(pageIdx);
  const dl    = page.toDisplayList();

  // Translate so the region's top-left maps to (0,0), then scale
  const tx = -region.x0 * scale;
  const ty = -region.y0 * scale;
  const w  = Math.round((region.x1 - region.x0) * scale);
  const h  = Math.round((region.y1 - region.y0) * scale);

  if (w < 10 || h < 10) { page.destroy(); return null; }

  const cropMatrix = [scale, 0, 0, scale, tx, ty];
  const pixmap     = new mupdf.Pixmap(mupdf.ColorSpace.DeviceRGB, [0, 0, w, h], false);
  pixmap.clear(255);
  const device = new mupdf.DrawDevice(cropMatrix, pixmap);
  dl.run(device, mupdf.Matrix.identity);
  device.close();

  const jpgBuf = pixmap.asJPEG(85, false);
  pixmap.destroy();
  page.destroy();

  return jpgBuf;
}

async function extractPDF(fileBuffer) {
  let textLayer = '';
  let pageCount = 0;

  // Step 1: Full text layer via pdf-parse (fast, free, no API)
  try {
    const parsed = await pdfParse(fileBuffer);
    pageCount    = parsed.numpages || 0;
    textLayer    = (parsed.text || '').replace(/\x00/g, '').replace(/\n{3,}/g, '\n\n').trim();
    console.log(`  PDF text layer: ${pageCount} pages, ${textLayer.length} chars`);
  } catch (e) {
    console.warn(`  pdf-parse error (non-fatal): ${e.message}`);
  }

  // Step 2: Smart visual extraction — crop & vision only chart/image regions
  const hasVision = !!(process.env.GEMINI_API_KEY || process.env.GROQ_API_KEY);
  const visualDescriptions = []; // { page, region, description }
  let visionCallCount = 0;

  if (hasVision && pageCount > 0) {
    try {
      const mupdfMod = await import('mupdf');
      const mupdf    = mupdfMod.default || mupdfMod;
      const doc      = mupdf.Document.openDocument(fileBuffer, 'application/pdf');
      const total    = doc.countPages();
      const scale    = 1.5;

      for (let i = 0; i < total; i++) {
        if (visionCallCount >= MAX_PAGES) break;

        try {
          const page     = doc.loadPage(i);
          const bounds   = page.getBounds(); // [x0,y0,x1,y1]
          const pageW    = bounds[2] - bounds[0];
          const pageH    = bounds[3] - bounds[1];
          const stext    = page.toStructuredText('preserve-images');
          const stJSON   = JSON.parse(stext.asJSON());
          page.destroy();

          const regions = detectVisualRegions(stJSON, pageH, pageW);
          if (regions.length === 0) continue;

          console.log(`  Page ${i+1}: found ${regions.length} visual region(s)`);

          for (const region of regions) {
            if (visionCallCount >= MAX_PAGES) break;

            // Heading comes directly from detectVisualRegions (heading-aware split)
            const nearestHeading = region.heading || null;
            const headingHint    = nearestHeading
              ? `\n\nNote: This chart/image appears under the heading: "${nearestHeading}". Use this as context when identifying the Section Name / Title.`
              : '';

            const jpgBuf = cropPageRegion(mupdf, doc, i, region, scale);
            if (!jpgBuf) continue;

            const jpgKB = jpgBuf.length / 1024;

            // Skip tiny/blank crops (likely just whitespace or thin dividers)
            if (jpgKB < 15) {
              console.log(`    Region y:${region.y0.toFixed(0)}-${region.y1.toFixed(0)} → ${jpgKB.toFixed(0)}KB, too small, skipping`);
              continue;
            }

            const regionLabel = nearestHeading
              ? `${nearestHeading} (y:${region.y0.toFixed(0)}-${region.y1.toFixed(0)})`
              : `y:${region.y0.toFixed(0)}-${region.y1.toFixed(0)}`;

            console.log(`    Region "${nearestHeading || 'unknown'}" y:${region.y0.toFixed(0)}-${region.y1.toFixed(0)} → ${jpgKB.toFixed(0)}KB → vision`);
            const b64  = Buffer.from(jpgBuf).toString('base64');
            const desc = await visionExtractWithContext(b64, 'image/jpeg', headingHint);

            if (desc) {
              visualDescriptions.push({
                page: i + 1,
                region: regionLabel,
                heading: nearestHeading || null,
                description: desc,
              });
              visionCallCount++;
            }
          }
        } catch (pageErr) {
          console.warn(`  Page ${i+1} visual detection failed (non-fatal): ${pageErr.message}`);
        }
      }

      doc.destroy();
      console.log(`  Visual extraction: ${visionCallCount} chart/image region(s) processed`);
    } catch (err) {
      console.warn(`  mupdf visual extraction failed (non-fatal): ${err.message}`);
    }
  }

  // Step 3: Combine text layer + visual descriptions
  const parts = [];
  if (textLayer.length > 50) {
    parts.push(`=== TEXT LAYER ===\n${textLayer}`);
  }
  if (visualDescriptions.length > 0) {
    const visualText = visualDescriptions
      .map(v => {
        const label = v.heading
          ? `--- Page ${v.page}: "${v.heading}" ---`
          : `--- Page ${v.page} Visual (${v.region}) ---`;
        return `${label}\n${v.description}`;
      })
      .join('\n\n');
    parts.push(`=== VISUAL CONTENT (Charts, Images, Diagrams) ===\n${visualText}`);
  }

  if (!parts.length) {
    return {
      fileType: 'pdf', text: '', hasText: false, pageCount,
      ocrProcessed: false, wordCount: 0,
      error: pageCount > 0
        ? `PDF has ${pageCount} pages but no extractable content. ${!hasVision ? 'Add GEMINI_API_KEY or GROQ_API_KEY for visual extraction.' : 'No text or visual content found.'}`
        : 'Could not parse PDF structure.',
    };
  }

  const combined = parts.join('\n\n');
  return {
    fileType: 'pdf', text: combined, hasText: true,
    pageCount, ocrProcessed: visionCallCount > 0,
    wordCount: combined.split(/\s+/).filter(Boolean).length,
    visionEnhanced: visionCallCount > 0,
    metadata: {
      textLayerChars:   textLayer.length,
      visualRegions:    visionCallCount,
    },
  };
}

async function extractImage(fileBuffer, mimeType) {
  const hasVision = !!(process.env.GEMINI_API_KEY || process.env.GROQ_API_KEY);

  if (!hasVision) {
    return {
      fileType: 'image', text: '', hasText: false, ocrProcessed: false,
      error: 'No vision API key configured. Add GEMINI_API_KEY or GROQ_API_KEY to extract text from images.',
    };
  }

  try {
    const b64    = fileBuffer.toString('base64');
    const mime   = mimeType || 'image/jpeg';
    const result = await visionExtract(b64, mime);

    if (result) {
      return {
        fileType: 'image', text: result, hasText: true,
        ocrProcessed: true, visionEnhanced: true,
        wordCount: result.split(/\s+/).filter(Boolean).length,
      };
    }

    return {
      fileType: 'image', text: '', hasText: false, ocrProcessed: true,
      error: 'Vision extraction returned no content. Check API key quota.',
    };
  } catch (err) {
    console.warn(`  Image vision failed (non-fatal): ${err.message}`);
    return {
      fileType: 'image', text: '', hasText: false, ocrProcessed: true,
      error: `Vision extraction failed: ${err.message}`,
    };
  }
}

async function extractDOCX(fileBuffer) {
  try {
    const result = await mammoth.extractRawText({ buffer: fileBuffer });
    const text   = (result.value || '').trim();
    if (!text) return { fileType: 'docx', text: '', hasText: false, error: 'DOCX is empty.' };
    return { fileType: 'docx', text, hasText: true, wordCount: text.split(/\s+/).filter(Boolean).length };
  } catch (err) {
    return { fileType: 'docx', text: '', hasText: false, error: `DOCX parse failed: ${err.message}` };
  }
}

// ═══ ROUTE ═══
app.post('/extract', async (req, res) => {
  const { fileUrl, originalName, mimetype: mimeType } = req.body;
  if (!fileUrl) return res.status(400).json({ error: 'No fileUrl provided' });

  // Download file — return graceful error if download fails
  let fileBuffer;
  try {
    const dlRes = await axios.get(fileUrl, {
      responseType: 'arraybuffer',
      timeout: 60000,
      maxContentLength: 100 * 1024 * 1024,
    });
    fileBuffer = Buffer.from(dlRes.data);
    if (!fileBuffer || fileBuffer.length === 0) throw new Error('Empty file downloaded');
    console.log(`  Downloaded: ${fileBuffer.length} bytes`);
  } catch (err) {
    console.error(`[Extraction] Download failed for ${originalName}: ${err.message}`);
    return res.json({
      fileType: 'unsupported', text: '', hasText: false,
      ocrProcessed: false, wordCount: 0, pageCount: null,
      error: `Could not download file: ${err.message}`,
    });
  }

  const origName = originalName || '';
  const ext = path.extname(origName).toLowerCase();
  console.log(`\n🔍 Extracting: ${origName} (${mimeType}, ${fileBuffer.length} bytes)`);

  try {
    let result;
    if (mimeType === 'application/pdf' || ext === '.pdf')
      result = await extractPDF(fileBuffer);
    else if (mimeType.includes('wordprocessingml') || mimeType === 'application/msword' || ['.docx','.doc'].includes(ext))
      result = await extractDOCX(fileBuffer);
    else if (mimeType.startsWith('image/') || ['.jpg','.jpeg','.png','.webp'].includes(ext))
      result = await extractImage(fileBuffer, mimeType);
    else
      result = { fileType: 'unsupported', text: '', hasText: false, error: `Unsupported file type: ${mimeType || ext}` };

    console.log(`  ✅ Done: hasText=${result.hasText} words=${result.wordCount||0} vision=${result.visionEnhanced||false}`);
    res.json(result);
  } catch (err) {
    // Catch-all: never 500 — always return a valid result
    console.error('[Extraction] Unexpected error:', err.message);
    res.json({
      fileType: 'unsupported', text: '', hasText: false,
      ocrProcessed: false, wordCount: 0,
      error: `Extraction error: ${err.message}`,
    });
  }
});

app.get('/', (_, res) => res.json({ service: 'extraction-service', status: 'ok' }));
app.get('/health', (_, res) => res.json({
  service: 'extraction-service', status: 'ok', port: PORT,
  vision: {
    gemini: !!process.env.GEMINI_API_KEY,
    groq:   !!process.env.GROQ_API_KEY,
  },
  pdfRenderer: 'mupdf (pure WASM)',
}));

app.use((err, req, res, next) => {
  console.error('[Extraction unhandled]', err.message);
  // Return valid extraction result even on unhandled errors
  res.json({ fileType: 'unsupported', text: '', hasText: false, error: err.message });
});

app.listen(PORT, '0.0.0.0', () => {
  const g = process.env.GEMINI_API_KEY ? '✅ Gemini' : '❌ No Gemini';
  const r = process.env.GROQ_API_KEY   ? '✅ Groq'   : '❌ No Groq';
  console.log(`🔍 Extraction Service :${PORT}  |  Vision: ${g}  ${r}`);
});
module.exports = app;
