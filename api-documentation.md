# Schulportal Hessen API Documentation

## Overview

The Schulportal Hessen API is a FastAPI wrapper around the `SchulportalHessenAPI` class that provides HTTP endpoints to interact with the Schulportal Hessen (Hessian School Portal) system. It supports multiple concurrent users with session-based authentication and a token-based system.

**Base URL:** `http://localhost:8000` (when running locally)

**Run locally:**
```bash
uvicorn api:app --reload
```

---

## Authentication

### Session Management

The API uses token-based session management to support multiple concurrent users:

- **Session Creation:** On successful login, a unique session token is generated
- **Session Duration:** Sessions expire after 60 minutes of inactivity (TTL: 3600 seconds)
- **Token Usage:** Include the token in the `X-Session-Token` header for all authenticated requests
- **Session Cleanup:** Sessions are automatically purged on expiration or can be manually terminated via logout

### Headers

All authenticated requests require the following header:

```
X-Session-Token: {token}
```

---

## Endpoints

### Authentication

#### POST `/login`

Authenticate a user and create a new session.

**Request Body:**
```json
{
  "school_id": "1234",
  "username": "john.doe",
  "password": "your_password"
}
```

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `school_id` | string | Schul-ID (e.g., 1234) |
| `username` | string | Username without school prefix |
| `password` | string | User password |

**Response:**
```json
{
  "token": "abc123def456...",
  "school_id": "1234",
  "username": "john.doe",
  "encryption_ready": true
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `token` | string | Session token to use for subsequent requests |
| `school_id` | string | School ID of the logged-in user |
| `username` | string | Username of the logged-in user |
| `encryption_ready` | boolean | Indicates if encryption is ready for the session |

**Status Codes:**
- `200 OK` - Login successful
- `401 Unauthorized` - Invalid credentials

---

#### POST `/logout`

Terminate the current session.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "status": "logged_out"
}
```

**Status Codes:**
- `200 OK` - Logout successful
- `401 Unauthorized` - Invalid or expired session token

---

### DSBmobile (Substitution Plan)

These endpoints require a valid `X-Session-Token` from `/login`. DSBmobile uses separate
credentials, so you pass them in the request body.

#### POST `/dsb/login`

Login to DSBmobile and establish a session for substitution plan access.

**Headers:**
- `X-Session-Token: {token}` (required)

**Request Body:**
```json
{
  "username": "{dsb_username}",
  "password": "{dsb_password}"
}
```

**Response:**
```json
{
  "success": true,
  "session_cookie": "{dsb_cookie}",
  "session_id": "{aspnet_session_id}",
  "response_url": "https://www.dsbmobile.de/default.aspx"
}
```

**Status Codes:**
- `200 OK` - Login attempt completed
- `401 Unauthorized` - Invalid or expired session token

---

#### POST `/dsb/plan-urls`

Fetch available substitution plan iframe URLs after login.

**Headers:**
- `X-Session-Token: {token}` (required)

**Request Body:**
```json
{
  "username": "{dsb_username}",
  "password": "{dsb_password}"
}
```

**Response:**
```json
{
  "success": true,
  "plan_urls": ["{plan_url}"],
  "count": 1
}
```

---

#### POST `/dsb/plan`

Fetch and parse the substitution plan table from a plan URL.

**Headers:**
- `X-Session-Token: {token}` (required)

**Request Body:**
```json
{
  "username": "{dsb_username}",
  "password": "{dsb_password}",
  "plan_index": 0,
  "plan_url": "{plan_url}",
  "include_raw": false
}
```

**Response:**
```json
{
  "success": true,
  "plan_url": "{plan_url}",
  "title": "{plan_title}",
  "raw_html": null,
  "tables": [
    {
      "caption": "{caption}",
      "headers": ["{header}"],
      "rows": [
        {"{header}": "{value}"}
      ]
    }
  ]
}
```

---

### System

#### GET `/health`

Check the health status of the API.

**Response:**
```json
{
  "status": "ok"
}
```

**Status Codes:**
- `200 OK` - API is operational

---

### School List

The school list endpoints provide access to public data about schools in Hesse. These endpoints do **not** require authentication.

#### GET `/school-list`

Retrieve all schools organized by district/region.

