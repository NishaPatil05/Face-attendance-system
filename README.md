# Face Recognition Attendance System

A desktop application that uses a webcam to recognise registered employees or
students by their face and records attendance automatically in an Excel file.
Everything runs **locally on your computer** - nothing is uploaded anywhere.

## Features

- Dashboard with live date/time, registered count and today's present count
- Person registration with guided multi-angle face capture (5 samples)
- Real-time recognition of **multiple faces**; unknown faces are never marked
- One attendance record per person per day (enforced in Python, SQLite and Excel)
- `attendance.xlsx` created and updated automatically (never overwritten)
- Attendance Records screen: search by name/ID, date filter, export, open Excel
- Registered People screen with confirmed deletion of face data
- Friendly handling of camera and Excel errors; detailed logging to `logs/app.log`

## Technologies

| Purpose | Library |
|---|---|
| Camera, image processing | `opencv-python` |
| Face detection | OpenCV **YuNet** (ONNX) |
| Face embeddings (128-d) | OpenCV **SFace** (ONNX) |
| Maths | `numpy` |
| Excel | `openpyxl` |
| Local web server | `Flask` (listens on `127.0.0.1` only) |
| **Frontend** | plain **HTML + CSS + JavaScript** (no frameworks, no build step) |
| Storage | `sqlite3` (ships with Python) |

The Python program does all the work (camera, recognition, database, Excel).
The browser only shows the interface. Live video reaches the page as an MJPEG
stream from the same Python process, so the camera is read **once**, by Python.

### Why this face-recognition library?

The popular `face_recognition` package depends on `dlib`, which must be compiled
on Windows and therefore needs **CMake and Visual Studio C++ Build Tools** -
a very common source of installation failures. OpenCV's built-in YuNet +
SFace models give modern, accurate deep-learning face embeddings and install
with a single `pip install opencv-python` wheel. No compiler, no GPU, no cloud.

## Folder structure

```text
face_attendance_system/
├── main.py              entry point: prepares everything, starts the local server
├── webapp.py            Flask app factory (routes + static frontend)
├── web_common.py        shared context, session registry, API security helpers
├── config.py            paths, tuning constants, user Settings
├── database.py          SQLite access (persons, attendance, transactions)
├── camera.py            webcam access + background capture thread + errors
├── live_session.py      camera worker that streams annotated video (MJPEG)
├── face_engine.py       detection, embeddings, face matching
├── attendance.py        attendance rules + recognition/debounce logic
├── live_attendance.py   live attendance session + /api/attendance routes
├── registration.py      guided face capture + /api/register routes
├── dashboard.py         /api/dashboard, /api/settings, /api/shutdown routes
├── reports.py           records, export, Excel and people routes
├── excel_manager.py     attendance.xlsx handling
├── utils.py             date/time formatting, logging setup, helpers
├── web/
│   ├── index.html       the single-page interface
│   ├── css/style.css    styling (light + dark mode)
│   └── js/app.js        all front-end behaviour
├── requirements.txt
├── README.md
├── tests/               automated tests (core logic + full web flow)
├── data/                attendance.db (+ settings.json)
├── face_data/models/    downloaded AI model files (contain no personal data)
├── exports/             copies of exported Excel files
└── logs/                app.log
```

`attendance.xlsx` is created in the project folder on first start.

## Installation (Windows)

1. **Check Python** (3.11 or newer; tick *"Add python.exe to PATH"* when installing
   from python.org):
   ```bash
   python --version
   ```
2. **Open a terminal in the project folder**, e.g.
   `cd C:\Users\You\face_attendance_system`
3. **Create a virtual environment** (an isolated copy of Python for this project):
   ```bash
   python -m venv venv
   ```
4. **Activate it** (your prompt will start with `(venv)`):
   ```bash
   venv\Scripts\activate
   ```
   If PowerShell blocks this, run once:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use Command Prompt.
5. **Install the dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
6. **Run:**
   ```bash
   python main.py
   ```
   Your default browser opens at `http://127.0.0.1:5000/`. Keep the terminal window
   open while you use the app. Quit with the **Exit** button or `Ctrl+C`.

On the **first start** the app downloads two small AI model files (about 40 MB,
one time) into `face_data/models/`. Only the public models are downloaded - never
any of your data. If your network blocks it, download manually and place the
files in `face_data/models/`:

- <https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx>
- <https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx>

No special Windows steps are needed: there is no dlib, CMake, Visual Studio or
Tkinter requirement. Any modern browser works (Chrome, Edge, Firefox).

## Configuration

- **Settings page** (saved to `data/settings.json`): camera index, recognition
  threshold, attendance cooldown, confirmation frames, plus a *Test Camera* button.
- **`config.py`**: application name, web host/port (default `127.0.0.1:5000`),
  Excel/database/model paths, minimum face size, number and wording of
  registration poses, etc.

| Setting | Default | Meaning |
|---|---|---|
| Camera index | 0 | 0 = built-in webcam, 1 = first external camera |
| Recognition threshold | 0.40 | cosine similarity needed to accept a match (higher = stricter) |
| Attendance cooldown | 60 s | how long the same person is ignored after being processed |
| Confirmation frames | 3 | consecutive frames a person must be recognised before marking |

## How to register a person

1. Dashboard -> **Register Person** (or the *Register* tab).
2. Fill in ID, name, department (email is optional). Existing IDs are rejected.
3. Click **Capture Face**. Follow the on-screen pose prompts (straight, left,
   right, up, down). Capture only happens when **exactly one** face is visible
   and large enough. A face that is already registered under another ID is refused.
4. Click **Save Person** -> "Person registered successfully."

## How to start attendance

