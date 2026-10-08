from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

doc = Document()

# ── Page margins ──
section = doc.sections[0]
section.page_width  = Inches(8.5)
section.page_height = Inches(11)
section.left_margin = section.right_margin = Inches(1)
section.top_margin  = section.bottom_margin = Inches(1)

# ── Styles ──
styles = doc.styles

def set_heading_style(paragraph, text, level, color_hex="0D7810"):
    run = paragraph.runs[0] if paragraph.runs else paragraph.add_run(text)
    run.font.name  = "Calibri"
    run.font.bold  = True
    run.font.color.rgb = RGBColor.from_string(color_hex)
    sizes = {1: 22, 2: 16, 3: 13, 4: 11}
    run.font.size = Pt(sizes.get(level, 11))

def add_heading(doc, text, level=1, color="0D7810"):
    p = doc.add_heading(text, level=level)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in p.runs:
        run.font.color.rgb = RGBColor.from_string(color)
        run.font.name = "Calibri"
    return p

def add_para(doc, text, bold=False, italic=False, size=10.5, color=None, indent=False):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.name  = "Calibri"
    run.font.size  = Pt(size)
    run.font.bold  = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    if indent:
        p.paragraph_format.left_indent = Inches(0.3)
    return p

def add_bullet(doc, text, level=0):
    p = doc.add_paragraph(style="List Bullet")
    run = p.add_run(text)
    run.font.name = "Calibri"
    run.font.size = Pt(10.5)
    p.paragraph_format.left_indent = Inches(0.3 * (level + 1))
    return p

def add_table(doc, headers, rows, col_widths=None):
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT

    # header row
    hdr_cells = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr_cells[i].text = h
        hdr_cells[i].paragraphs[0].runs[0].font.bold  = True
        hdr_cells[i].paragraphs[0].runs[0].font.name  = "Calibri"
        hdr_cells[i].paragraphs[0].runs[0].font.size  = Pt(10)
        hdr_cells[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(255, 255, 255)
        tc = hdr_cells[i]._tc
        tcPr = tc.get_or_add_tcPr()
        shd = OxmlElement('w:shd')
        shd.set(qn('w:val'), 'clear')
        shd.set(qn('w:color'), 'auto')
        shd.set(qn('w:fill'), '0D7810')
        tcPr.append(shd)

    # data rows
    for ri, row in enumerate(rows):
        cells = table.rows[ri + 1].cells
        fill = "F0FAF1" if ri % 2 == 0 else "FFFFFF"
        for ci, val in enumerate(row):
            cells[ci].text = str(val)
            cells[ci].paragraphs[0].runs[0].font.name = "Calibri"
            cells[ci].paragraphs[0].runs[0].font.size = Pt(10)
            tc = cells[ci]._tc
            tcPr = tc.get_or_add_tcPr()
            shd = OxmlElement('w:shd')
            shd.set(qn('w:val'), 'clear')
            shd.set(qn('w:color'), 'auto')
            shd.set(qn('w:fill'), fill)
            tcPr.append(shd)

    if col_widths:
        for i, w in enumerate(col_widths):
            for row in table.rows:
                row.cells[i].width = Inches(w)
    return table

def add_code(doc, code_text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent  = Inches(0.3)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(4)
    run = p.add_run(code_text)
    run.font.name = "Courier New"
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x0C, 0x63, 0xB4)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), 'EFF6FF')
    p._p.get_or_add_pPr().append(shd)
    return p

def page_break(doc):
    doc.add_page_break()

# ══════════════════════════════════════════════════════
#  COVER PAGE
# ══════════════════════════════════════════════════════
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("\n\n\n\nKomüte")
run.font.name  = "Calibri"
run.font.size  = Pt(42)
run.font.bold  = True
run.font.color.rgb = RGBColor.from_string("0D7810")

p2 = doc.add_paragraph()
p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
r2 = p2.add_run("Rideshare Platform — Technical Documentation")
r2.font.name  = "Calibri"
r2.font.size  = Pt(16)
r2.font.color.rgb = RGBColor.from_string("0C63B4")

doc.add_paragraph()
p3 = doc.add_paragraph()
p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
r3 = p3.add_run(f"Version 1.0  ·  {datetime.date.today().strftime('%B %Y')}")
r3.font.name  = "Calibri"
r3.font.size  = Pt(11)
r3.font.color.rgb = RGBColor(90, 112, 96)

page_break(doc)

