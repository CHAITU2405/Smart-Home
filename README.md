# Smart Lighting Dashboard (ESP32 + Flask on Render)

## What it does
- Web dashboard shows **3 zones**: **1 = Home**, **2 = Kitchen**, **3 = Hall**
- ESP32 sends updates with HTTP GET only: **`/update?state=...`**
- Dashboard reads **`/api/state`** (polling)

## Local run
```bash
pip install -r requirements.txt
python app.py
```
Open `http://127.0.0.1:5000` (or the port in the `PORT` environment variable).

**Production-style run (Gunicorn):**
```bash
gunicorn app:app --bind 127.0.0.1:8000
```

## Deploy on Render
1. Create a **Web Service**, connect this repo.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT`  
   (or rely on the included `Procfile`)
4. After deploy, copy your app URL, e.g. `https://your-app.onrender.com`

### ESP32 firmware
Use **HTTPS** (Render does not serve plain HTTP on the public URL):

```cpp
const char* serverUrl = "https://your-app.onrender.com/update";
// ...
String url = String(serverUrl) + "?state=" + data;
http.begin(url);
```

Zone payload (same as your sketch):
- `state=0` — all off  
- `state=1` — Home on  
- `state=1,2` — Home + Kitchen  
- `state=1,2,3` — all on  

### ESP32 certificate note
If `HTTPClient` fails on HTTPS, enable the appropriate **root CA** for Render or use a library that verifies Let’s Encrypt (depends on your ESP32 Arduino core version).

## API
| Method | Path | Purpose |
|--------|------|--------|
| GET | `/` | Dashboard |
| GET | `/update?state=1,2` | ESP32 pushes zone state |
| GET | `/api/state` | JSON for the dashboard (polling) |

## Dependencies
- **Flask** — web app  
- **Gunicorn** — production WSGI server (Render / `Procfile`)
