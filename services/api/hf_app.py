"""
Hugging Face Spaces launcher for Sherpa AI Mentor API.
HF Spaces (Gradio SDK) exposes port 7860 — uvicorn runs the FastAPI app there.
No Docker needed; HF installs requirements.txt automatically.
"""
import uvicorn
from app.main import app  # noqa: F401 — triggers config + route registration

if __name__ == "__main__" or True:
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=7860,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