# ══════════════════════════════════════════════════════
#  1. PROJECT OVERVIEW
# ══════════════════════════════════════════════════════
add_heading(doc, "1. Project Overview", 1)
add_para(doc,
    "Komüte is a web-based peer-to-peer ridesharing platform built for the Canadian market. "
    "It connects verified drivers who have empty seats on their planned trips with riders looking "
    "for affordable, eco-friendly travel. The platform supports real-time price negotiation via "
    "WebSocket chat, ride scheduling, recurring rides, and multi-stop routes.")

doc.add_paragraph()
add_heading(doc, "Key Goals", 2, "0C63B4")
add_bullet(doc, "Reduce per-trip carbon emissions by filling empty car seats.")
add_bullet(doc, "Provide affordable intercity and intra-city travel alternatives.")
add_bullet(doc, "Ensure safety through driver and vehicle verification.")
add_bullet(doc, "Enable transparent, fair pricing via in-app negotiation.")

page_break(doc)

# ══════════════════════════════════════════════════════
#  2. TECH STACK
# ══════════════════════════════════════════════════════
add_heading(doc, "2. Technology Stack", 1)

add_table(doc,
    ["Layer", "Technology", "Purpose"],
    [
        ["Backend Framework", "Django 5.2", "MVC web framework, ORM, admin, auth"],
        ["Real-time / WebSocket", "Django Channels 4 + Daphne", "Live chat & price negotiation"],
        ["WebSocket Protocol", "ASGI (Daphne server)", "Async server gateway interface"],
        ["Channel Layer", "In-Memory Channel Layer", "Message passing between consumers"],
        ["Database", "SQLite 3", "Relational data store (dev/prod)"],
        ["Auth", "django-allauth", "Email verification, OAuth support"],
        ["Geocoding API", "Photon (Komoot)", "Address autocomplete with lat/lng"],
        ["Routing API", "OSRM (project-osrm.org)", "Driving distance & duration"],
        ["Proximity Search", "Haversine formula (Python)", "Radius-based ride filtering"],
        ["Image Processing", "Pillow", "Resize & compress uploads"],
        ["Config / Secrets", "python-decouple", "Env variable management"],
        ["Email", "Hostinger SMTP", "Verification & notification emails"],
        ["Frontend", "Vanilla HTML/CSS/JS", "No frontend framework"],
        ["Fonts", "Google Fonts (Poppins, DM Sans)", "UI typography"],
    ],
    col_widths=[1.8, 2.0, 2.7]
)

page_break(doc)

# ══════════════════════════════════════════════════════
#  3. PROJECT STRUCTURE
# ══════════════════════════════════════════════════════
add_heading(doc, "3. Project Structure", 1)

add_code(doc,
"KomuteProject/\n"
"├── config/\n"
"│   ├── settings.py        # Django settings\n"
"│   ├── urls.py            # Root URL dispatcher\n"
"│   ├── asgi.py            # ASGI app (HTTP + WebSocket)\n"
"│   └── wsgi.py\n"
"├── komute/                # Main application\n"
"│   ├── models.py          # Database models\n"
"│   ├── views.py           # All HTTP view logic\n"
"│   ├── urls.py            # App URL patterns\n"
"│   ├── consumers.py       # WebSocket consumer (chat)\n"
"│   ├── routing.py         # WebSocket URL routing\n"
"│   ├── chat_tokens.py     # Signed token generator/resolver\n"
"│   ├── signals.py         # Post-save signals (email confirm)\n"
"│   ├── admin.py           # Django admin registrations\n"
"│   └── migrations/        # Database migrations\n"
"├── templates/             # HTML templates\n"
"├── static/                # CSS, JS, images\n"
"├── media/                 # User-uploaded files\n"
"└── manage.py"
)

page_break(doc)

# ══════════════════════════════════════════════════════
#  4. DATABASE MODELS
# ══════════════════════════════════════════════════════
add_heading(doc, "4. Database Models", 1)

