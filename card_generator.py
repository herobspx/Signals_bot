from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ====== إعدادات ======
WIDTH, HEIGHT = 1080, 1920

def generate_card(data):
    # ====== الخلفية ======
    bg = Image.open("card_bg.png").resize((WIDTH, HEIGHT))
    bg = bg.filter(ImageFilter.GaussianBlur(8))

    img = Image.new("RGB", (WIDTH, HEIGHT))
    img.paste(bg, (0, 0))

    draw = ImageDraw.Draw(img)

    # ====== الخطوط ======
    font_big = ImageFont.truetype("Arial.ttf", 120)
    font_medium = ImageFont.truetype("Arial.ttf", 50)
    font_small = ImageFont.truetype("Arial.ttf", 40)

    # ====== بيانات ======
    contract = data["contract"]  # SPXW 3900 08 Mar 24 (W) Call
    price = data["price"]        # 3.90
    change = data["change"]      # +1.06
    percent = data["percent"]    # +37.40%

    bid = data["bid"]
    ask = data["ask"]
    open_p = data["open"]
    high = data["high"]
    mid = data["mid"]
    volume = data["volume"]

    # ====== سهم الرجوع ======
    draw.text((50, 80), "<", font=font_medium, fill="white")

    # ====== العقد ======
    draw.text((120, 90), contract, font=font_medium, fill="white")

    # ====== السعر ======
    draw.text((120, 400), price, font=font_big, fill=(0, 255, 120))

    draw.text(
        (120, 550),
        f"▲ {change}  {percent}",
        font=font_medium,
        fill=(0, 255, 120),
    )

    # ====== جدول اليمين بدون إطار ======
    x = 700
    y = 300
    gap = 120

    # Bid / Ask
    draw.text((x, y), "Bid", font=font_small, fill="white")
    draw.text((x, y + 50), bid, font=font_medium, fill=(0, 255, 120))
    draw.text((x, y + 100), "33", font=font_small, fill="white")

    draw.text((x + 180, y), "Ask", font=font_small, fill="white")
    draw.text((x + 180, y + 50), ask, font=font_medium, fill=(255, 80, 80))
    draw.text((x + 180, y + 100), "40", font=font_small, fill="white")

    # Open / High
    y += gap
    draw.text((x, y), "Open", font=font_small, fill="white")
    draw.text((x, y + 50), open_p, font=font_medium, fill="white")

    draw.text((x + 180, y), "High", font=font_small, fill="white")
    draw.text((x + 180, y + 50), high, font=font_medium, fill="white")

    # Mid / Volume
    y += gap
    draw.text((x, y), "Mid", font=font_small, fill="white")
    draw.text((x, y + 50), mid, font=font_medium, fill="white")

    draw.text((x + 180, y), "Volume", font=font_small, fill="white")
    draw.text((x + 180, y + 50), volume, font=font_medium, fill="white")

    # ====== النص الشفاف ======
    overlay = Image.new("RGBA", img.size, (255,255,255,0))
    overlay_draw = ImageDraw.Draw(overlay)

    overlay_draw.text(
        (WIDTH//2 - 250, HEIGHT//2),
        "BAM | SPX",
        font=ImageFont.truetype("Arial.ttf", 120),
        fill=(255,255,255,60)
    )

    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    # ====== حفظ ======
    img = img.convert("RGB")
    img.save("card.png")

    return "card.png"
