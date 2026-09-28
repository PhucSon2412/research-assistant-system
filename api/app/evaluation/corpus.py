"""Synthetic evaluation corpus: reproducible PDF rendering and file checksums."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "evaluation" / "rag"
MAX_LINE_POINTS = 550  # 612 pt page, 35 pt left margin, keeps a right margin
FONT_SIZE = 11
LINE_STEP = 15


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def render_pdf(pages: list[dict]) -> bytes:
    """Draw text lines and text-free shapes; ``invariant`` fixes dates and IDs."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas  # dev dependency, only needed to rebuild

    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), pageCompression=0, invariant=1)
    for page in pages:
        document.setFont("Helvetica", FONT_SIZE)
        y = 750
        for line in page["lines"]:
            if stringWidth(line, "Helvetica", FONT_SIZE) > MAX_LINE_POINTS:
                raise ValueError(f"PDF source line wider than {MAX_LINE_POINTS} pt: {line!r}")
            document.drawString(35, y, line)
            y -= LINE_STEP
        document.setFillGray(0.6)
        for x, bottom, width, height in page.get("shapes", []):
            document.rect(x, bottom, width, height, stroke=1, fill=1)
        document.setFillGray(0)
        document.showPage()
    document.save()
    return output.getvalue()


def pdf_sources(root: Path = DEFAULT_ROOT) -> dict:
    return json.loads((root / "corpus" / "pdf_sources.json").read_text(encoding="utf-8"))["papers"]


def build_pdfs(root: Path = DEFAULT_ROOT, *, write: bool = True) -> dict[str, str]:
    """Render every PDF source; return ``{relative file: sha256}`` of the rendered bytes."""
    rendered = {}
    for spec in pdf_sources(root).values():
        data = render_pdf(spec["pages"])
        if write:
            (root / "corpus" / spec["file"]).write_bytes(data)
        rendered[spec["file"]] = sha256_bytes(data)
    return rendered