# 4.1 CustomUser
add_heading(doc, "4.1  CustomUser  (table: users)", 2, "0C63B4")
add_para(doc, "Extends Django's AbstractUser. Email is the unique login field (username is replaced).")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["full_name", "TextField", "User's display name"],
        ["email", "EmailField (unique)", "Login credential and primary contact"],
        ["email_verified", "BooleanField", "True after allauth email confirmation"],
        ["pending_email", "EmailField (null)", "Stores new email during change flow"],
        ["phone", "CharField", "E.164 format, optional"],
        ["role", "TextField", "rider | driver | admin"],
        ["dob", "DateField", "Date of birth (must be 18+)"],
        ["gender", "TextField", "male | female | other"],
        ["bio", "TextField", "Short profile bio"],
        ["avatar_url / selfie_url", "TextField", "Profile picture paths"],
        ["driver_verified", "BooleanField", "Admin-set driver verification flag"],
        ["license_number", "TextField", "Driver's licence number"],
        ["license_province", "TextField", "Issuing province"],
        ["license_expiry", "DateField", "Licence expiry date"],
        ["license_front_url", "TextField", "Uploaded licence image path"],
        ["avg_rating", "FloatField", "Computed average rating"],
        ["total_rides", "IntegerField", "Total completed rides"],
        ["created_at / updated_at", "DateTimeField", "Audit timestamps"],
    ],
    col_widths=[1.8, 1.5, 3.2]
)

doc.add_paragraph()

# 4.2 Vehicle
add_heading(doc, "4.2  Vehicle  (table: vehicles)", 2, "0C63B4")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["owner", "FK → CustomUser", "Driver who owns the vehicle"],
        ["make / model", "CharField", "e.g. Toyota Corolla"],
        ["year", "IntegerField", "Manufacturing year"],
        ["color", "CharField", "Vehicle colour"],
        ["plate", "CharField (unique)", "Licence plate number"],
        ["seat_capacity", "IntegerField", "Max seating capacity"],
        ["photo_url", "TextField", "Uploaded vehicle photo path"],
        ["is_primary", "BooleanField", "Primary vehicle flag"],
        ["vehicle_verified", "BooleanField", "Admin-set vehicle verification"],
    ],
    col_widths=[1.8, 1.5, 3.2]
)

doc.add_paragraph()

# 4.3 Rides
add_heading(doc, "4.3  Rides  (table: rides)", 2, "0C63B4")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["driver", "FK → CustomUser", "The posting driver"],
        ["vehicle", "FK → Vehicle", "Vehicle used for the trip"],
        ["origin / destination", "TextField", "Location labels"],
        ["origin_lat/lng", "FloatField", "Origin coordinates"],
        ["dest_lat/lng", "FloatField", "Destination coordinates"],
        ["depart_at / arrive_at", "DateTimeField", "Departure and estimated arrival"],
        ["seats_total / seats_available", "IntegerField", "Capacity management"],
        ["price_cents", "IntegerField", "Driver's asking price in cents (CAD)"],
        ["status", "TextField", "posted | full | cancelled | completed"],
        ["passenger_gender_pref", "TextField", "any | women_only | men_only"],
        ["recurring_group_id", "TextField (null)", "UUID linking recurring ride series"],
        ["preferences_json", "TextField", "JSON array of ride preferences"],
        ["notes", "TextField", "Free-text driver notes"],
    ],
    col_widths=[1.8, 1.5, 3.2]
)

doc.add_paragraph()

# 4.4 RideStop
add_heading(doc, "4.4  RideStop  (table: ride_stops)", 2, "0C63B4")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["ride", "FK → Rides", "Parent ride"],
        ["stop_order", "IntegerField", "Ordering sequence"],
        ["location", "TextField", "Stop label"],
        ["lat / lng", "FloatField", "Stop coordinates"],
        ["scheduled_at", "DateTimeField (null)", "Optional stop arrival time"],
    ],
    col_widths=[1.8, 1.5, 3.2]
)

doc.add_paragraph()

# 4.5 RideBooking
add_heading(doc, "4.5  RideBooking  (table: ride_bookings)", 2, "0C63B4")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["ride", "FK → Rides", "The booked ride"],
        ["passenger", "FK → CustomUser", "The booking rider"],
        ["pickup_stop", "FK → RideStop (null)", "Intermediate boarding point"],
        ["dropoff_stop", "FK → RideStop (null)", "Intermediate drop-off point"],
        ["seats", "PositiveIntegerField", "Number of seats booked"],
        ["agreed_price_cents", "PositiveIntegerField", "Final price incl. service fee"],
        ["status", "CharField", "requested | confirmed | rejected | cancelled | completed"],
        ["booked_at", "DateTimeField", "Booking creation timestamp"],
        ["confirmed_at", "DateTimeField (null)", "When booking was confirmed"],
        ["cancelled_at", "DateTimeField (null)", "When booking was cancelled"],
        ["cancellation_reason", "TextField (null)", "Reason for cancellation"],
    ],
    col_widths=[1.8, 1.5, 3.2]
)

