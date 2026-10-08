"""
Generate DL Verification documentation as a Word document.
Run with: venv\Scripts\python.exe docs/scripts/generate_dl_doc.py  (from repo root)
"""

from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

doc = Document()

# ── Page margins ──────────────────────────────────────────────────────────────
section = doc.sections[0]
section.top_margin    = Cm(2.0)
section.bottom_margin = Cm(2.0)
section.left_margin   = Cm(2.5)
section.right_margin  = Cm(2.5)

# ── Helper: shade a table row ─────────────────────────────────────────────────
def shade_cell(cell, hex_color):
    tc   = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd  = OxmlElement('w:shd')
    shd.set(qn('w:val'),   'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'),  hex_color)
    tcPr.append(shd)

def set_cell_font(cell, bold=False, size=10, color=None):
    for para in cell.paragraphs:
        for run in para.runs:
            run.bold      = bold
            run.font.size = Pt(size)
            if color:
                run.font.color.rgb = RGBColor(*color)

# ── Helper: add a styled heading ──────────────────────────────────────────────
def add_heading(text, level=1):
    p = doc.add_heading(text, level=level)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    return p

# ── Helper: add body paragraph ────────────────────────────────────────────────
def add_body(text, bold=False, italic=False):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold   = bold
    run.italic = italic
    run.font.size = Pt(11)
    return p

# ── Helper: add bullet ────────────────────────────────────────────────────────
def add_bullet(text, level=0):
    p = doc.add_paragraph(text, style='List Bullet')
    p.paragraph_format.left_indent = Inches(0.25 * (level + 1))
    run = p.runs[0]
    run.font.size = Pt(11)
    return p

# ── Helper: add code block ────────────────────────────────────────────────────
def add_code(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent  = Inches(0.4)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(4)
    run = p.add_run(text)
    run.font.name = 'Courier New'
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x1a, 0x1a, 0x5e)
    return p

# ── Helper: horizontal rule ───────────────────────────────────────────────────
def add_rule():
    doc.add_paragraph('─' * 80)

# ══════════════════════════════════════════════════════════════════════════════
#  TITLE PAGE
# ══════════════════════════════════════════════════════════════════════════════
title = doc.add_heading('DL Verification Module', level=0)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER

subtitle = doc.add_paragraph()
subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
sr = subtitle.add_run('Technical Documentation — Komute Project')
sr.font.size  = Pt(13)
sr.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
sr.italic = True

date_p = doc.add_paragraph()
date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
dr = date_p.add_run(f'Date: {datetime.date.today().strftime("%B %d, %Y")}')
dr.font.size = Pt(11)
dr.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════
add_heading('1. Overview', level=1)

add_body(
    'The DL (Driving Licence) Verification module is a multi-component system '
    'that checks whether the face on a driver\'s licence photo matches the face '
    'in a selfie taken by the user. It is designed to prevent fraud and ensure '
    'that only genuine, verified drivers can offer rides on the Komute platform.'
)

doc.add_paragraph()
add_body('The system is made up of three separate components that work together:', bold=True)
add_bullet('Django Web App (Komute) — the main platform where drivers upload their images and trigger verification.')
add_bullet('Node.js API (DL-Verification) — the middle-man that receives the images, runs face checking, and returns a decision.')
add_bullet('Python Face API — the AI engine that actually compares two faces and gives a similarity score.')

# ── Architecture diagram (text-based) ────────────────────────────────────────
doc.add_paragraph()
add_body('System Architecture (simplified flow):', bold=True)
add_code('Driver (Browser)')
add_code('      │  uploads licence + selfie, clicks "Verify"')
add_code('      ▼')
add_code('Django App  →  verify_driver_view  (views.py:1560)')
add_code('      │  reads saved images from storage, sends as HTTP POST')
add_code('      ▼')
add_code('Node.js API  →  POST /api/verify  (localhost:3000)')
add_code('      │  receives multipart images, calls Python service')
add_code('      ▼')
add_code('Python Face API  →  POST /api/face-verify  (localhost:5001)')
add_code('      │  runs face recognition, returns similarity score')
add_code('      ▼')
add_code('Node.js  →  applies threshold, builds decision, returns JSON')
add_code('      ▼')
add_code('Django  →  saves verified=True/False + similarity score to database')
add_code('      ▼')
add_code('Driver sees result on screen')

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — COMPONENT 1: verify_driver_view (Django)
# ══════════════════════════════════════════════════════════════════════════════
add_heading('2. Component 1 — verify_driver_view (Django)', level=1)

