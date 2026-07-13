"""
Tutti Pur Baseline endpoints to add to app.py
Add these lines at the end of app.py (before the last line if needed)
"""

# ─── Tutti Pur Baseline (S3 Upload + AI Analyze) ─────────────────────────────
from pur_baseline_service import (
    handle_s3_upload,
    handle_analyze_content,
    init_pur_baseline_db,
)

# Initialize the DB table on startup
init_pur_baseline_db()


@app.post("/api/tutti/upload-to-s3")
async def tutti_upload_to_s3(
    file: UploadFile = File(...),
    s3_bucket: str = Form("beads-photos-harry"),
    s3_key: str = Form(""),
    source_path: str = Form(""),
    file_name: str = Form(""),
) -> dict:
    """Upload a file to S3 via EC2 (Tutti Pur Baseline)."""
    content = await file.read()
    return handle_s3_upload(
        file_content=content,
        file_name=file_name or file.filename or "unknown",
        s3_bucket=s3_bucket,
        s3_key=s3_key or None,
        source_path=source_path,
    )


@app.post("/api/tutti/analyze-content")
async def tutti_analyze_content(
    file: UploadFile = File(...),
    s3_key: str = Form(""),
    s3_url: str = Form(""),
    file_name: str = Form(""),
    file_hash: str = Form(""),
    source_path: str = Form(""),
    db_name: str = Form(""),
    db_table: str = Form(""),
    db_columns: str = Form(""),
) -> dict:
    """
    Analyze equation CSV content and store in EC2 SQLite.
    Parses Marker Name, Well Number, Control/Canine/Feline/Equine Equation
    and inserts into makerpreeqn table.
    """
    content = await file.read()
    return handle_analyze_content(
        file_content=content,
        file_name=file_name or file.filename or "unknown",
        file_hash=file_hash,
        s3_key=s3_key,
        s3_url=s3_url,
        source_path=source_path,
        db_name=db_name,
        db_table=db_table,
        db_columns=db_columns,
    )