doc.add_paragraph()

# 4.6 Message
add_heading(doc, "4.6  Message  (table: messages)", 2, "0C63B4")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["ride", "FK → Rides", "Ride context for the message"],
        ["sender", "FK → CustomUser", "Message author"],
        ["receiver", "FK → CustomUser", "Intended recipient"],
        ["body", "TextField", "Message text"],
        ["read_at", "DateTimeField (null)", "Null = unread"],
        ["created_at", "DateTimeField", "Send timestamp"],
    ],
    col_widths=[1.8, 1.5, 3.2]
)

doc.add_paragraph()

# 4.7 PriceOffer
add_heading(doc, "4.7  PriceOffer  (table: negotiations)", 2, "0C63B4")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["booking", "FK → RideBooking", "Associated booking"],
        ["proposed_by", "FK → CustomUser", "Driver or rider who made the offer"],
        ["price_cents", "PositiveIntegerField", "Offered ride price in cents (excl. service fee)"],
        ["status", "CharField", "pending | accepted | declined | countered"],
        ["created_at", "DateTimeField", "Offer creation time"],
        ["responded_at", "DateTimeField (null)", "When the other party responded"],
    ],
    col_widths=[1.8, 1.5, 3.2]
)

doc.add_paragraph()

# 4.8 RideSearch
add_heading(doc, "4.8  RideSearch  (table: search_log)", 2, "0C63B4")
add_para(doc, "Analytics table — every rider search is logged for demand analysis.")
add_table(doc,
    ["Field", "Type", "Description"],
    [
        ["user", "FK → CustomUser (null)", "Searcher (null if anon)"],
        ["origin / destination", "TextField", "Search terms"],
        ["origin_lat/lng / dest_lat/lng", "FloatField", "Search coordinates"],
        ["search_date", "DateField", "Desired travel date"],
        ["passenger_count", "PositiveIntegerField", "Seats needed"],
        ["created_at", "DateTimeField", "Search timestamp"],
    ],
    col_widths=[2.0, 1.5, 3.0]
)

page_break(doc)

# ══════════════════════════════════════════════════════
#  5. URL ROUTES
# ══════════════════════════════════════════════════════
add_heading(doc, "5. URL Routes", 1)

add_table(doc,
    ["URL Pattern", "View", "Auth Required", "Description"],
    [
        ["/", "home_view", "No", "Landing / home page"],
        ["/signup/", "signup_view", "No", "User registration form"],
        ["/login/", "login_view", "No (redirects if logged in)", "Login form"],
        ["/logout/", "logout_view", "Yes (redirects if not)", "Session logout"],
        ["/role-select/", "role_select_view", "Yes", "First-login role selection"],
        ["/profile/", "profile_view", "Yes (manual check)", "Profile view & edit"],
        ["/post-ride/", "post_ride_view", "Yes (manual check)", "Driver posts a ride"],
        ["/ride-success/<id>/", "ride_success_view", "Yes", "Post-ride confirmation page"],
        ["/search-rides/", "search_rides_view", "Yes", "Rider searches for rides"],
        ["/ride-booking/<id>/", "create_ride_booking", "Yes", "Booking details & confirm"],
        ["/my-rides/", "my_rides_view", "Yes", "Driver & rider ride history"],
        ["/driver-inbox/", "driver_inbox_view", "Yes (driver only)", "Driver's message inbox"],
        ["/chat/<token>/", "chat_view", "Yes (driver/passenger only)", "Real-time chat room"],
        ["/change-password/", "change_password_view", "Yes", "Password change (AJAX)"],
        ["/profile/change-email/", "change_email_view", "Yes", "Email change flow"],
        ["/profile/send-verification/", "send_verification_email", "Yes", "Resend verification email"],
        ["/negotiations/create/<id>/", "create_price_offer_view", "Yes", "Submit price offer (AJAX)"],
        ["/negotiations/respond/<id>/", "respond_price_offer_view", "Yes", "Accept/decline/counter offer"],
        ["/about/", "about_view", "No", "About page"],
        ["/how-it-works/", "how_it_works_view", "No", "How it works page"],
        ["/privacy-policy/", "privacy_policy_view", "No", "Privacy policy"],
        ["/terms-condition/", "terms_view", "No", "Terms & conditions"],
        ["/community-guidelines/", "communityguide_view", "No", "Community guidelines"],
        ["/contact-us/", "contact_view", "No", "Contact page"],
        ["/accounts/confirm-email/<key>/", "CustomConfirmEmailView", "No", "Email confirmation link"],
        ["ws/chat/<token>/", "ChatConsumer (WebSocket)", "Yes", "Real-time WebSocket chat"],
    ],
    col_widths=[2.2, 1.8, 1.3, 2.2]
)

