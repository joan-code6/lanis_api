# LANIS wrapper API reference

LANIS is an unofficial wrapper around Schulportal Hessen (SPH). SPH is the official learning
platform; the routes below are LANIS routes that retrieve or modify data in SPH.

Base URL: `https://lanis-backend.joancode.dev`

Interactive documentation: `https://lanis-backend.joancode.dev/documentation`

Authenticated endpoints require `X-Session-Token: <access_token>`. JSON is used unless noted.

## Authentication and public endpoints

| Method | Path | Input | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | — | Service health |
| GET | `/metrics/stats` | — | Aggregate service statistics |
| POST | `/login` | `{school_id, username, password}` | Return access and refresh tokens |
| POST | `/auth/refresh` | `{refresh_token}` | Return a new access token |
| POST | `/logout` | auth header | Invalidate tokens and session |
| GET | `/school-list` | — | All districts and schools |
| GET | `/school-list/district/{district_id}` | path ID | Schools in a district |
| GET | `/school-list/search?q=...` | query text | Search school names/locations |

Access tokens expire after the `expires_in` seconds returned by login (normally 3600). Refresh
tokens are long-lived secrets.

## Authenticated reads

| Method | Path | Parameters | Purpose |
| --- | --- | --- | --- |
| GET | `/apps` | — | Available app records |
| GET | `/modules` | — | Available module records |
| GET | `/benutzer` | — | User profile |
| GET | `/kalender` | — | Calendar overview/filter metadata |
| GET | `/kalender/events` | `year=0`, `start=year`, `category`, `search`, `target`, `view_id` | Filtered events |
| GET | `/kalender/event/{event_id}` | optional `view_id` | Event detail |
| GET | `/vertretungsplan` | `include_raw=false` | Schulportal substitution plan |
| GET | `/stundenplan` | — | Timetable |
| GET | `/dateispeicher` | `folder_id=0` | File-storage folder |
| GET | `/dateispeicher/search` | `q` | Search file storage |
| GET | `/lerngruppen` | — | Study-group overview |
| GET | `/nachrichten/headers` | `get_type=All`, `last=0` | Conversation headers |
| GET | `/nachrichten/{conversation_id}` | `last=0` | Conversation messages |
| GET | `/nachrichten/search` | `q` | Search recipients |
| GET | `/meinunterricht` | — | Course overview |
| GET | `/meinunterricht/course/{course_id}` | — | Course entries and files |
| GET | `/meinunterricht/file/{file_hash}` | auth optional for cached files | Binary course file |
| GET | `/meinunterricht/entry` | `url` | Entry detail |
| GET | `/meinunterricht/weekly` | — | Weekly course view |
| GET | `/meinunterricht/submissions` | — | Submissions |
| GET | `/search/semantic` | `q`, `top_k=20` (1–100) | Optional semantic search |

Common `get_type` values include `All`, `Unread`, and `Sent`; use values returned or accepted by
the user's SPH instance rather than inventing a mailbox value. Treat response shapes as
endpoint-owned and check `success` where present; applet payloads vary by enabled SPH modules.

## Writes

| Method | Path | Body | Effect |
| --- | --- | --- | --- |
| POST | `/nachrichten/send` | `{recipients: [id], subject, body}` | Send new message |
| POST | `/nachrichten/reply` | `{conversation_id, body, to: "all"}` | Reply to conversation |
| POST | `/nachrichten/mark-read` | JSON string containing conversation ID | Mark read |
| POST | `/meinunterricht/homework-done` | form fields `course_id`, `entry_id`, `done` | Change done state |

Get explicit user confirmation immediately before invoking any of these.

## DSBmobile

All DSB routes require the Schulportal access header plus separate DSB credentials.

| Method | Path | JSON body | Purpose |
| --- | --- | --- | --- |
| POST | `/dsb/login` | `{username, password}` | Test or establish DSB login |
| POST | `/dsb/plan-urls` | `{username, password}` | Find plan iframe URLs |
| POST | `/dsb/plan` | `{username?, password?, plan_index: 0, plan_url?, include_raw: false}` | Parse one plan |

## Browser SSO app launch

Open `/app/{app_name}?token=<url-encoded-access-token>` in the user's browser. The route returns an
HTML redirect flow rather than JSON. Never share or log this URL.

For executable examples and route sequences, read [examples.md](examples.md).
