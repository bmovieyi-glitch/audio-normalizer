from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse
import subprocess
import json
import os
import uuid

MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB（無料プラン512MB制限に合わせて削減）
ALLOWED_MIME = {"video/mp4", "video/mpeg", "video/quicktime"}

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

UPLOAD_DIR = "/tmp/audio_normalizer"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "リクエストが多すぎます。しばらく待ってから再試行してください。"}
    )


def parse_loudnorm_json(stderr: str) -> dict:
    json_start = stderr.rfind("{")
    json_end = stderr.rfind("}") + 1
    if json_start == -1:
        raise ValueError("Could not find loudnorm JSON in output")
    return json.loads(stderr[json_start:json_end])


def cleanup_files(file_id: str):
    for suffix in ["_input.mp4", "_normalized.mp4"]:
        path = os.path.join(UPLOAD_DIR, f"{file_id}{suffix}")
        if os.path.exists(path):
            os.remove(path)


@app.get("/")
def read_root():
    return FileResponse("static/index.html")


@app.post("/analyze")
@limiter.limit("10/minute")
async def analyze(request: Request, file: UploadFile = File(...)):
    # MIMEタイプチェック
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="MP4ファイルのみ対応しています")

    file_id = str(uuid.uuid4())
    input_path = os.path.join(UPLOAD_DIR, f"{file_id}_input.mp4")

    # メモリに溜めず直接ディスクに書き込みながらサイズチェック
    size = 0
    with open(input_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                f.close()
                os.remove(input_path)
                raise HTTPException(status_code=400, detail="ファイルサイズは200MB以下にしてください")
            f.write(chunk)

    cmd = [
        "ffmpeg", "-i", input_path,
        "-af", "loudnorm=I=-18:TP=-1:LRA=11:print_format=json",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    try:
        loudness_data = parse_loudnorm_json(result.stderr)
    except (ValueError, json.JSONDecodeError):
        os.remove(input_path)
        raise HTTPException(status_code=500, detail="音声解析に失敗しました。動画に音声トラックが含まれているか確認してください。")

    input_i = float(loudness_data["input_i"])
    current_content_loudness = round(input_i - (-14.0), 1)

    return {
        "file_id": file_id,
        "filename": file.filename,
        "current_lufs": round(input_i, 1),
        "current_content_loudness": current_content_loudness,
        "after_lufs": -18.0,
        "after_content_loudness": -4.0,
        "loudness_data": loudness_data,
    }


@app.post("/normalize/{file_id}")
@limiter.limit("5/minute")
async def normalize(request: Request, file_id: str, background_tasks: BackgroundTasks, target_cl: float = -4.0):
    target_lufs = target_cl - 14.0
    if not (-30.0 <= target_lufs <= -6.0):
        raise HTTPException(status_code=400, detail="target_cl は -16.0〜+8.0 dB の範囲で指定してください")

    # file_id をパス traversal 対策で検証
    if not all(c in "0123456789abcdef-" for c in file_id):
        raise HTTPException(status_code=400, detail="不正なリクエストです")

    input_path = os.path.join(UPLOAD_DIR, f"{file_id}_input.mp4")
    output_path = os.path.join(UPLOAD_DIR, f"{file_id}_normalized.mp4")

    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません。再度アップロードしてください。")

    # Pass 1
    cmd1 = [
        "ffmpeg", "-i", input_path,
        "-af", f"loudnorm=I={target_lufs}:TP=-1:LRA=11:print_format=json",
        "-f", "null", "-"
    ]
    result1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=300)

    try:
        loudness_data = parse_loudnorm_json(result1.stderr)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=500, detail="音声解析に失敗しました")

    # Pass 2
    loudnorm_filter = (
        f"loudnorm=I={target_lufs}:TP=-1:LRA=11:"
        f"measured_I={loudness_data['input_i']}:"
        f"measured_LRA={loudness_data['input_lra']}:"
        f"measured_TP={loudness_data['input_tp']}:"
        f"measured_thresh={loudness_data['input_thresh']}:"
        f"offset={loudness_data['target_offset']}:"
        f"linear=true:print_format=summary"
    )

    cmd2 = [
        "ffmpeg", "-i", input_path,
        "-af", loudnorm_filter,
        "-c:v", "copy",
        "-y", output_path
    ]
    result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=600)

    if result2.returncode != 0:
        raise HTTPException(status_code=500, detail="正規化処理に失敗しました。再度お試しください。")

    background_tasks.add_task(cleanup_files, file_id)

    cl_str = f"{target_cl:+.1f}".replace("+", "plus").replace("-", "minus")
    filename = f"normalized_CL{cl_str}dB.mp4"
    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