page_break(doc)

# ══════════════════════════════════════════════════════
#  6. CORE FEATURES
# ══════════════════════════════════════════════════════
add_heading(doc, "6. Core Features", 1)

# 6.1
add_heading(doc, "6.1  User Authentication", 2, "0C63B4")
add_bullet(doc, "Custom email-based login (no username). Django's AbstractUser extended.")
add_bullet(doc, "django-allauth handles email verification via HMAC confirmation links.")
add_bullet(doc, "Passwords validated: min 8 chars, must contain letter and digit.")
add_bullet(doc, "Login page redirects already-authenticated users to profile with an info message.")
add_bullet(doc, "Logout page redirects unauthenticated visitors to login with an info message.")
add_bullet(doc, "Email change flow: sends HMAC-signed verification to new email; old email remains active until confirmed.")

doc.add_paragraph()

# 6.2
add_heading(doc, "6.2  Role System", 2, "0C63B4")
add_para(doc, "Every user has a role: rider, driver, or admin.")
add_bullet(doc, "First login → role-select screen (stored in session flag show_role_select).")
add_bullet(doc, "Drivers must be admin-verified (driver_verified = True) to post rides.")
add_bullet(doc, "Drivers must have at least one admin-verified vehicle to post rides.")
add_bullet(doc, "Riders must have role = rider to search and book rides.")

doc.add_paragraph()

# 6.3
add_heading(doc, "6.3  Ride Posting (Driver)", 2, "0C63B4")
add_para(doc, "Drivers fill a multi-section form to post a ride:")
add_bullet(doc, "Route Builder: origin and final destination with optional intermediate pickup and drop-off stops (max 5 total).")
add_bullet(doc, "Address Autocomplete: Photon/Komoot API suggests places; coordinates stored in hidden fields.")
add_bullet(doc, "Route Summary: once both endpoints are geocoded, OSRM API calculates driving distance (km) and estimated duration. Duration stored as duration_seconds and used to compute arrive_at.")
add_bullet(doc, "Ride Settings: vehicle selection (verified only), seat count (1–8), price per seat (CAD), passenger gender preference.")
add_bullet(doc, "Recurring Rides: toggle enables day-of-week selector and end date. A UUID recurring_group_id links all instances.")
add_bullet(doc, "Preferences: tags (Music OK, Pet Friendly, etc.) stored as JSON array.")
add_bullet(doc, "Validation: departure must be at least 30 minutes from now.")

doc.add_paragraph()

# 6.4
add_heading(doc, "6.4  Ride Search (Rider)", 2, "0C63B4")
add_para(doc, "Riders search by origin, destination, and date.")
add_bullet(doc, "Date picker enforces today as minimum via JavaScript (prevents past-date searches).")
add_bullet(doc, "Base query filters: status in (posted, full) AND depart_at >= now(). Past rides never appear.")
add_bullet(doc, "Coordinate-based proximity: Haversine formula filters rides within 15 km of searched origin/destination.")
add_bullet(doc, "Falls back to text icontains search if no coordinates provided.")
add_bullet(doc, "Additional filters: seats available, max price, departure time slot, ride preferences.")
add_bullet(doc, "Gender preference filtering: women_only rides hidden from male riders and vice versa.")
add_bullet(doc, "Every search is logged to the RideSearch table for demand analytics.")

doc.add_paragraph()

# 6.5
add_heading(doc, "6.5  Ride Booking Flow", 2, "0C63B4")
add_para(doc, "Two independent paths from the booking-details page, both unlocked by checking Terms & Conditions:")

add_para(doc, "Path A — Direct Confirm:", bold=True, indent=True)
add_bullet(doc, "Rider clicks Confirm & Book Seat.", level=1)
add_bullet(doc, "AJAX POST with intent=confirm.", level=1)
add_bullet(doc, "Booking created with STATUS_CONFIRMED immediately.", level=1)
add_bullet(doc, "Page stays; chat button enables. Rider can open chat for logistics.", level=1)

