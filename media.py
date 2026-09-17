"""Room images and satisfying chaos-dissolving animations for Zero Space."""
from __future__ import annotations

from math import cos, pi, sin
from pathlib import Path
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'assets'
ROOMS = ['00-empty.png', '01-cozy.png', '02-window.png', '03-finished.png']
CHAOS_TOTAL = 166
MILESTONES = (18, 71, 166)


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return 1 - (1 - value) ** 3


def _room(stage: int, size: int) -> Image.Image:
    # Ten collectible rewards are grouped into four large visual room states.
    # The exact reward progress remains visible in the bot's room and shop text.
    visual_stage = 0 if stage <= 0 else 1 if stage <= 4 else 2 if stage <= 7 else 3
    image = Image.open(ASSETS / ROOMS[visual_stage]).convert('RGB')
    return image.resize((size, size), Image.Resampling.LANCZOS)


def visual_ratio(remaining: float) -> float:
    """Continuous shrink plus visible milestone drops when a room area clears."""
    remaining = max(0.0, min(CHAOS_TOTAL, remaining))
    earned = CHAOS_TOTAL - remaining
    bonus = .08 * (earned >= MILESTONES[0]) + .10 * (earned >= MILESTONES[1])
    return max(0.0, remaining/CHAOS_TOTAL - bonus)


def _cloud(remaining: float, size: int, glow: float = 0.0) -> Image.Image:
    ratio = visual_ratio(remaining)
    if ratio <= 0:
        return Image.new('RGBA', (size, size))
    asset = Image.open(ASSETS / 'chaos-mist.png').convert('RGBA')
    width = int(size * (0.06 + 0.55 * ratio ** 0.8))
    asset = asset.resize((width, width), Image.Resampling.LANCZOS)
    alpha = asset.getchannel('A').point(lambda a: int(a * (0.42 + 0.52 * ratio)))
    asset.putalpha(alpha)
    layer = Image.new('RGBA', (size, size))
    x = int(size * 0.06)
    y = int(size * (0.36 + 0.09 * (1-ratio)))
    if glow:
        glow_mask = alpha.filter(ImageFilter.GaussianBlur(max(3, int(size*.018))))
        gold = Image.new('RGBA', asset.size, (255, 188, 62, int(150*glow)))
        gold.putalpha(glow_mask.point(lambda a: int(a * min(1, glow))))
        layer.alpha_composite(gold, (x, y))
    layer.alpha_composite(asset, (x, y))
    return layer


def compose_room(stage: int, chaos_remaining: int, destination: Path, size: int = 1080) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    base = _room(stage, size)
    ratio = visual_ratio(chaos_remaining)
    if ratio:
        shade = Image.new('RGB', base.size, (31, 29, 34))
        base = Image.blend(base, shade, min(.20, .20*ratio))
        base = ImageEnhance.Brightness(base).enhance(1.0 - .08*ratio)
        base = Image.alpha_composite(base.convert('RGBA'), _cloud(chaos_remaining, size)).convert('RGB')
    base.save(destination, 'JPEG', quality=94, optimize=True, progressive=True,
              subsampling=0)
    return destination


def render_transition(stage: int, before: int, after: int, reward: int,
                      destination: Path, size: int = 512) -> Path:
    """Render a 1.8 s GIF: sparks fly in, cloud glows, then softly shrinks."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    frame_count = 22 if reward < 12 else 26
    rng = random.Random((stage+1)*100000 + before*100 + after)
    particles = [(rng.uniform(-.05,.06), rng.uniform(-.07,.07), rng.uniform(.7,1.4)) for _ in range(8+reward)]
    cloud_x, cloud_y = size*.27, size*.61
    start_x, start_y = size*.84, size*.72
    for index in range(frame_count):
        t = index/(frame_count-1)
        impact = _ease(min(1, t/.48))
        dissolve = _ease(max(0, (t-.42)/.58))
        current = before + (after-before)*dissolve
        base = _room(stage, size)
        ratio = visual_ratio(current)
        shade = Image.new('RGB', base.size, (31,29,34))
        base = Image.blend(base, shade, min(.20,.20*ratio))
        base = ImageEnhance.Brightness(base).enhance(1.0-.08*ratio)
        canvas = base.convert('RGBA')
        glow = sin(pi*max(0,min(1,(t-.26)/.55))) * min(1, .35+reward/12)
        canvas = Image.alpha_composite(canvas, _cloud(current, size, glow))
        sparks = Image.new('RGBA', canvas.size)
        draw = ImageDraw.Draw(sparks)
        for n,(ox,oy,speed) in enumerate(particles):
            local = max(0,min(1,impact*speed - n*.018))
            curve = sin(local*pi)*size*(oy+(.02 if n%2 else -.02))
            x = start_x + (cloud_x-start_x)*local + ox*size*sin(local*pi)
            y = start_y + (cloud_y-start_y)*local + curve
            radius = max(2, int(size*(.004+.004*reward/12)*(1-local*.35)))
            draw.ellipse((x-radius*3,y-radius*3,x+radius*3,y+radius*3), fill=(255,186,53,45))
            draw.ellipse((x-radius,y-radius,x+radius,y+radius), fill=(255,225,132,235))
        sparks = sparks.filter(ImageFilter.GaussianBlur(size*.0025))
        canvas = Image.alpha_composite(canvas, sparks)
        if t > .48:
            light = int(20 * dissolve * min(1,reward/7))
            canvas = ImageEnhance.Brightness(canvas.convert('RGB')).enhance(1+light/255).convert('RGBA')
        frames.append(canvas.convert('P', palette=Image.Palette.ADAPTIVE, colors=192))
    frames[0].save(destination, save_all=True, append_images=frames[1:], duration=75,
                   loop=0, optimize=False, disposal=2)
    return destination
