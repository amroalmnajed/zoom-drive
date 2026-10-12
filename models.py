from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo
import config

class ZoomError(RuntimeError):
    pass

@dataclass
class Recording:
    meeting_id: str
    meeting_uuid: str
    topic: str
    host_email: str
    start_time: Optional[datetime]
    duration_min: Optional[float]
    file_id: str
    file_size: Optional[int]
    download_url: str
    recording_type: str
    participants: Optional[list[str]] = None
    resolution: Optional[str] = None
    bitrate: Optional[str] = None
    thumbnail_url: Optional[str] = None

    @property
    def key(self) -> str:
        return self.file_id or f"{self.meeting_uuid}:{self.recording_type}"

    def local_start(self) -> Optional[datetime]:
        if not self.start_time:
            return None
        return self.start_time.astimezone(ZoneInfo(config.LOCAL_TIMEZONE))


@dataclass
class ApprovalRequest:
    recording: Recording
    index: int
    total: int
    suggested_key: str
    matched_keyword: Optional[str]

@dataclass
class ApprovalDecision:
    action: Literal["upload", "skip", "quit"]
    folder_key: Optional[str] = None

@dataclass
class BatchRequest:
    recordings: list[Recording]
    selected_keys: set[str]
    action: Optional[Literal["upload", "skip"]] = None
    folder_key: Optional[str] = None