add_body(
    'File: komute/views.py, function verify_driver_view (line 1560)\n'
    'URL:  POST /profile/verify-driver/'
)

add_body(
    'This is the Django view that the driver\'s browser talks to when they click '
    '"Verify". It acts as the entry point of the entire verification process. '
    'It does NOT do the AI face matching itself — its job is to validate the '
    'request, fetch the saved images, call the Node.js API, and save the result.'
)

doc.add_paragraph()
add_heading('2.1  Step-by-Step Walkthrough', level=2)

steps = [
    (
        'Step 1 — Only POST Allowed',
        'The view immediately rejects any request that is not a POST (e.g. '
        'someone visiting the URL in a browser). This ensures the endpoint '
        'cannot be triggered accidentally.',
        'if request.method != "POST":\n    return JsonResponse({"error": "Method not allowed"}, status=405)'
    ),
    (
        'Step 2 — Identify the Logged-In User',
        'The view checks the session or Django\'s authentication system to find '
        'out who is making the request. If no user is found (not logged in), it '
        'returns a 401 Unauthorised error.',
        'user_id = request.session.get("user_id")\nuser = User.objects.filter(id=user_id).first()'
    ),
    (
        'Step 3 — Validate Images Exist',
        'Before doing anything, it checks that the driver has already uploaded '
        'both their licence photo and selfie to their profile. If either is '
        'missing, it stops and tells the user to upload them first.',
        'if not user.license_front_url:\n    return JsonResponse({"error": "Please upload licence first"}, status=400)\nif not user.selfie_url:\n    return JsonResponse({"error": "Please upload selfie first"}, status=400)'
    ),
    (
        'Step 4 — Convert URL to Storage Path',
        'Image URLs stored in the database look like /media/licenses/abc.jpg. '
        'Django\'s file storage system needs just the path without the /media/ '
        'prefix (e.g. licenses/abc.jpg). This step does that conversion.',
        'def _storage_path(url):\n    p = url.lstrip("/")\n    media_prefix = settings.MEDIA_URL.lstrip("/")\n    if p.startswith(media_prefix):\n        p = p[len(media_prefix):]\n    return p'
    ),
    (
        'Step 5 — Read Image Files into Memory',
        'The actual image file bytes are read from storage (local disk or cloud '
        'like S3). Both files are loaded into memory as raw binary data, ready '
        'to be sent to the Node.js API.',
        'with default_storage.open(path, "rb") as f:\n    license_bytes = f.read()\nwith default_storage.open(path, "rb") as f:\n    selfie_bytes  = f.read()'
    ),
    (
        'Step 6 — Send Images to Node.js API',
        'An HTTP POST request is sent to the Node.js verification service '
        '(localhost:3000/api/verify) with both images attached as multipart '
        'form data — just like attaching files to an email. A 120-second '
        'timeout is set to handle slow AI processing.',
        'resp = requests.post(\n    "http://localhost:3000/api/verify",\n    files={\n        "dl_image":     ("dl_image.jpg", license_bytes, "image/jpeg"),\n        "selfie_image": ("selfie.jpg",    selfie_bytes,  "image/jpeg"),\n    },\n    timeout=120\n)'
    ),
    (
        'Step 7 — Save Result to Database',
        'The Node.js API returns a JSON response. Django reads two key values: '
        '"verified" (True/False) and "similarity" (the face match score as a '
        'decimal, e.g. 0.87). These are saved directly to the user\'s profile.',
        'user.driver_verified  = result.get("verified", False)\nuser.face_match_score = result.get("face_match", {}).get("similarity", 0.0)\nuser.save(update_fields=["driver_verified", "face_match_score"])'
    ),
    (
        'Step 8 — Return Result to Browser',
        'The final JSON response is sent back to the driver\'s browser. It '
        'includes whether they are verified, the similarity percentage, and '
        'the full decision breakdown from the Node.js service.',
        'return JsonResponse({\n    "verified":   verified,\n    "similarity": round(float(similarity) * 100, 1),\n    "face_match": result.get("face_match", {}),\n    "decision":   result.get("decision", {}),\n})'
    ),
]

