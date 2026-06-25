from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class OcrResult:
    # Keep output schema unified across models / Chuẩn hóa schema output cho mọi model
    doc_id: str
    source_pdf: str
    model_name: str
    page_no: int
    text: str
    confidence: float = 0.0
    blocks: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        # Export serializable dict payload / Xuất payload dạng dict để lưu JSON
        return asdict(self)