**Response:**
```json
{
  "success": true,
  "districts": [
    {
      "id": "7",
      "name": "{region_name}",
      "schools": [
        {
          "id": "3354",
          "name": "{school_name}",
          "location": "{city_name}"
        },
        "..."
      ]
    },
    "..."
  ]
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | Indicates if the request was successful |
| `districts` | array | List of all districts with their schools |
| `districts[].id` | string | District ID |
| `districts[].name` | string | District name (e.g., "Bergstraße/Odenwaldkreis") |
| `districts[].schools` | array | List of schools in the district |
| `districts[].schools[].id` | string | School ID |
| `districts[].schools[].name` | string | School name |
| `districts[].schools[].location` | string | City/location of the school |

**Status Codes:**
- `200 OK` - School list retrieved successfully
- `500 Internal Server Error` - Failed to fetch or parse school list

---

#### GET `/school-list/district/{district_id}`

Retrieve schools for a specific district by ID.

**Path Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `district_id` | string | The district ID (e.g., "7") |

**Response:**
```json
{
  "success": true,
  "district": {
    "id": "7",
    "name": "{region_name}",
    "schools": [
      {
        "id": "3354",
        "name": "{school_name}",
        "location": "{city_name}"
      },
      "..."
    ]
  }
}
```

**Status Codes:**
- `200 OK` - District schools retrieved successfully
- `500 Internal Server Error` - Failed to fetch or parse school list

---

#### GET `/school-list/search`

Search for schools by name across all districts (case-insensitive).

**Query Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `q` | string | Yes | School name or partial name to search for |

**Response:**
```json
{
  "success": true,
  "query": "{search_term}",
  "count": 3,
  "results": [
    {
      "district_id": "7",
      "district_name": "{region_name}",
      "school": {
        "id": "3351",
        "name": "{school_name}",
        "location": "{city_name}"
      }
    },
    "..."
  ]
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | Indicates if the request was successful |
| `query` | string | The search term used |
| `count` | integer | Number of matching schools |
| `results` | array | List of matching schools with their district info |

**Status Codes:**
- `200 OK` - Search completed successfully
- `500 Internal Server Error` - Failed to fetch or parse school list

---

### User Information

#### GET `/benutzer`

Retrieve the currently logged-in user's profile information.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "data": {
    "{username}": "...",
    "{firstname}": "...",
    "{lastname}": "...",
    "{email}": "...",
    "..."
  }
}
```

**Status Codes:**
- `200 OK` - User data retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

### Available Modules & Apps

#### GET `/modules`

Retrieve all available modules for the current user.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "modules": [
    {
      "name": "{module_name}",
      "url": "{module_url}",
      "color": "{color_code}",
      "logo": "{logo_class}",
      "folders": ["{folder_name}"],
      "target": "{target}",
      "usable": true,
      "usage": ["{method_name}"]
    },
    "..."
  ]
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | Indicates if the request was successful |
| `modules` | array | List of available modules |
| `modules[].name` | string | Name of the module |
| `modules[].url` | string | URL to access the module |
| `modules[].color` | string | Color code for the module |
| `modules[].logo` | string | CSS class for the module icon |
| `modules[].folders` | array | List of folders the module belongs to |
| `modules[].target` | string | Link target (_blank or _self) |
| `modules[].usable` | boolean | Whether the module is supported by this package |
| `modules[].usage` | array | List of API method names for this module |

**Status Codes:**
- `200 OK` - Modules retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

#### GET `/apps`

Retrieve all available apps for the current user.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "data": {
    "error": "0",
    "folders": [
      {
        "name": "{folder_name}",
        "logo": "{logo_class}",
        "farbe": "{color_code}"
      },
      "..."
    ],
    "entrys": [
      {
        "Name": "{app_name}",
        "Farbe": "{color_code}",
        "Logo": "{logo_class}",
        "Ordner": ["{folder_name}"],
        "link": "{app_link}",
        "target": "{target}"
      },
      "..."
    ],
    "till": {timestamp}
  }
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | Indicates if the request was successful |
| `data` | object | Container for app data |
| `data.error` | string | Error code (0 for success) |
| `data.folders` | array | List of available folders |
| `data.folders[].name` | string | Name of the folder |
| `data.folders[].logo` | string | CSS class for the folder icon |
| `data.folders[].farbe` | string | Color code for the folder |
| `data.entrys` | array | List of available apps/entries |
| `data.entrys[].Name` | string | Name of the app |
| `data.entrys[].Farbe` | string | Color code for the app |
| `data.entrys[].Logo` | string | CSS class for the app icon |
| `data.entrys[].Ordner` | array | List of folders the app belongs to |
| `data.entrys[].link` | string | Link to the app |
| `data.entrys[].target` | string | Link target (_blank or _self) |
| `data.till` | integer | Timestamp until the data is valid |

**Status Codes:**
- `200 OK` - Apps retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

### Messages (Nachrichten)

#### GET `/nachrichten/headers`

Retrieve message headers/list of conversations.

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `get_type` | string | "All" | Filter type: "All", "Unread", "Sent", etc. |
| `last` | integer | 0 | ID of the last message to start pagination from |

**Response:**
```json
{
  "success": true,
  "total": 40,
  "conversations": [
    {
      "id": "{conversation_id}",
      "sender": "{sender_username}",
      "subject": "{subject}",
      "date": "{date}",
      "unread": 0,
      "read": true,
      "..."
    },
    "..."
  ]
}
```

**Notes:**
- Each conversation includes `unread` from the portal (0/1). If missing, the API sets `unread` to 0.
- The API adds `read` as a derived boolean (`read = !unread`).

**Status Codes:**
- `200 OK` - Message headers retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

#### GET `/nachrichten/{conversation_id}`

Retrieve a specific conversation with all messages.

**Headers:**
- `X-Session-Token: {token}` (required)

**Path Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `conversation_id` | string | The ID of the conversation |

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `last` | integer | 0 | ID of the last message to start pagination from |

**Response:**
```json
{
  "success": true,
  "conversation_id": "{conversation_id}",
  "messages": [
    {
      "id": "{message_id}",
      "sender": "{sender_username}",
      "content": "{message_content}",
      "date": "{date}",
      "..."
    },
    "..."
  ]
}
```

**Status Codes:**
- `200 OK` - Conversation retrieved successfully
- `401 Unauthorized` - Invalid or expired session token
- `404 Not Found` - Conversation not found

---

#### GET `/nachrichten/search`

Search for message recipients.

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string | Search query (name, username, etc.) |

**Response:**
```json
{
  "success": true,
  "results": [
    {
      "id": "{user_id}",
      "name": "{full_name}",
      "username": "{username}",
      "type": "{user_type}",
      "..."
    },
    "..."
  ]
}
```

**Status Codes:**
- `200 OK` - Search results retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

#### POST `/nachrichten/send`

Send a new message.

**Headers:**
- `X-Session-Token: {token}` (required)

**Request Body:**
```json
{
  "recipients": ["{user_id_1}", "{user_id_2}"],
  "subject": "{message_subject}",
  "body": "{message_body}"
}
```

**Response:**
```json
{
  "success": true,
  "message_id": "{message_id}"
}
```

**Status Codes:**
- `200 OK` - Message sent successfully
- `400 Bad Request` - Invalid message format
- `401 Unauthorized` - Invalid or expired session token

---

#### POST `/nachrichten/reply`

Send a reply to an existing conversation.

**Headers:**
- `X-Session-Token: {token}` (required)

**Request Body:**
```json
{
  "conversation_id": "{conversation_id}",
  "body": "{reply_body}",
  "to": "all"
}
```

**Response:**
```json
{
  "success": true,
  "details": {
    "back": true,
    "id": "{message_id}"
  }
}
```

**Status Codes:**
- `200 OK` - Reply sent successfully
- `400 Bad Request` - Invalid reply payload
- `401 Unauthorized` - Invalid or expired session token

---

### Courses (Mein Unterricht)

#### GET `/meinunterricht`

Retrieve an overview of all courses.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "courses": [
    {
      "id": "{course_id}",
      "name": "{course_name}",
      "teacher": "{teacher_name}",
      "entries_count": 5,
      "..."
    },
    "..."
  ]
}
```