for i, (title_text, explanation, code_text) in enumerate(steps, 1):
    add_body(f'{title_text}', bold=True)
    add_body(explanation)
    add_code(code_text)
    doc.add_paragraph()

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — COMPONENT 2: Node.js DL-Verification API
# ══════════════════════════════════════════════════════════════════════════════
add_heading('3. Component 2 — Node.js DL-Verification API', level=1)

add_body(
    'File: DL-Verification-main/backend/node-api/routes/verify.js\n'
    'Endpoint: POST /api/verify  (runs on localhost:3000)'
)

add_body(
    'This is the middle-man service. It receives the images from Django, '
    'forwards them to the Python face AI, then applies business rules to '
    'decide whether verification passes or fails. It also handles its own '
    'frontend (not used by Komute — Django bypasses it).'
)

doc.add_paragraph()
add_heading('3.1  What It Does Step by Step', level=2)

node_steps = [
    ('Receives Images', 'The endpoint accepts the two image files (dl_image and selfie_image) using multer, a Node.js file upload library. Files are held in memory — not saved to disk.'),
    ('Validates Files',  'If either image is missing from the request, it immediately returns a 400 error with a clear message.'),
    ('Calls Python Face API', 'It sends both image buffers to the Python service at localhost:5001/api/face-verify. This is the actual AI comparison step. The Node.js service waits up to 30 seconds for a response.'),
    ('Receives Face Result', 'The Python API returns: match (true/false), similarity (0.0 to 1.0), face1_detected (licence face found?), face2_detected (selfie face found?).'),
    ('Runs Decision Engine', 'The decision.js module applies a threshold rule: similarity must be ≥ 0.38 (38%) AND both faces must be detected. If any condition fails, verified = false with a specific reason code.'),
    ('Builds Final Response', 'All results are packaged into a structured JSON response and returned to Django.'),
]

for title_text, explanation in node_steps:
    add_body(f'{title_text}', bold=True)
    add_body(explanation)
    doc.add_paragraph()

add_heading('3.2  Rejection Reason Codes', level=2)
add_body('If verification fails, the decision engine returns one of these specific codes:')

table = doc.add_table(rows=1, cols=2)
table.style = 'Table Grid'
table.alignment = WD_TABLE_ALIGNMENT.LEFT

hdr = table.rows[0].cells
hdr[0].text = 'Rejection Code'
hdr[1].text = 'Meaning'
for cell in hdr:
    shade_cell(cell, '1F497D')
    set_cell_font(cell, bold=True, size=10, color=(255, 255, 255))

reasons = [
    ('NO_FACE_IN_DL',              'No face was detected in the driving licence image'),
    ('NO_FACE_IN_SELFIE',          'No face was detected in the selfie image'),
    ('LOW_FACE_SIMILARITY',        'Faces were detected but similarity score is below 0.38'),
    ('FACE_VERIFICATION_MISSING',  'Face result data was completely absent (service error)'),
    ('SERVICE_ERROR',              'The Python face API reported an internal error'),
]

row_colors = ['FFFFFF', 'EEF3FB']
for i, (code, meaning) in enumerate(reasons):
    row = table.add_row().cells
    row[0].text = code
    row[1].text = meaning
    shade_cell(row[0], row_colors[i % 2])
    shade_cell(row[1], row_colors[i % 2])

doc.add_paragraph()
doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — COMPONENT 3: Python Face API
# ══════════════════════════════════════════════════════════════════════════════
add_heading('4. Component 3 — Python Face API', level=1)

add_body(
    'File: DL-Verification-main/backend/python-face-api/\n'
    'Endpoint: POST /api/face-verify  (runs on localhost:5001)'
)

add_body(
    'This is the AI engine. It uses a face recognition library to extract '
    'the face from both images and measure how similar they are. It returns '
    'a raw similarity score (a number between 0 and 1).'
)

doc.add_paragraph()
add_body('What it returns to Node.js:', bold=True)

table2 = doc.add_table(rows=1, cols=3)
table2.style = 'Table Grid'
table2.alignment = WD_TABLE_ALIGNMENT.LEFT

hdr2 = table2.rows[0].cells
hdr2[0].text = 'Field'
hdr2[1].text = 'Type'
hdr2[2].text = 'Meaning'
for cell in hdr2:
    shade_cell(cell, '1F497D')
    set_cell_font(cell, bold=True, size=10, color=(255, 255, 255))

