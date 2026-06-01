---
name: azure-ocr
description: Extract text from PDFs using Azure Document Intelligence (prebuilt-layout). Triggers when user asks to OCR a PDF, "이 PDF OCR해줘", "PDF 텍스트 뽑아줘", "extract text from PDF". Outputs page-by-page text with "=== Page N ===" markers, table structure restored as TSV.
---

# Azure Document Intelligence OCR

Use Azure Document Intelligence prebuilt-layout to extract text from PDFs.
Output format: `=== Page N ===` markers with TSV table structure restored.

## Required environment variables

- `AZURE_API_KEY` — Document Intelligence subscription key
- `AZURE_API_ENDPOINT` — `https://<resource>.cognitiveservices.azure.com/`

If either is missing, abort and tell the user.

## API version

`2024-11-30`

## Workflow

Document Intelligence is **asynchronous**: submit → poll → fetch.

### Step 1 — Submit PDF

```bash
ENDPOINT="${AZURE_API_ENDPOINT%/}"
MODEL="prebuilt-layout"
API_VER="2024-11-30"
FILE="/path/to/file.pdf"

OPERATION_URL=$(curl -sS -i -X POST \
  "${ENDPOINT}/documentintelligence/documentModels/${MODEL}:analyze?api-version=${API_VER}" \
  -H "Ocp-Apim-Subscription-Key: ${AZURE_API_KEY}" \
  -H "Content-Type: application/pdf" \
  --data-binary "@${FILE}" \
  | grep -i '^operation-location:' | tr -d '\r' | awk '{print $2}')

[[ -z "$OPERATION_URL" ]] && { echo "Submit failed"; exit 1; }
```

### Step 2 — Poll until succeeded

```bash
RAW="/tmp/ocr_raw_$$.json"
for i in $(seq 1 60); do
  curl -sS "$OPERATION_URL" -H "Ocp-Apim-Subscription-Key: ${AZURE_API_KEY}" -o "$RAW"
  STATUS=$(python3 -c "import json; print(json.load(open('$RAW')).get('status',''))" 2>/dev/null)
  echo "[$i] status: $STATUS"
  [[ "$STATUS" == "succeeded" || "$STATUS" == "failed" ]] && break
  sleep 3
done
[[ "$STATUS" != "succeeded" ]] && { echo "OCR failed: $STATUS"; exit 1; }
```

### Step 3 — Extract per-page text, bbox JSON, and page images

```python
import json, sys, os, pathlib

raw_path  = sys.argv[1]
pages_dir = sys.argv[2]
pdf_path  = sys.argv[3] if len(sys.argv) > 3 else None

result  = json.loads(pathlib.Path(raw_path).read_text())
analyze = result.get("analyzeResult", {})
pages   = analyze.get("pages", [])
tables  = analyze.get("tables", [])

# per-page table reconstruction
page_tables = {}
for tbl in tables:
    regions = tbl.get("boundingRegions", [])
    pg = regions[0]["pageNumber"] if regions else None
    if pg is None:
        continue
    cells = tbl.get("cells", [])
    if not cells:
        continue
    max_r = max(c["rowIndex"] for c in cells)
    max_c = max(c["columnIndex"] for c in cells)
    grid = [[""] * (max_c + 1) for _ in range(max_r + 1)]
    for cell in cells:
        grid[cell["rowIndex"]][cell["columnIndex"]] = cell.get("content", "").strip()
    tsv = "\n".join("\t".join(row) for row in grid)
    page_tables.setdefault(pg, []).append(tsv)

os.makedirs(pages_dir, exist_ok=True)
for page in pages:
    pg_num = page["pageNumber"]

    # ── .ocr.txt (기존) ──────────────────────────────────────────
    lines = page.get("lines", [])
    out = ["\n".join(ln.get("content", "") for ln in lines)]
    if pg_num in page_tables:
        out += ["", "--- tables ---", ""]
        for tsv in page_tables[pg_num]:
            out += [tsv, ""]
    fname = os.path.join(pages_dir, f"page_{pg_num:03d}.ocr.txt")
    pathlib.Path(fname).write_text("\n".join(out))
    print(f"  wrote {fname}")

    # ── .ocr.json (line bbox) ────────────────────────────────────
    lines_data = []
    for ln in lines:
        poly = ln.get("polygon", [])
        if len(poly) >= 8:
            xs, ys = poly[0::2], poly[1::2]
            bbox = [min(xs), min(ys), max(xs), max(ys)]
        else:
            bbox = []
        lines_data.append({"text": ln.get("content", ""), "bbox": bbox})
    ocr_json = {
        "page": pg_num,
        "width": page.get("width", 0),
        "height": page.get("height", 0),
        "unit": page.get("unit", "pixel"),
        "lines": lines_data,
    }
    json_fname = os.path.join(pages_dir, f"page_{pg_num:03d}.ocr.json")
    pathlib.Path(json_fname).write_text(
        json.dumps(ocr_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  wrote {json_fname}")

# ── .png (PyMuPDF) ───────────────────────────────────────────────
if pdf_path:
    try:
        import fitz  # pymupdf
        doc = fitz.open(pdf_path)
        mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI
        for pg in doc:
            pix = pg.get_pixmap(matrix=mat)
            png_fname = os.path.join(pages_dir, f"page_{pg.number + 1:03d}.png")
            pix.save(png_fname)
            print(f"  wrote {png_fname}")
        doc.close()
    except ImportError:
        print("  ⚠ pymupdf 없음 — PNG 생성 스킵 (pip install pymupdf)")
```

Run as:
```bash
python3 extract.py /tmp/ocr_raw_$$.json <pages_dir> "$FILE"
# e.g. python3 extract.py /tmp/ocr_raw_$$.json samples/<doc_id>_pages samples/<doc_id>.pdf
```

## Output

Save individual page files to `<same_dir>/<pdf_stem>_pages/page_NNN.ocr.txt`.
No combined `.ocr.txt` is created.

## Common errors

| HTTP | Meaning | Fix |
|------|---------|-----|
| 401 | Invalid key | Re-check `AZURE_API_KEY` |
| 403 | Quota exceeded | Wait or upgrade |
| 400 | Bad request | Verify PDF is not encrypted |
| 429 | Rate limit | Sleep and retry |