add_para(doc, "Path B — Negotiate then Confirm:", bold=True, indent=True)
add_bullet(doc, "Rider clicks Chat with Driver.", level=1)
add_bullet(doc, "Form POST with intent=negotiate.", level=1)
add_bullet(doc, "Booking created with STATUS_REQUESTED.", level=1)
add_bullet(doc, "Redirects to chat room. Rider proposes a price; driver accepts/counters.", level=1)
add_bullet(doc, "On offer acceptance, booking becomes STATUS_CONFIRMED with new agreed_price_cents.", level=1)

doc.add_paragraph()

# 6.6
add_heading(doc, "6.6  Pricing & Service Fee", 2, "0C63B4")
add_bullet(doc, "SERVICE_FEE_CENTS = 100 ($1.00 CAD flat fee per booking).")
add_bullet(doc, "agreed_price_cents stores the total amount charged to the rider (ride price + service fee).")
add_bullet(doc, "Negotiation is conducted on the ride price only (excluding service fee).")
add_bullet(doc, "When a negotiated offer is accepted via HTTP, SERVICE_FEE_CENTS is re-added: agreed_price_cents = offer.price_cents + SERVICE_FEE_CENTS.")
add_bullet(doc, "PriceOffer.price_cents represents ride price only (what the driver earns).")

doc.add_paragraph()

# 6.7
add_heading(doc, "6.7  Real-time Chat & Negotiation", 2, "0C63B4")
add_para(doc, "Built on Django Channels with Daphne ASGI server.")

add_para(doc, "Security layers:", bold=True)
add_bullet(doc, "AuthMiddlewareStack in asgi.py populates scope['user'] from Django session.")
add_bullet(doc, "Consumer rejects if not user.is_authenticated.")
add_bullet(doc, "Chat token is a django.core.signing HMAC-signed payload (expires 7 days).")
add_bullet(doc, "Only the booking's driver and passenger can connect — others are disconnected.")

add_para(doc, "WebSocket message types:", bold=True)
add_table(doc,
    ["msg_type", "Direction", "Action"],
    [
        ["message", "Either → Both", "Sends a text chat message; saved to Message table"],
        ["price_offer", "Either → Both", "Proposes a price; saved to PriceOffer table"],
        ["price_accepted", "Either → Both", "Accepts pending offer; updates RideBooking"],
    ],
    col_widths=[1.5, 1.5, 3.5]
)

doc.add_paragraph()

# 6.8
add_heading(doc, "6.8  My Rides Dashboard", 2, "0C63B4")
add_para(doc, "Single page serving both driver and rider views simultaneously.")
add_bullet(doc, "Driver view: all posted rides with booking counts, passenger avatars, earnings total, CO2 saved estimate.")
add_bullet(doc, "Rider view: all bookings with status, driver info, total spent.")
add_bullet(doc, "Tabbed filters: All / Active / Upcoming / Completed / Cancelled.")
add_bullet(doc, "Unread message badge count from Message table.")

doc.add_paragraph()

# 6.9
add_heading(doc, "6.9  Driver Inbox", 2, "0C63B4")
add_bullet(doc, "Lists all active bookings for the driver with conversation previews.")
add_bullet(doc, "Shows unread badge count, negotiation status (pending offer / agreed price), online status.")
add_bullet(doc, "Sorted: unread conversations first.")
add_bullet(doc, "Filter tabs: All / Unread / Negotiating / Active / Completed.")

doc.add_paragraph()

# 6.10
add_heading(doc, "6.10  File Uploads", 2, "0C63B4")
add_bullet(doc, "Profile selfie, driver licence, and vehicle photos uploaded via form.")
add_bullet(doc, "Pillow resizes images to max 1200px on the longest side, quality 85%, saved as JPEG.")
add_bullet(doc, "EXIF rotation corrected automatically.")
add_bullet(doc, "Stored in media/selfies/, media/licenses/, media/vehicles/.")

page_break(doc)

# ══════════════════════════════════════════════════════
#  7. SECURITY
# ══════════════════════════════════════════════════════
add_heading(doc, "7. Security", 1)

