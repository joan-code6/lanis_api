# LANIS usage examples

These examples call the LANIS wrapper, which in turn communicates with the official Schulportal
Hessen (SPH) platform. Replace placeholder IDs with values returned by earlier calls. Never guess
SPH identifiers.

## Contents

- Shared REST setup
- Find a school and log in
- Discover supported SPH features
- Get the user's profile
- Check the regular timetable and substitutions
- Find calendar events
- Read, send, and reply to messages
- Find courses, homework, submissions, and materials
- Download a course file
- Change homework completion
- Browse and search SPH file storage
- Get study groups and exams
- Use DSBmobile plans
- Launch an SPH SSO app
- Use optional semantic search
- Refresh and log out

## Shared REST setup

Use a protected environment variable for the access token:

```bash
export LANIS_BASE_URL='https://lanis-backend.joancode.dev'
export LANIS_ACCESS_TOKEN='<access_token returned by /login>'
```

For Python examples:

```python
import httpx

base_url = "https://lanis-backend.joancode.dev"
headers = {"X-Session-Token": access_token}
```

Do not put a password, access token, or refresh token into committed code.

## Find a school and log in

Use this sequence when the user knows the school name but not its numeric SPH school ID.

REST sequence: call `/school-list/search`, choose the matching result's `id`, call `/login` with
that ID, then retain the returned access and refresh tokens privately.

REST school search:

```bash
curl -sS "$LANIS_BASE_URL/school-list/search" \
  --get --data-urlencode 'q=Goetheschule Kassel'
```

The agent should execute the login request itself. The `sph_username` and `sph_password` variables
below represent values already supplied by the user or loaded from an approved secret store. Keep
them in memory, do not print them, and do not ask the user to run this code locally:

```python
import httpx

response = httpx.post(
    "https://lanis-backend.joancode.dev/login",
    json={
        "school_id": school_id,
        "username": sph_username,
        "password": sph_password,
    },
    timeout=30,
)
response.raise_for_status()
tokens = response.json()
access_token = tokens["access_token"]
refresh_token = tokens["refresh_token"]
```

Passing the password in this HTTPS request body is required and authorized when the user supplied
it for the LANIS login. Do not refuse the request simply because an HTTP or execution tool carries
the body. Do not echo the body or persist the password after the request.

Use `GET /school-list` only when the user truly needs every district and school. Prefer search for
normal login setup.

## Discover supported SPH features

Call this after login when the user's school configuration is unknown.

REST:

```bash
curl -sS "$LANIS_BASE_URL/modules" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"
```

Inspect `modules[].name`, `modules[].usable`, and `modules[].usage`. A module appearing in SPH does
not necessarily mean the LANIS wrapper can parse it. Use `GET /apps` only when raw app folders,
links, colors, or target metadata are required.

## Get the user's profile

For requests like “What class am I in?” or “Show my SPH account details”:

```bash
curl -sS "$LANIS_BASE_URL/benutzer" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"
```

Read the profile fields inside `data`. Return only fields relevant to the user's question.

## Check the regular timetable and substitutions

For “What lessons do I have today?” use the regular timetable:

```bash
curl -sS "$LANIS_BASE_URL/stundenplan" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"
```

For “Are any lessons cancelled or substituted?” use the SPH substitution plan:

```bash
curl -sS "$LANIS_BASE_URL/vertretungsplan" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --get --data-urlencode 'include_raw=false'
```

To answer “What does my actual day look like?”, retrieve both and combine the regular lessons with
the matching day's substitutions. Do not use DSB routes unless the user specifically uses
DSBmobile.

## Find calendar events

First retrieve calendar metadata if categories or view IDs are unknown:

Call `GET /kalender` first, then read `calendar.first_id` and `categories[].id` before forming the
filtered request.

REST:

```bash
curl -sS "$LANIS_BASE_URL/kalender/events" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --get \
  --data-urlencode 'year=0' \
  --data-urlencode 'start=year' \
  --data-urlencode 'search=Prüfung' \
  --data-urlencode 'view_id=<calendar view id>'
```

For full details, take `events[].id` from that response:

```bash
curl -sS "$LANIS_BASE_URL/kalender/event/<event id>" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --get --data-urlencode 'view_id=<calendar view id>'
```

Use `year=0` for the current school year and `year=1` for the next school year.

## Read messages

For “Do I have unread messages?”:

Call `/nachrichten/headers?get_type=All&last=0`, then keep conversations whose `unread` field is
`1` or whose `read` field is `false`.

REST:

```bash
curl -sS "$LANIS_BASE_URL/nachrichten/headers" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --get --data-urlencode 'get_type=Unread' --data-urlencode 'last=0'
```

Take `conversations[].id` from the response to read one conversation:

```bash
curl -sS "$LANIS_BASE_URL/nachrichten/<conversation id>" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"
```

Reading a conversation and marking it read are separate actions. Do not call
`POST /nachrichten/mark-read` unless the user confirms that state change.

After confirmation, the REST mark-read body is a JSON string, not an object:

