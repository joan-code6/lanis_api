"""Render the public status card used by Open Graph link previews."""
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont


def render_status_image(data: dict) -> bytes:
    scale = 2
    image = Image.new('RGB', (1200 * scale, 630 * scale), '#0a0a0a')
    draw = ImageDraw.Draw(image)
    font_path = Path(__file__).parent / 'assets' / 'DejaVuSans.ttf'
    font = ImageFont.truetype(str(font_path), 44 * scale)
    draw.text((48 * scale, 64 * scale), 'Verfügbarkeit', fill='#fafafa', font=font)
    percent = data.get('summary_windows', {}).get('30d', {}).get('uptime_percent')
    label = '—' if percent is None else f'{percent:.2f}'.replace('.', ',') + ' %'
    draw.text((1152 * scale, 64 * scale), label, anchor='rt', fill='#fafafa', font=font)
    colors = {'up': '#10b981', 'degraded': '#f59e0b', 'down': '#f43f5e', 'unknown': '#525252'}
    days = data.get('daily', [])[-30:]
    days = [{'status': 'unknown'}] * (30 - len(days)) + days
    for index, day in enumerate(days):
        status = day['status']
        # Match the website: incident overlap can raise a day's severity.
        if day.get('day'):
            start = datetime.fromisoformat(day['day']).replace(tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            severity = {'unknown': 0, 'up': 1, 'degraded': 2, 'down': 3}
            for incident in data.get('incidents', []):
                begin = datetime.fromisoformat(incident['started_at'].replace('Z', '+00:00'))
                finish = datetime.fromisoformat((incident.get('resolved_at') or data['generated_at']).replace('Z', '+00:00'))
                if begin < end and finish > start and severity.get(incident['status'], 0) > severity.get(status, 0):
                    status = incident['status']
        x = 48 + (index % 15) * 74
        y = 250 + (index // 15) * 88
        draw.ellipse(tuple(int(v * scale) for v in (x, y, x + 62, y + 62)), fill=colors.get(status, colors['unknown']))
    output = BytesIO()
    image.resize((1200, 630), Image.Resampling.LANCZOS).save(output, format='PNG')
    return output.getvalue()