**Status Codes:**
- `200 OK` - Course overview retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

#### GET `/meinunterricht/course/{course_id}`

Retrieve detailed information about a specific course.

**Headers:**
- `X-Session-Token: {token}` (required)

**Path Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `course_id` | string | The ID of the course |

**Response:**
```json
{
  "success": true,
  "course_id": "{course_id}",
  "course_name": "{course_name}",
  "semester": "{semester}",
  "teacher_short": "{teacher_short}",
  "teacher_full": "{teacher_full}",
  "entry_count": 0,
  "entries": [
    {
      "entry_id": "{entry_id}",
      "date": "",
      "hours": "{hours}",
      "thema": "{thema}",
      "homework": "{homework}",
      "homework_done": false,
      "attendance": "{attendance}",
      "content": "{detail_content}",
      "files": [
        {
          "name": "{file_name}",
          "size": "{size}",
          "url": "{file_url}",
          "download_url": "{download_url}"
        }
      ]
    }
  ],
  "exams": [],
  "marks": [],
  "attendance_summary": []
}
```

**Status Codes:**
- `200 OK` - Course details retrieved successfully
- `401 Unauthorized` - Invalid or expired session token
- `404 Not Found` - Course not found

---

#### GET `/meinunterricht/entry`

Retrieve detailed information about a specific course entry.

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | string | The URL of the entry to fetch details for |