face_fields = [
    ('match',           'boolean', 'True if similarity >= threshold (0.38)'),
    ('similarity',      'float',   'Face similarity score, 0.0 to 1.0 (e.g. 0.87 = 87%)'),
    ('face1_detected',  'boolean', 'Was a face found in the DL image?'),
    ('face2_detected',  'boolean', 'Was a face found in the selfie image?'),
    ('error',           'string',  'Error message if face detection failed (optional)'),
]

for i, (field, ftype, meaning) in enumerate(face_fields):
    row = table2.add_row().cells
    row[0].text = field
    row[1].text = ftype
    row[2].text = meaning
    color = 'FFFFFF' if i % 2 == 0 else 'EEF3FB'
    shade_cell(row[0], color)
    shade_cell(row[1], color)
    shade_cell(row[2], color)

doc.add_paragraph()
doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — THE HANDSHAKE (end-to-end data flow)
# ══════════════════════════════════════════════════════════════════════════════
add_heading('5. The Handshake — End-to-End Data Flow', level=1)

add_body(
    'This section traces exactly what data moves between each component, '
    'from the moment the driver clicks "Verify" to the moment the result '
    'is shown on screen.'
)

doc.add_paragraph()

handshake_steps = [
    (
        'Handshake 1: Browser → Django',
        'Protocol: HTTPS POST to /profile/verify-driver/',
        'Data sent: session cookie (to identify the user) — NO image files at this stage.\n'
        'The images were uploaded earlier when the driver saved their profile.\n'
        'Django reads the images from its own storage internally.',
    ),
    (
        'Handshake 2: Django → Node.js',
        'Protocol: HTTP POST to http://localhost:3000/api/verify',
        'Data sent: multipart/form-data with two fields:\n'
        '  • dl_image     → raw bytes of the licence photo + filename "dl_image.jpg"\n'
        '  • selfie_image → raw bytes of the selfie photo  + filename "selfie.jpg"\n'
        'Timeout: 120 seconds\n'
        'Note: This is a server-to-server call. The user\'s browser is not involved.',
    ),
    (
        'Handshake 3: Node.js → Python Face API',
        'Protocol: HTTP POST to http://localhost:5001/api/face-verify',
        'Data sent: multipart/form-data with two fields:\n'
        '  • image1 → DL image buffer (labelled "dl.jpg")\n'
        '  • image2 → Selfie image buffer (labelled "selfie_0.jpg")\n'
        'Timeout: 30 seconds',
    ),
    (
        'Handshake 4: Python Face API → Node.js (response)',
        'Protocol: JSON response',
        '{\n'
        '  "match":          true,\n'
        '  "similarity":     0.87,\n'
        '  "face1_detected": true,\n'
        '  "face2_detected": true\n'
        '}',
    ),
    (
        'Handshake 5: Node.js → Django (response)',
        'Protocol: JSON response',
        '{\n'
        '  "success":  true,\n'
        '  "verified": true,\n'
        '  "face_match": {\n'
        '      "matched":              true,\n'
        '      "similarity":           0.87,\n'
        '      "threshold":            0.38,\n'
        '      "dl_face_detected":     true,\n'
        '      "selfie_face_detected": true\n'
        '  },\n'
        '  "decision": {\n'
        '      "components": {\n'
        '          "face_verification": {\n'
        '              "passed":    true,\n'
        '              "reason":    "Face match successful (similarity: 0.870)",\n'
        '              "similarity": 0.87,\n'
        '              "threshold":  0.38\n'
        '          }\n'
        '      },\n'
        '      "failure_reasons": []\n'
        '  },\n'
        '  "confidence":          0.87,\n'
        '  "processing_time_ms":  1234,\n'
        '  "timestamp":           "2026-06-03T10:00:00.000Z",\n'
        '  "message":             "Verification successful"\n'
        '}',
    ),
    (
        'Handshake 6: Django saves to DB and responds to Browser',
        'Django stores in database:',
        'user.driver_verified  = True    (saved to DB)\n'
        'user.face_match_score = 0.87    (saved to DB)\n\n'
        'Django responds to browser:\n'
        '{\n'
        '  "verified":   true,\n'
        '  "similarity": 87.0,\n'
        '  "face_match": { ... },\n'
        '  "decision":   { ... }\n'
        '}',
    ),
]

