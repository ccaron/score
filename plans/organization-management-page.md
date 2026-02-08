# Plan: Organization Management Page

## Goal
Create a new `/admin/organization` page with an expandable tree view for managing the complete organizational hierarchy: Leagues → Seasons → Divisions → Team Registrations → Rosters.

## Current State
- Separate isolated pages exist for leagues, seasons, divisions, registrations, rosters
- No unified view showing relationships
- Hard to understand context when managing rosters
- Location: `src/score/cloud.py` has individual endpoints scattered throughout

## Proposed UI Structure

```
Organization
─────────────────────────────────────────────────────────────────
[+ Add League]

▼ Bay Area Adult League (BAAL)                              [rec]
  │
  ├─▼ 2025-2026 Season                           Jan 2025 - Apr 2026
  │   │
  │   ├─▼ A Division                                    [+ Add Team]
  │   │   │
  │   │   ├─▼ Ice Dogs (DOG)                         12 players [+ Add Player]
  │   │   │   ├─ #12 John Smith (C) - C
  │   │   │   ├─ #8  Jane Doe (LW) - A
  │   │   │   └─ #21 Bob Wilson (D)
  │   │   │
  │   │   └─▼ Polar Bears (PBR)                       8 players [+ Add Player]
  │   │       └─ ...
  │   │
  │   └─▼ B Division                                    [+ Add Team]
  │       └─ ...
  │
  └─▼ 2024-2025 Season (archived)
      └─ ...

▶ National Hockey League (NHL)                     [professional]
  └─ (collapsed)
```

## Tree Node Types

| Level | Entity | Expandable | Actions |
|-------|--------|------------|---------|
| 1 | League | Yes | + Add Season, Edit |
| 2 | Season | Yes | + Add Division |
| 3 | Division | Yes | + Add Team (Registration) |
| 4 | Team Registration | Yes | + Add Player |
| 5 | Player (Roster Entry) | No | Remove |

## Implementation

### 1. New Endpoint: `GET /admin/organization`

Single endpoint that:
1. Fetches all leagues
2. For each league, fetches seasons (via league_seasons)
3. For each season, fetches divisions (via league_season_divisions or team_registrations)
4. For each division, fetches team registrations
5. For each registration, fetches roster entries with player names

**Data structure:**
```python
{
  "leagues": [
    {
      "league_id": "baal",
      "name": "Bay Area Adult League",
      "league_type": "rec",
      "seasons": [
        {
          "season_id": "2025-2026",
          "name": "2025-2026 Season",
          "start_date": "2025-01-01",
          "divisions": [
            {
              "division_id": "div-a",
              "name": "A Division",
              "registrations": [
                {
                  "registration_id": "reg-dogs-2025",
                  "team_name": "Ice Dogs",
                  "team_abbrev": "DOG",
                  "roster_count": 12,
                  "roster": [
                    {"player_id": 1, "full_name": "John Smith", "jersey_number": 12, "position": "C", "is_captain": true}
                  ]
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

### 2. SQL Queries

**Query 1: Leagues**
```sql
SELECT league_id, name, league_type, description FROM leagues ORDER BY name
```

**Query 2: Seasons for league** (via league_seasons junction)
```sql
SELECT s.season_id, s.name, s.start_date, s.end_date
FROM seasons s
JOIN league_seasons ls ON s.season_id = ls.season_id
WHERE ls.league_id = ?
ORDER BY s.start_date DESC
```

**Query 3: Divisions with registrations for league+season**
```sql
SELECT DISTINCT d.division_id, d.name
FROM divisions d
JOIN team_registrations tr ON d.division_id = tr.division_id
WHERE tr.league_id = ? AND tr.season_id = ?
ORDER BY d.name
```

**Query 4: Team registrations for division**
```sql
SELECT tr.registration_id, t.team_id, t.name as team_name, t.abbreviation,
       (SELECT COUNT(*) FROM roster_entries re
        WHERE re.registration_id = tr.registration_id AND re.removed_at IS NULL) as roster_count
FROM team_registrations tr
JOIN teams t ON tr.team_id = t.team_id
WHERE tr.league_id = ? AND tr.season_id = ? AND tr.division_id = ?
ORDER BY t.name
```

**Query 5: Roster entries for registration**
```sql
SELECT re.id, re.player_id, p.full_name, re.jersey_number, re.position,
       re.roster_status, re.is_captain, re.is_alternate