**Response:**
```json
{
  "success": true,
  "entry": {
    "id": "{entry_id}",
    "title": "{entry_title}",
    "content": "{entry_content}",
    "date": "{date}",
    "attachments": [
      {
        "name": "{file_name}",
        "url": "{file_url}",
        "..."
      },
      "..."
    ]
  }
}
```

**Status Codes:**
- `200 OK` - Entry details retrieved successfully
- `401 Unauthorized` - Invalid or expired session token
- `404 Not Found` - Entry not found

---

#### GET `/meinunterricht/weekly`

Retrieve a weekly view of course entries.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "week": {
    "start_date": "{date}",
    "entries": [
      {
        "date": "{date}",
        "course": "{course_name}",
        "entry": "{entry_title}",
        "url": "{entry_url}",
        "..."
      },
      "..."
    ]
  }
}
```

**Status Codes:**
- `200 OK` - Weekly view retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

#### GET `/meinunterricht/submissions`

Retrieve all submissions/tasks that need attention.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "submissions": [
    {
      "id": "{submission_id}",
      "title": "{submission_title}",
      "course": "{course_name}",
      "due_date": "{date}",
      "status": "{status}",
      "url": "{submission_url}",
      "..."
    },
    "..."
  ]
}
```

**Status Codes:**
- `200 OK` - Submissions retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

### Calendar (Kalender)

#### GET `/kalender`

Retrieve the calendar overview page metadata for the current user.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "page_title": "Kalender",
  "calendar": {
    "first_id": "{calendar_view_id}",
    "new_events_count": "{count}",
    "can_write": false,
    "key": "{calendar_key}",
    "public_view": false,
    "institution": "{school_id}",
    "is_admin": false
  },
  "categories": [
    {
      "id": 20,
      "name": "Sonstige Termine",
      "color": "#2e2e2e",
      "logo": ""
    }
  ],
  "groups": [],
  "export_links": [
    {
      "label": "als PDF",
      "url": "kalender.php?a=export..."
    }
  ]
}
```

**Status Codes:**
- `200 OK` - Calendar overview retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

#### GET `/kalender/events`

Retrieve calendar events using the same filter contract as the web UI.

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
- `year` - `0` for the current school year, `1` for the next school year
- `start` - Calendar start mode, default `year`
- `category` - Filter by category id
- `search` - Search text for title, location, or description
- `target` - Zielgruppe filter
- `view_id` - Selected calendar view id

**Response:**
```json
{
  "success": true,
  "events": [
    {
      "id": "{event_id}",
      "title": "{event_title}",
      "category": 20,
      "description": "{description}",
      "start": {"date": "2026-04-29 08:00:00"},
      "end": {"date": "2026-04-29 09:30:00"},
      "all_day": false,
      "new": "",
      "editable": false,
      "properties": {}
    }
  ],
  "count": 1,
  "filters": {
    "year": 0,
    "start": "year",
    "category": "",
    "search": "",
    "target": "",
    "view_id": "{calendar_view_id}"
  }
}
```

**Status Codes:**
- `200 OK` - Calendar events retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

#### GET `/kalender/event/{event_id}`

Retrieve the full payload for a single calendar event.

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
- `view_id` - Selected calendar view id

**Response:**
```json
{
  "success": true,
  "event": {
    "id": "{event_id}",
    "title": "{event_title}",
    "...": "..."
  },
  "filters": {
    "event_id": "{event_id}",
    "view_id": "{calendar_view_id}"
  }
}
```

**Status Codes:**
- `200 OK` - Calendar event retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

### Substitution Plan (Schulportal)

#### GET `/vertretungsplan`

Retrieve the substitution plan from Schulportal Hessen (vertretungsplan.php).

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_raw` | boolean | false | Include raw HTML in the response |