Dashboard -> **Start Attendance**. Stand in front of the camera. A green box shows
the name and ID; unknown faces show **Unknown Person**; unsure faces show
**Face not recognized**. When attendance is saved the panel shows
**"✓ Attendance Marked"** with name, time and status for a few seconds. A person
already marked today sees **"Already Present Today"**.

## How Excel works

`attendance.xlsx` (sheet *Attendance*) has the columns
`Employee ID | Name | Department | Date | Time | Status`, with formatted headers,
automatic column widths and a filter. Dates are `DD-MM-YYYY`, times `HH:MM:SS AM/PM`,
status `P`. New rows are **appended** - the file is never overwritten; each save goes
to a temporary file that atomically replaces the original. **Open Excel** first adds
any database rows missing from the sheet, then opens the file.

## How duplicate prevention works

1. **Debounce:** a person must be recognised in several consecutive frames before
   anything happens, and is then ignored for the cooldown period.
2. **Python check:** the database is asked whether the person is already present today.
3. **Database constraint:** `UNIQUE (employee_id, attendance_date)` on the
   `attendance` table - the final guarantee, even if two processes race.
4. **Excel check:** a row is not added if the sheet already has that person and date.

**Database/Excel consistency:** the attendance row is inserted inside a database
transaction and committed only *after* Excel was written successfully. If
`attendance.xlsx` is open in Excel, the transaction is rolled back, the screen says
*"Please close attendance.xlsx and try again."* and the person is retried
automatically after 5 seconds.

## Privacy considerations

- Face data is biometric data. It never leaves your computer. The web server
  listens on `127.0.0.1` only (not reachable from other devices), and every API
  call needs a random per-run token, so other websites in your browser cannot use it.
- **Photographs are never saved.** Registration keeps only numeric face signatures
  (128 numbers per sample, 5 samples per person).
- Signatures live in `data/attendance.db`, table `persons`, column `face_encoding`.
  They are never displayed in the UI or written to the log.
- Deleting a person (Registered People -> *Delete Selected*, with confirmation)
  permanently removes their face signatures. Past attendance rows are kept as an
  audit trail; delete them from the database/Excel manually if your policy requires.
- Protect the project folder (Windows account password, BitLocker) and tell people
  what you collect. Check your local data-protection laws and obtain consent.
- Limitation: face recognition can be fooled by a photo or video of a person
  (no liveness detection). For high-security use add supervision or liveness checks.

## Troubleshooting

| Problem | Solution |
|---|---|
| "Unable to access webcam..." | Check the cable, close Zoom/Teams/browser tabs using the camera, try camera index 1 in Settings (use *Test Camera*). |
| Camera found but no image | Windows Settings -> Privacy & security -> Camera -> allow desktop apps. |
| "Please close attendance.xlsx and try again." | Close the file in Excel. Nothing was lost - the person is retried automatically. |
| Face models could not be downloaded | Download the two `.onnx` files manually (links above) into `face_data/models/`. |
| Many "Face not recognized" | Improve lighting, face the camera, re-register with good light, or lower the threshold slightly (e.g. 0.35). |
| Wrong person matched | Raise the threshold (e.g. 0.45) and re-register both people in good light. |
| Page does not open / port busy | Open the address printed in the terminal (the app uses the next free port if 5000 is taken). |
| Camera light stays on after closing the tab | It is released automatically after ~20 s; use **Exit** to release it immediately. |
| `attendance.xlsx looks damaged` | Rename the file; a new one is created and **Open Excel** re-adds the rows from the database. |

## Automated tests

```bash
python -m unittest discover -s tests -v
```

`tests/test_core.py` covers the attendance rules, database and Excel; `tests/test_web.py`
runs the full web flow (register -> recognise -> records -> export -> delete) with a
fake camera.

## Manual testing checklist

Tick each item. "Excel" means `attendance.xlsx`.

1. [ ] **Register Rahul** (e.g. EMP001): all 5 poses captured, "Person registered successfully."
2. [ ] **Recognise Rahul**: Start Attendance -> green box "Rahul (EMP001)", "✓ Attendance Marked", correct time.
3. [ ] **Check Excel**: Open Excel button -> one row, `P`, date `DD-MM-YYYY`, time `HH:MM:SS AM/PM`.
4. [ ] **Recognise Rahul again** (after the cooldown): "Already Present Today".
5. [ ] **Duplicate not created**: Excel and Attendance Records still show exactly one Rahul row for today.
6. [ ] **Register Priya** (EMP002). Registering Priya with ID EMP001 must be refused.
7. [ ] **Rahul + Priya together**: each is labelled correctly and Priya gets her own record.
8. [ ] **Unknown person**: shows "Unknown Person"; nothing is added to Excel/database.
9. [ ] **Multiple faces** (registered + unknown): each face labelled independently.
10. [ ] **Camera unavailable**: unplug the camera / use a wrong index -> friendly message, no crash. Unplug while running -> error dialog, return to dashboard.
11. [ ] **Excel locked**: open `attendance.xlsx` in Excel, recognise a new person -> "Please close attendance.xlsx and try again."; no row in Attendance Records; close Excel -> the person is marked within seconds.
12. [ ] **Application restart**: close and reopen; people and attendance are still present.
13. [ ] **Database persistence**: counts on the dashboard match before and after restart.
14. [ ] **Next-day attendance**: set the PC date to tomorrow (or wait a day), recognise Rahul -> a *new* row; yesterday's row is preserved.
15. [ ] **Delete a registered person**: confirmation appears; afterwards the person shows as "Unknown Person" and is gone from the list.
16. [ ] **Re-register the deleted person** with the same ID: allowed; recognised again.