for title_text, subtitle_text, detail in handshake_steps:
    add_body(title_text, bold=True)
    add_body(subtitle_text, italic=True)
    add_code(detail)
    doc.add_paragraph()

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — WHAT GETS SAVED TO DATABASE
# ══════════════════════════════════════════════════════════════════════════════
add_heading('6. What Gets Saved to the Database', level=1)

add_body(
    'After a successful API call, Django updates exactly two fields on the '
    'User record. Everything else returned by the Node.js API is sent to '
    'the browser for display only — it is not stored.'
)
doc.add_paragraph()

table3 = doc.add_table(rows=1, cols=3)
table3.style = 'Table Grid'
table3.alignment = WD_TABLE_ALIGNMENT.LEFT

hdr3 = table3.rows[0].cells
hdr3[0].text = 'Database Field'
hdr3[1].text = 'Source'
hdr3[2].text = 'Description'
for cell in hdr3:
    shade_cell(cell, '1F497D')
    set_cell_font(cell, bold=True, size=10, color=(255, 255, 255))

db_rows = [
    ('driver_verified',  'result["verified"]',                      'True if face matched, False if not'),
    ('face_match_score', 'result["face_match"]["similarity"]',       'Raw decimal score, e.g. 0.87 (87% similar)'),
]

for i, (field, source, desc) in enumerate(db_rows):
    row = table3.add_row().cells
    row[0].text = field
    row[1].text = source
    row[2].text = desc
    color = 'FFFFFF' if i % 2 == 0 else 'EEF3FB'
    for cell in row:
        shade_cell(cell, color)

doc.add_paragraph()
doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — HOW THE FRONTEND PAGE IS BYPASSED
# ══════════════════════════════════════════════════════════════════════════════
add_heading('7. How the DL-Verification Frontend Is Bypassed', level=1)

add_body(
    'The DL-Verification Node.js app ships with its own React frontend — '
    'a web page that lets a user upload images and see the result. '
    'Komute does NOT use this page. Here is why and how:'
)

doc.add_paragraph()
add_body('The DL-Verification app exposes two things on port 3000:', bold=True)
add_bullet('A frontend HTML page at http://localhost:3000/ — built for standalone testing.')
add_bullet('A raw REST API at http://localhost:3000/api/verify — designed for programmatic use.')

doc.add_paragraph()
add_body(
    'Django\'s verify_driver_view calls the API endpoint directly using '
    'the Python requests library. It sends an HTTP POST with the image files '
    'and receives JSON back — all on the server side. The user\'s browser '
    'never visits or sees the Node.js frontend page. Komute\'s own profile '
    'page acts as the user interface instead.'
)

doc.add_paragraph()
add_body('In simple terms:', bold=True)
add_bullet('Standalone use → user opens the Node.js frontend page, uploads images manually.')
add_bullet('Komute use → Django reads saved images and calls the API directly, skipping the frontend entirely.')

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 8 — FULL RESPONSE FIELD REFERENCE
# ══════════════════════════════════════════════════════════════════════════════
add_heading('8. Full Response Field Reference', level=1)
add_body('Complete list of all fields returned by the Node.js API to Django:')
doc.add_paragraph()

table4 = doc.add_table(rows=1, cols=3)
table4.style = 'Table Grid'
table4.alignment = WD_TABLE_ALIGNMENT.LEFT

hdr4 = table4.rows[0].cells
hdr4[0].text = 'Field'
hdr4[1].text = 'Type'
hdr4[2].text = 'Description'
for cell in hdr4:
    shade_cell(cell, '1F497D')
    set_cell_font(cell, bold=True, size=10, color=(255, 255, 255))

all_fields = [
    ('success',                                         'bool',   'True if the API call itself worked without errors'),
    ('verified',                                        'bool',   'Final pass/fail — True means the driver is verified'),
    ('face_match.matched',                              'bool',   'Did the AI face engine say it is a match?'),
    ('face_match.similarity',                           'float',  'Face similarity 0.0–1.0 (e.g. 0.87 = 87% similar)'),
    ('face_match.threshold',                            'float',  'Minimum similarity required — fixed at 0.38 (38%)'),
    ('face_match.dl_face_detected',                     'bool',   'Was a face detected in the DL image?'),
    ('face_match.selfie_face_detected',                 'bool',   'Was a face detected in the selfie image?'),
    ('decision.components.face_verification.passed',    'bool',   'Did the face verification component pass?'),
    ('decision.components.face_verification.reason',    'string', 'Human-readable explanation of the result'),
    ('decision.components.face_verification.similarity','float',  'Same similarity score, echoed here for reference'),
    ('decision.components.face_verification.threshold', 'float',  'Threshold used for the decision'),
    ('decision.failure_reasons',                        'array',  'Empty if passed; contains reason code + message if failed'),
    ('confidence',                                      'float',  'Same as similarity — used as the confidence score'),
    ('processing_time_ms',                              'int',    'How long the full verification took in milliseconds'),
    ('timestamp',                                       'string', 'ISO 8601 timestamp of when the decision was made'),
    ('message',                                         'string', '"Verification successful" or failure description'),
]