FROM roster_entries re
JOIN players p ON re.player_id = p.player_id
WHERE re.registration_id = ? AND re.removed_at IS NULL
ORDER BY re.jersey_number, p.last_name
```

### 3. HTML/CSS Structure

**Tree node pattern:**
```html
<div class="tree-node level-1" data-id="baal" data-type="league">
  <div class="node-header" onclick="toggleNode(this)">
    <span class="toggle-icon">▼</span>
    <span class="node-name">Bay Area Adult League</span>
    <span class="node-badge rec">rec</span>
    <button class="btn-add" onclick="openModal('season', 'baal')">+ Season</button>
  </div>
  <div class="node-children">
    <!-- Nested season nodes -->
  </div>
</div>
```

**CSS classes:**
- `.tree-node.level-1` through `.level-5` for indentation
- `.node-header` - Clickable row with flex layout
- `.toggle-icon` - Rotates 90° when collapsed
- `.node-children` - Container for nested nodes, hidden when collapsed
- `.node-badge` - Colored badges (league-type, position, captain/alternate)

### 4. Modal Forms

**Add League Modal:**
- league_id (text, required)
- name (text, required)
- league_type (dropdown: professional, amateur, rec)
- description (textarea)

**Add Season Modal (context: league_id):**
- season_id (text, required)
- name (text, required)
- start_date (date, required)
- end_date (date)
- Also creates league_seasons junction record

**Add Division Modal (context: league_id, season_id):**
- division_id (text, required)
- name (text, required)
- division_type (dropdown)
- Also creates league_season_divisions junction record (if needed)

**Add Team Registration Modal (context: league_id, season_id, division_id):**
- team_id (dropdown of existing teams, or create new)
- registration_id (auto-generated or manual)

**Add Player Modal (context: registration_id):**
- player_id (searchable dropdown of existing players)
- jersey_number (number)
- position (dropdown: C, LW, RW, D, G)
- roster_status (dropdown: active, injured, scratched)
- is_captain, is_alternate (checkboxes)

### 5. JavaScript Functions

```javascript
// Tree navigation
toggleNode(header)           // Expand/collapse node
loadChildren(node)           // Lazy-load children if not loaded

// Modal management
openModal(type, ...context)  // Open appropriate modal with context
closeModal()
submitModal()                // POST to appropriate endpoint

// CRUD operations - use existing endpoints
POST /admin/leagues
POST /admin/seasons + POST to create league_seasons
POST /admin/divisions
POST /admin/team-registrations
POST /admin/roster-entries
```

### 6. Existing Endpoints to Reuse

| Action | Endpoint | Model |
|--------|----------|-------|
| Create league | `POST /admin/leagues` | League |
| Create season | `POST /admin/seasons` | Season |
| Create division | `POST /admin/divisions` | Division |
| Create registration | `POST /admin/team-registrations` | TeamRegistration |
| Add player to roster | `POST /admin/roster-entries` | RosterEntry |
| List teams (for dropdown) | `GET /admin/teams-v2` | - |
| List players (for dropdown) | `GET /admin/players?format=json` | - |

### 7. New Endpoint Needed

**POST /admin/league-seasons** - Link a season to a league
```python
@app.post("/admin/league-seasons")
async def create_league_season(league_id: str, season_id: str, rule_set_id: str = None):
    # Insert into league_seasons table
```

## Files to Modify

| File | Changes |
|------|---------|
| `src/score/cloud.py` | Add `GET /admin/organization` (~200 lines), add `POST /admin/league-seasons` |
| `src/score/static/admin.css` | Add tree view styles (~80 lines) |
| `src/score/cloud.py` | Update `admin_nav()` to add "Organization" link |

## Implementation Order

1. **Add navigation link** - Add "organization" to ADMIN_NAV_ITEMS
2. **Create league-seasons endpoint** - POST to link seasons to leagues
3. **Create organization endpoint** - Build nested data structure
4. **Generate HTML** - Tree view with expandable nodes
5. **Add CSS** - Tree styling, indentation, toggle icons
6. **Add JavaScript** - Toggle, modals, form submission
7. **Test** - Full hierarchy creation flow

## Testing

1. Start fresh: `make run`
2. Visit `http://localhost:8001/admin/organization`
3. Create a league → season → division → team registration → add players
4. Verify tree expands/collapses correctly
5. Verify existing data (from seed) displays properly
6. Test with empty database

## Edge Cases

- League with no seasons
- Season with no divisions (show "No divisions" message)
- Division with no teams
- Team with no players
- Handling tournaments (separate from league+season path)
