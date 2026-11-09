"""The ride receipt PDF (API module 3). Moved verbatim from
komute/views.py ride_receipt_pdf_view; the website and the API both send it.
Callers decide who may see it (komute.services.rides.receipt_rides)."""
import io

from django.utils import timezone

from komute.services.common import display_name
from komute.services.geo import haversine_km
from komute.services.rides import local_dt


def build_receipt_pdf(ride):
    """(pdf bytes, ride reference like "KMT-2026-0042"). Prefetch the ride's
    stops, bookings__passenger and bookings__pickup_stop."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    # Paragraph parses its text as markup (and fetches <img> sources), so
    # user-typed names and places must be escaped before they go in.
    from xml.sax.saxutils import escape as _pdf_escape

    # ── build context (same logic as ride_summary_view) ──────────
    origin_city = ride.origin.split(',')[0].strip()
    distance_km = round(haversine_km(ride.origin_lat, ride.origin_lng, ride.dest_lat, ride.dest_lng))

    duration = ''
    if ride.arrive_at and ride.depart_at:
        delta   = ride.arrive_at - ride.depart_at
        hours   = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    active_bookings = [b for b in ride.bookings.all() if b.status not in ('cancelled', 'rejected')]
    passenger_count = sum(b.seats for b in active_bookings)

    CO2_PER_KM_KG = 0.12
    co2_saved_kg  = round(passenger_count * distance_km * CO2_PER_KM_KG, 1)

    total_cents = 0
    enriched_bookings = []
    for b in active_bookings:
        name  = display_name(b.passenger)
        pickup_name = b.pickup_stop.location.split(',')[0].strip() if b.pickup_stop else origin_city
        total_cents += b.agreed_price_cents
        enriched_bookings.append({
            'name':         name,
            'seats':        b.seats,
            'pickup':       pickup_name,
            'contribution': f"${b.agreed_price_cents / 100:.2f}",
        })

    ride_ref = f"KMT-{ride.depart_at.year}-{ride.id:04d}"
    driver_name = display_name(ride.driver)

    # Convert to ride's local timezone for display in the PDF
    _depart_local = local_dt(ride, ride.depart_at)
    _arrive_local = local_dt(ride, ride.arrive_at) if ride.arrive_at else None
    _tz_suffix    = (' ' + _depart_local.tzname()) if ride.tz and _depart_local.tzname() else ' UTC'

    # ── PDF generation ────────────────────────────────────────────
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title='Komute Ride Receipt',
        author='Komute',
    )

    GREEN  = colors.HexColor('#0D7810')
    DARK   = colors.HexColor('#121212')
    MUTED  = colors.HexColor('#5a7060')
    LIGHT  = colors.HexColor('#E9F7EE')
    WHITE  = colors.white

    styles = getSampleStyleSheet()
    def _s(name, **kw):
        base = styles[name] if name in styles else styles['Normal']
        return ParagraphStyle(name + '_custom', parent=base, **kw)

    title_style   = _s('Normal', fontName='Helvetica-Bold', fontSize=22, textColor=GREEN, spaceAfter=2)
    sub_style     = _s('Normal', fontName='Helvetica',      fontSize=10, textColor=MUTED, spaceAfter=6)
    head_style    = _s('Normal', fontName='Helvetica-Bold', fontSize=11, textColor=DARK,  spaceAfter=4)
    body_style    = _s('Normal', fontName='Helvetica',      fontSize=10, textColor=DARK)
    muted_style   = _s('Normal', fontName='Helvetica',      fontSize=9,  textColor=MUTED)
    right_style   = _s('Normal', fontName='Helvetica',      fontSize=10, textColor=DARK, alignment=TA_RIGHT)
    total_style   = _s('Normal', fontName='Helvetica-Bold', fontSize=11, textColor=GREEN, alignment=TA_RIGHT)
    center_style  = _s('Normal', fontName='Helvetica',      fontSize=9,  textColor=MUTED, alignment=TA_CENTER)
    green_bold    = _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=GREEN)

    W = A4[0] - 40*mm  # usable width

    story = []

    # ── Header ──
    header_data = [[
        Paragraph('KOMÜTE', title_style),
        Paragraph(f'Ride Receipt<br/><font size="9" color="#5a7060">{ride_ref}</font>', _s('Normal', fontName='Helvetica-Bold', fontSize=14, textColor=DARK, alignment=TA_RIGHT)),
    ]]
    header_tbl = Table(header_data, colWidths=[W * 0.5, W * 0.5])
    header_tbl.setStyle(TableStyle([
        ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
        ('BACKGROUND',  (0,0), (-1,-1), LIGHT),
        ('TOPPADDING',  (0,0), (-1,-1), 10),
        ('BOTTOMPADDING',(0,0),(-1,-1), 10),
        ('LEFTPADDING', (0,0), (-1,-1), 14),
        ('RIGHTPADDING',(0,0), (-1,-1), 14),
        ('ROUNDEDCORNERS', (0,0), (-1,-1), [8, 8, 8, 8]),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 5*mm))
    story.append(Paragraph(f'Issued on {timezone.now().strftime("%B %d, %Y")}  ·  Driver: {_pdf_escape(driver_name)}', sub_style))
    story.append(HRFlowable(width=W, color=colors.HexColor('#c2dfc8'), thickness=1, spaceAfter=5*mm))

    # ── Trip Overview ──
    story.append(Paragraph('Trip Overview', head_style))
    trip_data = [
        ['Route',      f'{ride.origin}  →  {ride.destination}'],
        ['Date',       _depart_local.strftime('%A, %B %d, %Y').replace(' 0', ' ')],
        ['Departed',   _depart_local.strftime('%I:%M %p').lstrip('0') + _tz_suffix],
        ['Arrived',    _arrive_local.strftime('%I:%M %p').lstrip('0') + _tz_suffix if _arrive_local else '—'],
        ['Duration',   duration or '—'],
        ['Distance',   f'{distance_km} km'],
        ['Driver',     driver_name],
        ['Status',     ride.get_status_display()],
        ['Passengers', f'{passenger_count} passenger{"s" if passenger_count != 1 else ""}'],
    ]
    trip_tbl = Table(trip_data, colWidths=[W * 0.28, W * 0.72])
    trip_tbl.setStyle(TableStyle([
        ('FONTNAME',    (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME',    (1,0), (1,-1), 'Helvetica'),
        ('FONTSIZE',    (0,0), (-1,-1), 10),
        ('TEXTCOLOR',   (0,0), (0,-1), MUTED),
        ('TEXTCOLOR',   (1,0), (1,-1), DARK),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [WHITE, LIGHT]),
        ('TOPPADDING',  (0,0), (-1,-1), 6),
        ('BOTTOMPADDING',(0,0),(-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING',(0,0),(-1,-1), 10),
        ('GRID',        (0,0), (-1,-1), 0.5, colors.HexColor('#e0ede3')),
        ('ROUNDEDCORNERS', (0,0), (-1,-1), [6, 6, 6, 6]),
    ]))
    story.append(trip_tbl)
    story.append(Spacer(1, 5*mm))

    # ── Passenger & Cost Breakdown ──
    story.append(Paragraph('Cost-Share Breakdown', head_style))
    cost_header = [
        Paragraph('Passenger', _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE)),
        Paragraph('Seats',     _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE, alignment=TA_CENTER)),
        Paragraph('Pickup',    _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE)),
        Paragraph('Amount',    _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE, alignment=TA_RIGHT)),
    ]
    cost_rows = [cost_header]
    for b in enriched_bookings:
        cost_rows.append([
            Paragraph(_pdf_escape(b['name']), body_style),
            Paragraph(str(b['seats']),   _s('Normal', fontName='Helvetica', fontSize=10, textColor=DARK, alignment=TA_CENTER)),
            Paragraph(_pdf_escape(b['pickup']), muted_style),
            Paragraph(b['contribution'], right_style),
        ])
    cost_rows.append([
        Paragraph('Total received', green_bold),
        '', '',
        Paragraph(f"${total_cents / 100:.2f}", total_style),
    ])

    cost_tbl = Table(cost_rows, colWidths=[W*0.38, W*0.12, W*0.32, W*0.18])
    cost_style_cmds = [
        ('BACKGROUND',   (0,0), (-1,0),  GREEN),
        ('ROWBACKGROUNDS',(0,1), (-1,-2), [WHITE, LIGHT]),
        ('BACKGROUND',   (0,-1),(-1,-1), colors.HexColor('#E9F7EE')),
        ('TOPPADDING',   (0,0), (-1,-1), 7),
        ('BOTTOMPADDING',(0,0), (-1,-1), 7),
        ('LEFTPADDING',  (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ('GRID',         (0,0), (-1,-1), 0.5, colors.HexColor('#e0ede3')),
        ('SPAN',         (1,-1),(2,-1)),
        ('LINEABOVE',    (0,-1),(-1,-1), 1.5, GREEN),
    ]
    cost_tbl.setStyle(TableStyle(cost_style_cmds))
    story.append(cost_tbl)
    story.append(Spacer(1, 5*mm))

    # ── Eco Impact ──
    story.append(Paragraph('Eco Impact', head_style))
    eco_data = [[
        Paragraph(f'<b>{co2_saved_kg} kg</b><br/><font size="8" color="#5a7060">CO₂ Saved</font>', _s('Normal', fontName='Helvetica', fontSize=11, textColor=GREEN, alignment=TA_CENTER)),
        Paragraph(f'<b>{passenger_count}</b><br/><font size="8" color="#5a7060">Passengers</font>', _s('Normal', fontName='Helvetica', fontSize=11, textColor=DARK, alignment=TA_CENTER)),
        Paragraph(f'<b>{distance_km} km</b><br/><font size="8" color="#5a7060">Distance</font>', _s('Normal', fontName='Helvetica', fontSize=11, textColor=DARK, alignment=TA_CENTER)),
    ]]
    eco_tbl = Table(eco_data, colWidths=[W/3, W/3, W/3])
    eco_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), LIGHT),
        ('TOPPADDING',    (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('INNERGRID',     (0,0), (-1,-1), 0.5, colors.HexColor('#c2dfc8')),
        ('BOX',           (0,0), (-1,-1), 0.5, colors.HexColor('#c2dfc8')),
    ]))
    story.append(eco_tbl)
    story.append(Spacer(1, 6*mm))

    # ── Footer note ──
    story.append(HRFlowable(width=W, color=colors.HexColor('#c2dfc8'), thickness=1, spaceAfter=4*mm))
    story.append(Paragraph(
        'Komüte is free to use. Amounts above were paid directly between driver and passengers. '
        'Komüte did not process or hold any funds — it simply connected the parties.',
        _s('Normal', fontName='Helvetica-Oblique', fontSize=8, textColor=MUTED),
    ))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(f'© {timezone.now().year} Komüte by Indus Network LLC  ·  {ride_ref}', center_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue(), ride_ref