add_table(doc,
    ["Concern", "Implementation"],
    [
        ["Authentication", "@login_required decorator on all protected views"],
        ["CSRF", "Django CsrfViewMiddleware on all POST endpoints; AJAX requests include CSRF token via FormData"],
        ["Chat access control", "Signed token (HMAC) + driver/passenger membership check at HTTP and WebSocket layer"],
        ["Token forgery", "django.core.signing with SECRET_KEY + 7-day expiry"],
        ["Redirect abuse (login)", "Logged-in users redirected away from /login/ with info message"],
        ["Redirect abuse (logout)", "Unauthenticated users redirected away from /logout/ with info message"],
        ["Page caching", "@never_cache on login_view prevents browser back-button access"],
        ["Input validation", "Email, phone (E.164), password complexity validated server-side"],
        ["File uploads", "Images processed through Pillow — raw file never served directly"],
        ["Admin access", "Django admin at /admin/ restricted to staff/superuser accounts"],
        ["SQL injection", "Django ORM parameterised queries used throughout — no raw SQL"],
        ["XSS", "Django template auto-escaping enabled; no mark_safe usage"],
        ["Error pages", "Custom 400/403/404/500 handlers render error.html"],
    ],
    col_widths=[2.2, 4.3]
)

page_break(doc)

# ══════════════════════════════════════════════════════
#  8. EXTERNAL APIS
# ══════════════════════════════════════════════════════
add_heading(doc, "8. External APIs & Services", 1)

add_table(doc,
    ["Service", "Used For", "Called From", "Notes"],
    [
        ["Photon (photon.komoot.io)", "Address autocomplete & geocoding", "Browser (JS)", "Free, no API key required"],
        ["OSRM (router.project-osrm.org)", "Driving distance & duration", "Browser (JS)", "Free public instance; waypoints include stops"],
        ["Hostinger SMTP", "Email verification, email change notifications", "Django backend", "Configured via .env"],
        ["Google Fonts", "UI typography (Poppins, DM Sans, Montserrat)", "Browser (CDN)", "Loaded in <head>"],
    ],
    col_widths=[2.0, 1.8, 1.5, 2.2]
)

page_break(doc)

# ══════════════════════════════════════════════════════
#  9. KEY BUSINESS LOGIC
# ══════════════════════════════════════════════════════
add_heading(doc, "9. Key Business Logic", 1)

add_heading(doc, "9.1  Haversine Proximity Search", 2, "0C63B4")
add_para(doc,
    "When a rider searches with coordinates, rides are filtered by straight-line distance using the Haversine formula "
    "implemented in _haversine_km(). The search radius is 15 km (SEARCH_RADIUS_KM). This avoids calling OSRM for every "
    "ride in the database, making the filter fast and scalable with no external dependencies.")

doc.add_paragraph()

add_heading(doc, "9.2  Recurring Rides", 2, "0C63B4")
add_para(doc,
    "When a driver enables the recurring toggle, the system iterates from the start date to the recurring_end_date. "
    "For each day matching the selected weekdays, a separate Rides record is created. All share the same recurring_group_id "
    "(UUID), linking them as a series. The driver is redirected to the first ride's success page.")

doc.add_paragraph()

add_heading(doc, "9.3  Seat Management", 2, "0C63B4")
add_para(doc,
    "On booking creation, seats_available is decremented by the booked seat count. If seats_available reaches 0, "
    "ride.status is set to 'full' — the ride still appears in search results but is labelled full. "
    "Cancelled bookings should restore seats (currently a manual admin operation).")

doc.add_paragraph()

add_heading(doc, "9.4  Email Change Flow", 2, "0C63B4")
add_para(doc,
    "1. Rider submits new email + current password via AJAX.\n"
    "2. A pending_email field is written; user.email is NOT changed yet.\n"
    "3. An HMAC confirmation link is emailed to the new address.\n"
    "4. On click, CustomConfirmEmailView.post() delegates to allauth, then signals.py updates user.email and username.\n"
    "5. Session is invalidated; user must log in with the new email.")

doc.add_paragraph()

add_heading(doc, "9.5  Chat Token Security", 2, "0C63B4")
add_para(doc,
    "make_chat_token(booking_id) calls django.core.signing.dumps({'bid': booking_id}, salt='komute.chat'). "
    "The token is a base64-encoded, HMAC-signed string. resolve_chat_token() verifies the signature and rejects "
    "tokens older than 7 days (max_age=604800 seconds). Tampered or expired tokens return None → Http404.")

page_break(doc)

# ══════════════════════════════════════════════════════
#  10. CONFIGURATION
# ══════════════════════════════════════════════════════
add_heading(doc, "10. Configuration & Environment", 1)

