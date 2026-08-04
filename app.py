"""
Merchant Risk Intelligence.

Entry point. Everything real lives in the mri package:

    mri/policy.py     versioned weights, bands and reason codes
    mri/engine.py     orchestration, timeouts, confidence
    mri/decision.py   score -> reserve percentage and payout delay
    mri/signals/      the seven signal categories
    mri/benchmark.py  the labelled 60-domain harness

Run it:

    pip install -r requirements.txt
    python app.py          # http://localhost:8000
"""
from mri.api import app  # noqa: F401  (uvicorn app:app)

if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
