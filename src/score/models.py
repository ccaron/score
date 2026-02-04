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
    # Organizational context
    league_name: Optional[str] = None
    season_name: Optional[str] = None
    division_name: Optional[str] = None


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


# ---------- League/Season/Division Models ----------

class League(BaseModel):
    """League organization."""
    league_id: str
    name: str
    league_type: Optional[str] = None  # "professional", "amateur", "rec"
    description: Optional[str] = None
    website: Optional[str] = None
    logo_url: Optional[str] = None


class Season(BaseModel):
    """Time period for competitions."""
    season_id: str
    name: str  # "2025-2026", "Winter 2026"
    start_date: str  # ISO 8601
    end_date: Optional[str] = None


class Division(BaseModel):
    """Team grouping."""
    division_id: str
    name: str
    division_type: Optional[str] = None  # "conference", "division", "bracket", "pool"
    parent_division_id: Optional[str] = None
    description: Optional[str] = None


class Tournament(BaseModel):
    """Time-bound event (alternative to league+season)."""
    tournament_id: str
    name: str
    start_date: str
    end_date: str
    location: Optional[str] = None
    tournament_type: Optional[str] = None  # "championship", "invitational", "playoff"
    description: Optional[str] = None


# ---------- Team/Player Models ----------

class Team(BaseModel):
    """Team organization."""
    team_id: str
    name: str
    city: Optional[str] = None
    abbreviation: Optional[str] = None  # "TOR", "MTL"
    team_type: Optional[str] = None  # "franchise", "club", "pickup"
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None


class Player(BaseModel):
    """Individual athlete."""
    player_id: int
    first_name: str
    last_name: str
    full_name: str
    birth_date: Optional[str] = None
    birth_city: Optional[str] = None
    birth_country: Optional[str] = None
    height_inches: Optional[int] = None
    weight_pounds: Optional[int] = None
    shoots_catches: Optional[str] = None  # "L", "R"
    public_email: Optional[str] = None
    public_phone: Optional[str] = None


# ---------- Venue Models ----------

class Rink(BaseModel):
    """Physical venue."""
    rink_id: str
    name: str
    address: Optional[str] = None
    city: Optional[str] = None
    province_state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    parking_info: Optional[str] = None
    notes: Optional[str] = None


class RinkSheet(BaseModel):
    """Ice surface within a rink."""
    sheet_id: str
    rink_id: str
    name: str  # "Sheet A", "Main Rink"
    surface_type: Optional[str] = None  # "NHL", "Olympic"
    capacity: Optional[int] = None


class Official(BaseModel):
    """Referee or linesman."""
    official_id: str
    first_name: str
    last_name: str
    full_name: str
    certification_level: Optional[str] = None


# ---------- Rule Set Models ----------

class RuleSet(BaseModel):
    """League rule configuration."""
    rule_set_id: str
    name: str  # "NHL Rules", "Youth U12", "Adult Rec"
    description: Optional[str] = None
    # Game structure
    num_periods: int = 3
    period_length_min: int = 20
    intermission_length_min: int = 15
    overtime_length_min: Optional[int] = None
    overtime_type: Optional[str] = None  # "sudden_death", "full_period"
    # Gameplay rules
    icing_rule: str = "hybrid"  # "touch", "hybrid", "no_touch"
    offside_rule: str = "standard"  # "standard", "no_offside"
    body_checking: bool = True
    # Point system
    points_win: int = 2
    points_loss: int = 0
    points_tie: int = 1
    points_otl: int = 1  # Overtime loss
    # Roster rules
    max_roster_size: Optional[int] = None
    min_players_to_start: Optional[int] = None
    max_players_dressed: Optional[int] = None


class Infraction(BaseModel):
    """Penalty infraction defined for a rule set."""
    rule_set_id: str
    code: str  # "TRIP", "HOOK", "SLASH"
    name: str  # "Tripping", "Hooking", "Slashing"
    description: Optional[str] = None
    default_severity: str  # "minor", "major", "misconduct"
    default_duration_min: int
    allows_minor: bool = True
    allows_major: bool = False
    allows_misconduct: bool = False
    allows_match: bool = False
    is_active: bool = True
    display_order: Optional[int] = None


# ---------- Registration/Roster Models ----------

class TeamRegistration(BaseModel):
    """Team competing in a context (league+season or tournament)."""
    registration_id: str
    team_id: str
    # Context: League+Season OR Tournament (mutually exclusive)
    league_id: Optional[str] = None
    season_id: Optional[str] = None
    tournament_id: Optional[str] = None
    # Division within that context
    division_id: str


class RosterEntry(BaseModel):
    """Player on a team's roster for a period."""
    registration_id: str
    player_id: int
    jersey_number: Optional[int] = None
    position: Optional[str] = None  # "C", "LW", "RW", "D", "G"
    roster_status: str = "active"  # "active", "injured", "scratched"
    is_captain: bool = False
    is_alternate: bool = False


class SparePlayer(BaseModel):
    """Player available to sub."""
    player_id: int
    league_id: str
    season_id: str
    positions: Optional[list[str]] = None  # ["C", "LW", "D"] or ["G"]
    skill_level: Optional[str] = None  # "A", "B", "C"
    notes: Optional[str] = None
    is_active: bool = True


# ---------- Playoff Models ----------

class PlayoffBracket(BaseModel):
    """Playoff structure."""
    bracket_id: str
    league_id: str
    season_id: str
    division_id: Optional[str] = None  # NULL if league-wide
    name: str
    format: str  # "single_elimination", "double_elimination"
    num_teams: Optional[int] = None


class PlayoffSeries(BaseModel):
    """Series within a bracket."""
    series_id: str
    bracket_id: str
    round: int  # 1 = first round, 2 = semi, etc.
    series_number: Optional[int] = None  # Position in round
    higher_seed_registration_id: Optional[str] = None
    lower_seed_registration_id: Optional[str] = None
    format: str  # "single_game", "best_of_3", "best_of_5", "best_of_7"
    winner_registration_id: Optional[str] = None


# ---------- Game Models (New) ----------

class GameCreate(BaseModel):
    """Request to create a new game."""
    game_id: str
    rink_id: str
    sheet_id: Optional[str] = None
    home_registration_id: str
    away_registration_id: str
    scheduled_start: str  # ISO 8601
    period_length_min: int
    num_periods: int = 3
    game_type: str = "regular"  # "regular", "playoff", "exhibition"
    series_id: Optional[str] = None
    playoff_game_number: Optional[int] = None


class GameInfo(BaseModel):
    """Game information with full context."""
    game_id: str
    rink_id: str
    sheet_id: Optional[str] = None
    home_registration_id: str
    away_registration_id: str
    scheduled_start: str
    period_length_min: int
    num_periods: int = 3
    game_type: str = "regular"
    game_status: str = "scheduled"  # "scheduled", "in_progress", "final", "postponed", "cancelled"
    series_id: Optional[str] = None
    playoff_game_number: Optional[int] = None
    # Derived fields (from joins)
    home_team_name: Optional[str] = None
    away_team_name: Optional[str] = None
    home_team_abbrev: Optional[str] = None
    away_team_abbrev: Optional[str] = None
    rink_name: Optional[str] = None


# ---------- Event Official Assignment ----------

class GameOfficialAssignment(BaseModel):
    """Official assigned to a game."""
    game_id: str
    official_id: str
    role: str  # "referee", "linesman", "scorekeeper"
