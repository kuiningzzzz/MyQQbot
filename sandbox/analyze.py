"""Fixed OCR entry point, invoked only as an unprivileged sandbox user."""
import base64
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 25_000_000


def command(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def image_info(path, temp, page):
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((2400, 2400))
        target = temp / f"ocr-{page}.png"
        image.save(target)
        text = command(["tesseract", str(target), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "3"])
        image.thumbnail((1400, 1400))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        return {"page": page, "text": text[:20000], "image": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()}


def analyze(path, max_pages):
    with tempfile.TemporaryDirectory(prefix="ocr-") as temp:
        temp = Path(temp)
        if path.suffix != ".pdf":
            return {"pages": [image_info(path, temp, 1)], "truncated": False}
        info = command(["pdfinfo", str(path)])
        count = next(int(line.split(":", 1)[1]) for line in info.splitlines() if line.startswith("Pages:"))
        pages = []
        for number in range(1, min(count, max_pages) + 1):
            text = command(["pdftotext", "-f", str(number), "-l", str(number), "-layout", str(path), "-"])
            row = {"page": number, "text": text[:20000]}
            if len(text.strip()) < 30 or number <= 4:
                prefix = temp / f"page-{number}"
                command(["pdftoppm", "-f", str(number), "-l", str(number), "-singlefile", "-scale-to", "2200", "-png", str(path), str(prefix)])
                ocr = image_info(prefix.with_suffix(".png"), temp, number)
                row["text"] = ("[PDF文本层]\n" + text + "\n[OCR识别]\n" + ocr["text"])[:20000]
                if number <= 4:
                    row["image"] = ocr["image"]
            pages.append(row)
        return {"pages": pages, "page_count": count, "truncated": count > max_pages, "visual_pages": min(4, count)}


if __name__ == "__main__":
    print(json.dumps(analyze(Path(sys.argv[1]), int(sys.argv[2])), ensure_ascii=False))
