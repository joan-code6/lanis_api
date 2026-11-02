"""Render the public status card used by Open Graph link previews."""
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont


def render_status_image(data: dict) -> bytes:
    scale = 2
    image = Image.new('RGB', (1200 * scale, 642 * scale), '#0a0a0a')
    draw = ImageDraw.Draw(image)
    font_path = Path(__file__).parent / 'assets' / 'Outfit-SemiBold.ttf'
    font = ImageFont.truetype(str(font_path), 42 * scale)
    small_font = ImageFont.truetype(str(font_path), 24 * scale)
    draw.text((64 * scale, 30 * scale), 'Schulportal Hessen', fill='#a3a3a3', font=small_font, anchor='lt')
    current = data.get('current') or {}
    state = current.get('status', 'unknown')
    # Never present an old successful measurement as a live healthy state.
    try:
        checked = datetime.fromisoformat(str(current.get('checked_at')).replace('Z', '+00:00'))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        stale_after = data.get('measurement', {}).get('stale_after_seconds', 600)
        if current.get('stale') or age < -60 or age > stale_after:
            state = 'unknown'
    except (ValueError, TypeError):
        state = 'unknown'
    live_labels = {
        'up': ('Jetzt erreichbar', '#10b981'),
        'degraded': ('Jetzt eingeschränkt', '#f59e0b'),
        'down': ('Aktuell nicht erreichbar', '#f43f5e'),
        'unknown': ('Aktueller Status unbekannt', '#a3a3a3'),
    }
    live_label, live_color = live_labels.get(state, live_labels['unknown'])
    draw.ellipse((64 * scale, 144 * scale, 76 * scale, 156 * scale), fill=live_color)
    draw.text((88 * scale, 137 * scale), live_label, fill=live_color, font=small_font, anchor='lt')
    draw.text((64 * scale, 72 * scale), 'Verfügbarkeit', fill='#fafafa', font=font, anchor='lt')
    percent = data.get('summary_windows', {}).get('90d', {}).get('uptime_percent')
    label = '—' if percent is None else f'{percent:.2f}'.replace('.', ',') + ' %'
    label_width = draw.textlength(label, font=font)
    draw.text((1136 * scale - label_width, 72 * scale), label, anchor='lt', fill='#fafafa', font=font)
    colors = {'up': '#10b981', 'degraded': '#f59e0b', 'down': '#f43f5e', 'unknown': '#525252'}
    days = data.get('daily', [])[-90:]
    days = [{'status': 'unknown'}] * (90 - len(days)) + days
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
        x = 64 + (index % 18) * (1022 / 17)
        y = 210 + (index // 18) * 66
        draw.ellipse(tuple(int(v * scale) for v in (x, y, x + 50, y + 50)), fill=colors.get(status, colors['unknown']))
    output = BytesIO()
    image.resize((1200, 630), Image.Resampling.LANCZOS).save(output, format='PNG')
    return output.getvalue()
