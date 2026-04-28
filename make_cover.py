"""
封面生成器 — 给无文字底图叠加书名与作者名，输出 600×800 成品封面。
用法：
  python make_cover.py --base <底图路径> --title <书名行1|书名行2> --author <作者> [--output <输出路径>]
"""

import os
import argparse
from PIL import Image, ImageDraw, ImageFont


def pick_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_cover(base_img_path, title_lines, author_name, output_path,
               font_title_size=52, font_author_size=22):
    FONT_TITLE_CANDIDATES = [
        r"C:\Windows\Fonts\FZYTK.TTF",
        r"C:\Windows\Fonts\FZSTK.TTF",
        r"C:\Windows\Fonts\Noto Sans SC Bold (TrueType).otf",
        r"C:\Windows\Fonts\Dengb.ttf",
    ]
    FONT_AUTHOR_CANDIDATES = [
        r"C:\Windows\Fonts\Noto Sans SC Medium (TrueType).otf",
        r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf",
        r"C:\Windows\Fonts\Deng.ttf",
    ]

    img = Image.open(base_img_path).convert("RGBA")
    w, h = img.size
    target_ratio = 600 / 800
    current_ratio = w / h
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    img = img.resize((600, 800), Image.LANCZOS)

    overlay = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    for y in range(450, 800):
        alpha = int(180 * (y - 450) / (800 - 450))
        draw_overlay.rectangle([(0, y), (600, y + 1)], fill=(20, 10, 30, alpha))
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)
    font_title = pick_font(FONT_TITLE_CANDIDATES, font_title_size)
    font_author = pick_font(FONT_AUTHOR_CANDIDATES, font_author_size)

    def draw_text_with_outline(draw_obj, pos, text, font, fill, outline_fill, outline_width=2):
        x, y = pos
        for dx in range(-outline_width, outline_width + 1):
            for dy in range(-outline_width, outline_width + 1):
                if dx != 0 or dy != 0:
                    draw_obj.text((x + dx, y + dy), text, font=font, fill=outline_fill)
        draw_obj.text((x, y), text, font=font, fill=fill)

    y_start = 570 - (len(title_lines) - 1) * 35
    for i, line in enumerate(title_lines):
        fs = font_title_size if i < 2 else font_title_size - 8
        f = pick_font(FONT_TITLE_CANDIDATES, fs)
        bbox = draw.textbbox((0, 0), line, font=f)
        tw = bbox[2] - bbox[0]
        x = (600 - tw) // 2
        y = y_start + i * (fs + 15)
        draw_text_with_outline(draw, (x, y), line, f,
                                fill=(255, 255, 255, 255),
                                outline_fill=(30, 10, 50, 200), outline_width=3)

    author_text = f"{author_name}  著"
    bbox_a = draw.textbbox((0, 0), author_text, font=font_author)
    tw_a = bbox_a[2] - bbox_a[0]
    x_a = (600 - tw_a) // 2
    y_a = y_start + len(title_lines) * 65
    draw_text_with_outline(draw, (x_a, y_a), author_text, font_author,
                            fill=(220, 210, 230, 255),
                            outline_fill=(20, 5, 40, 180), outline_width=2)

    img_rgb = img.convert("RGB")
    img_rgb.save(output_path, "PNG", quality=95)
    print(f"封面已保存至: {output_path}")
    print(f"尺寸: {img_rgb.size}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="小说封面生成器")
    parser.add_argument("--base", required=True, help="底图路径（PNG/JPG）")
    parser.add_argument("--title", required=True, help="书名（多行用 | 分隔）")
    parser.add_argument("--author", required=True, help="作者名")
    parser.add_argument("--output", default="cover_final.png", help="输出路径")
    args = parser.parse_args()
    titles = [t.strip() for t in args.title.split("|") if t.strip()]
    make_cover(args.base, titles, args.author, args.output)
