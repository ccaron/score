"""Shared Pydantic models for score-app and score-cloud."""

from typing import Optional
from pydantic import BaseModel


# ---------- Game/Schedule Models ----------

class Game(BaseModel):
    """Game metadata from schedule."""
    game_id: str
    home_team: str
    away_team: str
    home_abbrev: Optional[str] = None
    away_abbrev: Optional[str] = None
    start_time: str  # ISO 8601 format
    period_length_min: int


class ScheduleResponse(BaseModel):
    """Response from schedule endpoint."""
    schedule_version: str
    games: list[Game]


# ---------- Event Models ----------

class Event(BaseModel):
    """Event record for cloud sync."""
    event_id: str
    seq: int
    type: str
    ts_local: str  # ISO 8601 format
    payload: dict


class PostEventsRequest(BaseModel):
    """Request to upload events to cloud."""
    device_id: str
    session_id: str
    events: list[Event]


class PostEventsResponse(BaseModel):
    """Response from event upload."""
    acked_through: int
    server_time: str


# ---------- Goal Models ----------

class Goal(BaseModel):
    """Goal record with attribution."""
    id: str
    team: str  # "home" | "away"
    time: str
    cancelled: bool = False
    scorer_id: Optional[int] = None
    assist1_id: Optional[int] = None
    assist2_id: Optional[int] = None


# ---------- Player/Roster Models ----------

class PlayerInfo(BaseModel):
    """Player information for roster display."""
    player_id: int
    full_name: str
    jersey_number: Optional[int] = None
    position: Optional[str] = None
    status: str = "active"


# ---------- Heartbeat Models ----------

class HeartbeatRequest(BaseModel):
    """Device heartbeat request."""
    device_id: str
    current_game_id: Optional[str] = None
    game_state: Optional[str] = None
    clock_running: Optional[bool] = None
    clock_value_ms: Optional[int] = None
    last_event_seq: Optional[int] = None
    app_version: Optional[str] = None
    ts_local: str


class HeartbeatResponse(BaseModel):
    """Device heartbeat response."""
    status: str
    server_time: str


# ---------- Device Configuration Models ----------

class DeviceConfigResponse(BaseModel):
    """Device configuration from cloud."""
    device_id: str
    is_assigned: bool
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    message: Optional[str] = None


class DeviceInfo(BaseModel):
    """Device information."""
    device_id: str
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    is_assigned: bool
    first_seen_at: int
    last_seen_at: int
    notes: Optional[str] = None


class CreateDeviceRequest(BaseModel):
    """Request to create a new device."""
    device_id: str
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    notes: Optional[str] = None


class CreateRinkRequest(BaseModel):
    """Request to create a new rink."""
    rink_id: str
    name: str


class AssignDeviceRequest(BaseModel):
    """Request to assign a device to a rink."""
    rink_id: str
    sheet_name: str
    device_name: Optional[str] = None
    notes: Optional[str] = None


class UpdateDeviceRequest(BaseModel):
    """Request to update device properties."""
    rink_id: Optional[str] = None
    sheet_name: Optional[str] = None
    device_name: Optional[str] = None
    notes: Optional[str] = None


class DeviceListResponse(BaseModel):
    """Response with list of devices."""
    devices: list[DeviceInfo]


# ---------- App Request Models ----------

class SetTimeRequest(BaseModel):
    """Request to set clock time."""
    time_str: str  # Format: "MM:SS"


class SelectModeRequest(BaseModel):
    """Request to select mode (clock or game)."""
    mode: str  # "clock" or game_id


class AddGoalRequest(BaseModel):
    """Request to add a goal."""
    team: str  # "home" or "away"
    scorer_id: Optional[int] = None
    assist1_id: Optional[int] = None
    assist2_id: Optional[int] = None


class CancelGoalRequest(BaseModel):
    """Request to cancel a goal."""
    goal_id: str


class AddShotRequest(BaseModel):
    """Request to add a shot."""
    team: str  # "home" or "away"


class ChangeScoreRequest(BaseModel):
    """Request to change score directly."""
    team: str  # "home" or "away"
    delta: int  # +1 or -1