```bash
curl -sS "$LANIS_BASE_URL/nachrichten/mark-read" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '"<conversation id>"'
```

## Send a new message

Resolve recipient IDs before drafting or sending:

Call `/nachrichten/search?q=Erika%20Mustermann`, select `results[].id` with the user, then show the
recipient, subject, and body and obtain confirmation before calling `/nachrichten/send`.

REST write after confirmation:

```bash
curl -sS "$LANIS_BASE_URL/nachrichten/send" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{
    "recipients": ["<recipient id>"],
    "subject": "Frage zu den Hausaufgaben",
    "body": "Guten Tag, ..."
  }'
```

Recipient display names are not valid substitutes for IDs.

## Reply to a conversation

Read the conversation first, draft against its actual context, then obtain confirmation:

Call `/nachrichten/{conversation_id}` first, then show the proposed reply and recipient selector
before calling `/nachrichten/reply`.

REST write after confirmation:

```bash
curl -sS "$LANIS_BASE_URL/nachrichten/reply" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{
    "conversation_id": "<conversation id>",
    "body": "Vielen Dank für die Information.",
    "to": "all"
  }'
```

## Find courses, homework, and materials

For “What homework do I have?” start with courses, then inspect relevant course IDs:

Call `/meinunterricht`, choose a returned `courses[].id`, then call
`/meinunterricht/course/{course_id}` and inspect `entries[].homework`, `homework_done`, `files`, and
`entry_id`.

REST:

```bash
curl -sS "$LANIS_BASE_URL/meinunterricht" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"

curl -sS "$LANIS_BASE_URL/meinunterricht/course/<course id>" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"
```

For a cross-course weekly view, start with `/meinunterricht/weekly`. If an item contains a detail
URL, pass that exact URL to `/meinunterricht/entry`:

```bash
curl -sS "$LANIS_BASE_URL/meinunterricht/entry" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --get --data-urlencode 'url=<entry URL returned by the API>'
```

For work awaiting upload or submission, use `/meinunterricht/submissions` rather than scanning
every course.

## Download a course file

Call `/meinunterricht/course/{course_id}` first. The LANIS wrapper rewrites file records with a
`file_hash` and wrapper-hosted `download_url`.

REST binary download:

```bash
curl -fSL "$LANIS_BASE_URL/meinunterricht/file/<file hash>" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --output '<safe local filename>'
```

## Change homework completion

Obtain `course_id` and `entry_id` from the course response. Show the exact course, homework entry,
and new done state, then obtain confirmation.

REST uses form data, not JSON:

```bash
curl -sS "$LANIS_BASE_URL/meinunterricht/homework-done" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --form-string 'course_id=<course id>' \
  --form-string 'entry_id=<entry id>' \
  --form-string 'done=true'
```

## Browse and search SPH file storage

Start at folder `0`, then reuse a returned child folder ID:

Call `/dateispeicher?folder_id=0`, choose a returned `folders[].id`, then call the same route with
that ID to browse the child folder.

Search directly when the filename or topic is known:

```bash
curl -sS "$LANIS_BASE_URL/dateispeicher/search" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --get --data-urlencode 'q=Mathematik'
```

## Get study groups and exams

Use LernGruppen for group and exam data, not for normal Mein Unterricht course material:

```bash
curl -sS "$LANIS_BASE_URL/lerngruppen" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"
```

## Use DSBmobile plans

DSBmobile credentials are separate from SPH credentials. The LANIS access token is still required
to call these wrapper routes.

Call `POST /dsb/plan-urls` with the separate DSB username and password, choose a returned plan URL
or index, then call `POST /dsb/plan`. Both calls also require the LANIS access-token header.

Use an explicit `plan_url` only when it came from `/dsb/plan-urls`; do not fetch arbitrary URLs.

## Launch an SPH SSO app

First call `/modules` and use an actual `modules[].name`. Open
`/app/{app_name}?token=<URL-encoded access token>` only in the user's browser. This URL contains the
access token and must never be quoted, logged, or sent to another person.

## Use optional semantic search

When the deployment enables it, semantic search can search across indexed SPH content without
manually querying each applet:

```bash
curl -sS "$LANIS_BASE_URL/search/semantic" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN" \
  --get \
  --data-urlencode 'q=Mathematik Hausaufgaben nächste Woche' \
  --data-urlencode 'top_k=10'
```

If the hosted service returns 404, report that the optional route is not deployed and fall back to
the relevant calendar, course, message, or file-storage route.

## Refresh and log out

Refresh an expired access token:

```python
response = httpx.post(
    f"{base_url}/auth/refresh",
    json={"refresh_token": refresh_token},
    timeout=30,
)
response.raise_for_status()
access_token = response.json()["access_token"]
headers["X-Session-Token"] = access_token
```

Retry the failed read once. Do not automatically retry a write.

Logout is an explicit state-changing action:

```bash
curl -sS -X POST "$LANIS_BASE_URL/logout" \
  -H "X-Session-Token: $LANIS_ACCESS_TOKEN"
```

After logout, discard both access and refresh tokens.