add_para(doc, "Sensitive values are loaded via python-decouple from a .env file in the project root:")
add_table(doc,
    ["Variable", "Description"],
    [
        ["EMAIL_HOST_USER", "SMTP sender email address (Hostinger)"],
        ["EMAIL_HOST_PASSWORD", "SMTP account password"],
        ["SECRET_KEY", "Django secret key (used for signing, CSRF, sessions)"],
    ],
    col_widths=[2.0, 4.5]
)

doc.add_paragraph()
add_para(doc, "Other notable settings:")
add_bullet(doc, "DEBUG = False (production mode)")
add_bullet(doc, "ALLOWED_HOSTS = ['*'] (restrict in production)")
add_bullet(doc, "TIME_ZONE = 'UTC' — all datetimes stored in UTC")
add_bullet(doc, "CHANNEL_LAYERS uses InMemoryChannelLayer — replace with Redis for multi-process deployment")
add_bullet(doc, "MEDIA_ROOT = BASE_DIR / 'media' — ensure this directory is writable")
add_bullet(doc, "AUTH_USER_MODEL = 'komute.CustomUser'")
add_bullet(doc, "SITE_ID = 1 — required by django-allauth")

page_break(doc)

# ══════════════════════════════════════════════════════
#  11. KNOWN ISSUES / FUTURE IMPROVEMENTS
# ══════════════════════════════════════════════════════
add_heading(doc, "11. Known Issues & Future Improvements", 1)

add_table(doc,
    ["Item", "Status", "Notes"],
    [
        ["InMemoryChannelLayer", "To Fix", "Not suitable for multi-process/multi-server. Replace with channels_redis."],
        ["WebSocket price acceptance — no who-check", "Bug", "Both driver and rider can accept any offer via WS. Should validate that the acceptor is not the proposer."],
        ["WebSocket service fee missing", "Bug", "accept_price_offer() sets agreed_price_cents = offer price only, missing +SERVICE_FEE_CENTS."],
        ["Cancelled bookings don't restore seats", "Improvement", "seats_available should increment on cancellation."],
        ["No payment gateway", "Future", "Payment processing not yet integrated."],
        ["No push notifications", "Future", "New booking/message alerts not sent outside the app."],
        ["SQLite in production", "To Fix", "Replace with PostgreSQL for concurrent write performance."],
        ["ALLOWED_HOSTS = '*'", "Security", "Should be restricted to the actual domain in production."],
        ["service_fee_cents not stored separately", "Improvement", "agreed_price_cents bundles ride price + fee. Add service_fee_cents field to RideBooking for clean accounting."],
    ],
    col_widths=[2.2, 1.0, 3.3]
)

page_break(doc)

# ══════════════════════════════════════════════════════
#  12. GLOSSARY
# ══════════════════════════════════════════════════════
add_heading(doc, "12. Glossary", 1)

add_table(doc,
    ["Term", "Definition"],
    [
        ["Driver", "A verified user who posts rides and has at least one verified vehicle"],
        ["Rider", "A user who searches and books seats on posted rides"],
        ["agreed_price_cents", "The final total the rider pays, including the $1 service fee, stored in cents"],
        ["price_cents (PriceOffer)", "The ride-only price (excluding service fee) proposed during negotiation"],
        ["STATUS_REQUESTED", "Booking created but price not yet agreed — negotiation in progress"],
        ["STATUS_CONFIRMED", "Booking confirmed at an agreed price"],
        ["Chat token", "An HMAC-signed URL-safe string encoding a booking ID, used to authenticate chat access"],
        ["Haversine formula", "Mathematical formula computing straight-line distance between two lat/lng coordinates"],
        ["OSRM", "Open Source Routing Machine — free routing API returning road distance and drive time"],
        ["Photon", "Open geocoding API by Komoot — converts text addresses to coordinates"],
        ["recurring_group_id", "UUID shared by all Rides records that belong to the same recurring series"],
        ["SERVICE_FEE_CENTS", "Platform fee of 100 cents ($1.00 CAD) added to every booking"],
    ],
    col_widths=[2.2, 4.3]
)

# ── Footer on all pages ──
for section in doc.sections:
    footer = section.footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run(f"Komüte Technical Documentation  ·  Confidential  ·  {datetime.date.today().year}")
    run.font.name = "Calibri"
    run.font.size = Pt(8.5)
    run.font.color.rgb = RGBColor(90, 112, 96)

doc.save(r"d:\VS Projects\python_tutorial\Indus Network\KomuteProject\Komute_Documentation.docx")
print("Done — Komute_Documentation.docx saved.")