**Response:**
```json
{
  "success": true,
  "mode": "ajax",
  "last_updated": "{timestamp}",
  "days": [
    {
      "date": "{date}",
      "substitutions": [
        {"fach": "{subject}", "klasse": "{class}"}
      ]
    }
  ],
  "count": 1
}
```

**Status Codes:**
- `200 OK` - Substitution plan retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

### Timetable (Stundenplan)

#### GET `/stundenplan`

Retrieve the timetable from stundenplan.php (all and personal views).

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "plan_for_all": [[{"name": "{subject}", "room": "{room}"}]],
  "plan_for_own": [[{"name": "{subject}"}]],
  "hours": [{"label": "1", "start_time": {"hour": 8, "minute": 0}}],
  "week_badge": "{week}"
}
```

**Status Codes:**
- `200 OK` - Timetable retrieved successfully
- `401 Unauthorized` - Invalid or expired session token

---

### File Storage (Dateispeicher)

#### GET `/dateispeicher`

Retrieve files and folders from the dateispeicher.

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `folder_id` | integer | 0 | Folder id to fetch |

**Response:**
```json
{
  "success": true,
  "folder_id": 0,
  "files": [{"id": 1, "name": "{file}"}],
  "folders": [{"id": 2, "name": "{folder}"}]
}
```

#### GET `/dateispeicher/search`

Search files in the dateispeicher.

**Headers:**
- `X-Session-Token: {token}` (required)

**Query Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string | Search query |

**Response:**
```json
{
  "success": true,
  "query": "{query}",
  "results": []
}
```

---

### Study Groups (Lerngruppen)

#### GET `/lerngruppen`

Retrieve study groups and exam data from lerngruppen.php.

**Headers:**
- `X-Session-Token: {token}` (required)

**Response:**
```json
{
  "success": true,
  "groups": [{"id": "{group_id}", "course_name": "{course}"}],
  "exams": [{"id": "{exam_id}", "date": "{date}"}]
}
```

---

## Error Responses

All error responses follow this format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

### Common Error Codes

| Code | Description |
|------|-------------|
| `200 OK` | Request successful |
| `400 Bad Request` | Invalid request parameters or body |
| `401 Unauthorized` | Invalid, expired, or missing session token |
| `404 Not Found` | Requested resource not found |
| `500 Internal Server Error` | Server error |

---

## Examples

### Example: Login and Fetch Messages

```bash
# Step 1: Login
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{
    "school_id": "1234",
    "username": "john.doe",
    "password": "password123"
  }'

# Response:
# {
#   "token": "abc123def456...",
#   "school_id": "1234",
#   "username": "john.doe",
#   "encryption_ready": true
# }

# Step 2: Fetch message headers using the token
curl -X GET "http://localhost:8000/nachrichten/headers?get_type=All" \
  -H "X-Session-Token: abc123def456..."

# Step 3: Logout
curl -X POST http://localhost:8000/logout \
  -H "X-Session-Token: abc123def456..."
```

### Example: Fetch Course Details

```bash
# Get all courses
curl -X GET http://localhost:8000/meinunterricht \
  -H "X-Session-Token: {token}"

# Get a specific course
curl -X GET http://localhost:8000/meinunterricht/course/12345 \
  -H "X-Session-Token: {token}"
```

---

## Notes

- Session tokens expire after 60 minutes of inactivity
- All timestamps are in UTC format
- Response data is anonymized in this documentation; placeholders like `{username}`, `{course_name}` represent actual values
- Multiple concurrent users are supported through session isolation
- The API automatically cleans up expired sessions