for i, (field, ftype, desc) in enumerate(all_fields):
    row = table4.add_row().cells
    row[0].text = field
    row[1].text = ftype
    row[2].text = desc
    color = 'FFFFFF' if i % 2 == 0 else 'EEF3FB'
    for cell in row:
        shade_cell(cell, color)

doc.add_paragraph()
doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 9 — ERROR SCENARIOS
# ══════════════════════════════════════════════════════════════════════════════
add_heading('9. Error Scenarios and What Happens', level=1)
doc.add_paragraph()

table5 = doc.add_table(rows=1, cols=3)
table5.style = 'Table Grid'
table5.alignment = WD_TABLE_ALIGNMENT.LEFT

hdr5 = table5.rows[0].cells
hdr5[0].text = 'Scenario'
hdr5[1].text = 'Where Caught'
hdr5[2].text = 'Response'
for cell in hdr5:
    shade_cell(cell, '1F497D')
    set_cell_font(cell, bold=True, size=10, color=(255, 255, 255))

errors = [
    ('User not logged in',                    'Django',  '401 — "Not authenticated"'),
    ('Licence image not uploaded',            'Django',  '400 — "Please upload your license image first"'),
    ('Selfie not uploaded',                   'Django',  '400 — "Please upload your selfie first"'),
    ('Image file missing from storage',       'Django',  '500 — "Could not read saved images"'),
    ('Node.js service not running',           'Django',  '503 — "Verification service is offline"'),
    ('Python Face API not running',           'Node.js', 'verified=false, error="Face API unavailable"'),
    ('No face in DL photo',                   'Node.js', 'verified=false, code=NO_FACE_IN_DL'),
    ('No face in selfie',                     'Node.js', 'verified=false, code=NO_FACE_IN_SELFIE'),
    ('Face similarity below 0.38 threshold',  'Node.js', 'verified=false, code=LOW_FACE_SIMILARITY'),
    ('GET request instead of POST to Django', 'Django',  '405 — "Method not allowed"'),
]

for i, (scenario, where, response) in enumerate(errors):
    row = table5.add_row().cells
    row[0].text = scenario
    row[1].text = where
    row[2].text = response
    color = 'FFFFFF' if i % 2 == 0 else 'EEF3FB'
    for cell in row:
        shade_cell(cell, color)

doc.add_paragraph()
doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 10 — QUICK SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
add_heading('10. Quick Summary', level=1)

summary_points = [
    'The driver uploads their licence and selfie once when saving their profile.',
    'Clicking "Verify" triggers a POST to Django\'s verify_driver_view.',
    'Django reads the saved images from storage and POSTs them (as file bytes) to the Node.js service on localhost:3000.',
    'Node.js forwards the images to the Python face AI on localhost:5001.',
    'The Python AI compares the two faces and returns a similarity score (0.0–1.0).',
    'Node.js applies a threshold (≥ 0.38 = match) and both-faces-detected checks.',
    'Node.js returns a structured JSON with verified=true/false plus all detail.',
    'Django saves driver_verified and face_match_score to the database.',
    'Django sends the result back to the browser as JSON.',
    'The driver\'s browser displays whether they passed or failed, and the similarity percentage.',
]

for point in summary_points:
    add_bullet(point)

doc.add_paragraph()
add_body(
    'The DL-Verification frontend page (the React UI that ships with the Node.js app) '
    'is completely bypassed. Django acts as its own UI, calling the Node.js REST API '
    'directly from the server side.',
    italic=True
)

# ══════════════════════════════════════════════════════════════════════════════
#  SAVE
# ══════════════════════════════════════════════════════════════════════════════
output_path = r'd:\VS Projects\python_tutorial\Indus Network\DL_Verification_Documentation.docx'
doc.save(output_path)
print(f'Document saved: {output_path}')
