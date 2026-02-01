# Device Management Guide

This guide explains how to manage device assignments using the cloud API admin endpoints.

## Overview

Each score-app device generates a unique ID from its MAC address (e.g., `dev-a1b2c3`). Devices register themselves automatically when they first connect to the cloud. You then assign them to specific rinks and sheets using the admin API.

## Admin Endpoints

Base URL: `http://localhost:8001` (or your cloud server)

### 1. List All Devices

See all registered devices and their current assignments.

```bash
curl http://localhost:8001/admin/devices
```

**Response:**
```json
{
  "devices": [
    {
      "device_id": "dev-a1b2c3",
      "rink_id": "rink-alpha",
      "sheet_name": "Sheet 1",
      "device_name": "Alpha Main Display",
      "is_assigned": true,
      "first_seen_at": 1706832000,
      "last_seen_at": 1706835600,
      "notes": "Raspberry Pi 4, 8GB"
    },
    {
      "device_id": "dev-d4e5f6",
      "rink_id": null,
      "sheet_name": null,
      "device_name": null,
      "is_assigned": false,
      "first_seen_at": 1706832100,
      "last_seen_at": 1706835700,
      "notes": null
    }
  ]
}
```

### 2. Get Device Details

Get information about a specific device.

```bash
curl http://localhost:8001/admin/devices/dev-a1b2c3
```

### 3. Update Device (Assign/Reassign)

Update a device's assignment and details. This is the main endpoint for assigning devices.

```bash
curl -X PUT http://localhost:8001/admin/devices/dev-a1b2c3 \
  -H "Content-Type: application/json" \
  -d '{
    "rink_id": "rink-alpha",
    "sheet_name": "Sheet 1",
    "device_name": "Alpha Main Display",
    "notes": "Raspberry Pi 4, 8GB RAM"
  }'
```

**Fields (all optional):**
- `rink_id` - Must exist in rinks table. Required together with sheet_name to mark as assigned.
- `sheet_name` - Any descriptive name (e.g., "Sheet 1", "Sheet 2", "Practice Rink")
- `device_name` - Friendly name for the device
- `notes` - Any additional notes (hardware specs, location, etc.)

**Response:**
```json
{
  "status": "ok",
  "message": "Device dev-a1b2c3 updated",
  "device": {
    "device_id": "dev-a1b2c3",
    "rink_id": "rink-alpha",
    "sheet_name": "Sheet 1",
    "device_name": "Alpha Main Display",
    "is_assigned": true,
    "first_seen_at": 1706832000,
    "last_seen_at": 1706835600,
    "notes": "Raspberry Pi 4, 8GB RAM"
  }
}
```

**Partial Updates:**

You can update just specific fields:

```bash
# Just change the sheet name
curl -X PUT http://localhost:8001/admin/devices/dev-a1b2c3 \
  -H "Content-Type: application/json" \
  -d '{"sheet_name": "Sheet 2"}'

# Just update notes
curl -X PUT http://localhost:8001/admin/devices/dev-a1b2c3 \
  -H "Content-Type: application/json" \
  -d '{"notes": "Upgraded to 16GB RAM"}'
```

### 4. Unassign a Device

Remove the assignment (device becomes unassigned):

```bash
curl -X DELETE http://localhost:8001/admin/devices/dev-a1b2c3/assignment
```

**Response:**
```json
{
  "status": "ok",
  "message": "Device dev-a1b2c3 unassigned"
}
```

## Operational Workflow

### When a New Device Connects

1. **Device boots up** - Shows "DEV-A1B2C3 ⚠ Not Assigned" on screen
2. **Rink staff contacts you** - Sends photo or text: "Please assign DEV-A1B2C3 to Sheet 1"
3. **You check the device list:**
   ```bash
   curl http://localhost:8001/admin/devices | jq '.devices[] | select(.device_id=="dev-a1b2c3")'
   ```
4. **You assign it:**
   ```bash
   curl -X PUT http://localhost:8001/admin/devices/dev-a1b2c3 \
     -H "Content-Type: application/json" \
     -d '{"rink_id": "rink-alpha", "sheet_name": "Sheet 1"}'
   ```
5. **Device picks up new config** - Within seconds, shows "DEV-A1B2C3 Sheet 1"

### Moving a Device to a Different Sheet

Just update with the new sheet name:

```bash
curl -X PUT http://localhost:8001/admin/devices/dev-a1b2c3 \
  -H "Content-Type: application/json" \
  -d '{"sheet_name": "Sheet 2"}'
```

The rink_id stays the same, only sheet changes.

### Swapping Devices Between Sheets

Easy way - just update each device:

```bash
# Move device 1 to Sheet 2
curl -X PUT http://localhost:8001/admin/devices/dev-111111 \
  -H "Content-Type: application/json" \
  -d '{"sheet_name": "Sheet 2"}'

# Move device 2 to Sheet 1
curl -X PUT http://localhost:8001/admin/devices/dev-222222 \
  -H "Content-Type: application/json" \
  -d '{"sheet_name": "Sheet 1"}'
```

### Moving a Device to a Different Rink

Update both rink and sheet:

```bash
curl -X PUT http://localhost:8001/admin/devices/dev-a1b2c3 \
  -H "Content-Type: application/json" \
  -d '{
    "rink_id": "rink-beta",
    "sheet_name": "Sheet 1"
  }'
```

## Tips

- **Device IDs are permanent** - Generated from MAC address, won't change
- **last_seen_at timestamp** - Shows when device last checked in (helps identify dead devices)
- **Use notes field** - Record hardware specs, purchase date, anything useful
- **Devices auto-register** - No need to manually create them, they appear when they connect
- **Updates are immediate** - Device picks up changes within seconds
- **Partial updates work** - Only send fields you want to change

## Example: Quick Assignment Script

```bash
#!/bin/bash
# assign-device.sh - Quick device assignment helper

CLOUD_URL="http://localhost:8001"
DEVICE_ID="$1"
RINK_ID="$2"
SHEET_NAME="$3"

if [ -z "$DEVICE_ID" ] || [ -z "$RINK_ID" ] || [ -z "$SHEET_NAME" ]; then
  echo "Usage: $0 <device_id> <rink_id> <sheet_name>"
  echo "Example: $0 dev-a1b2c3 rink-alpha \"Sheet 1\""
  exit 1
fi

curl -X PUT "$CLOUD_URL/admin/devices/$DEVICE_ID" \
  -H "Content-Type: application/json" \
  -d "{\"rink_id\": \"$RINK_ID\", \"sheet_name\": \"$SHEET_NAME\"}" \
  | jq .

echo ""
echo "Device $DEVICE_ID assigned to $RINK_ID - $SHEET_NAME"
```

Usage:
```bash
chmod +x assign-device.sh
./assign-device.sh dev-a1b2c3 rink-alpha "Sheet 1"
```

## Error Messages

- `Device {id} not found` - Device hasn't connected yet, wait for first check-in
- `Rink {id} not found` - Need to create the rink first in the database
- `Device must connect at least once` - Device needs to register before assignment

## API Design Notes

The endpoints follow REST conventions:
- `GET /admin/devices` - List resources
- `GET /admin/devices/{id}` - Get one resource
- `PUT /admin/devices/{id}` - Update resource (idempotent)
- `DELETE /admin/devices/{id}/assignment` - Delete sub-resource

This makes it easy to integrate with standard REST clients and frameworks.
