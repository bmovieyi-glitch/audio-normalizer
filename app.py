import os
import json
import tempfile
import subprocess
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
        return response

app = FastAPI()
app.add_middleware(SecurityHeadersMiddleware)

@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        result = subprocess.run([
            "ffmpeg", "-i", tmp_path,
            "-af", "loudnorm=I=-18:TP=-1:LRA=11:print_format=json",
            "-f", "null", "-"
        ], capture_output=True, text=True, timeout=120)
        stderr = result.stderr
        start = stderr.rfind("{")
        end = stderr.rfind("}") + 1
        if start == -1:
            return JSONResponse({"error": "解析失敗"}, status_code=500)
        return json.loads(stderr[start:end])
    finally:
        os.unlink(tmp_path)

@app.post("/api/normalize")
async def normalize(
    file: UploadFile = File(...),
    target_lufs: float = Form(-18.0),
    input_i: float = Form(...),
    input_lra: float = Form(...),
    input_tp: float = Form(...),
    input_thresh: float = Form(...),
    target_offset: float = Form(...),
):
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_in:
        tmp_in.write(await file.read())
        in_path = tmp_in.name

    out_fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(out_fd)

    try:
        af = (
            f"loudnorm=I={target_lufs}:TP=-1:LRA=11"
            f":measured_I={input_i}"
            f":measured_LRA={input_lra}"
            f":measured_TP={input_tp}"
            f":measured_thresh={input_thresh}"
            f":offset={target_offset}"
            f":linear=true"
        )
        result = subprocess.run([
            "ffmpeg", "-y", "-i", in_path,
            "-af", af, "-c:v", "copy", out_path
        ], capture_output=True, timeout=600)

        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.decode()[-500:]}, status_code=500)

        response = FileResponse(out_path, media_type="video/mp4", filename="normalized.mp4")
        return response
    except Exception as e:
        if os.path.exists(out_path):
            os.unlink(out_path)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if os.path.exists(in_path):
            os.unlink(in_path)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    return FileResponse("static/index.html")
