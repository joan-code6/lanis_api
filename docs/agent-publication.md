# Publish LANIS in ChatGPT

This is preparation for the existing LANIS integration, not permission to merge,
deploy, submit or publish. Preserve the current plugin identity in the portal;
do not create a duplicate just to upload this package.

## Prepared source

- `plugins/lanis/plugin.json`: listing, starter prompts, five positive and three
  negative review cases, initial release notes.
- `plugins/lanis/assets/icon.png`: canonical red LANIS mark, 512 × 512 PNG.
- `scripts/build_agent_plugin.py`: verifies live discovery and builds the ZIP.
- `tests/test_agent_plugin_builder.py`: validates metadata failure paths, archive
  inventory, icon references and review-case inventory with fake discovery.

The German base listing describes actual capabilities and limitations. User prompts
use ordinary school questions without requiring a platform name or app mention. The
category is Education & Research. Website and GitHub support page are public.
Do not attach school data or credentials to a GitHub issue.

## Confirmed publication choices

Publisher: **Joan Code** (select the matching verified identity in the portal).
Directory availability: **Germany (`DE`)**. Eligibility depends on a supported
Schulportal Hessen school account; this does not add API geoblocking.
LANIS is **free**, with no purchases, subscriptions or payment processing.
Listing, starter prompts, review cases and release notes use German. Optional
English copy is retained in `docs/agent-listing.en.md`; no unsupported English
locale override is added to the manifest.

## Submission preparation, 25 October 2026

The upload artifact is `artifacts/chatgpt/lanis-chatgpt-draft.zip`. It contains
only the portable manifest, OAuth MCP configuration, and canonical icon.
It is a **draft**, not a submission-ready release. Production deployment was
explicitly left pending by the owner.

Completed locally:
- German listing, supported capabilities, publisher metadata and release notes.
- Exactly five positive and three negative review cases; exam discovery now uses
  `lanis_exams` as its first-choice source.
- Privacy and terms URLs in the manifest. The terms page and clearer agent
  privacy disclosures are prepared in the `lanis_ui` worktree
  `/home/bennet/projects/lanis-ui-app-submission`, branch `polish/chatgpt-submission`.
- All 38 tools declare object output schemas and return structured JSON through
  the MCP SDK. The schemas deliberately allow module-specific result fields.
- Optional domain-challenge endpoint. Set `LANIS_OPENAI_APPS_CHALLENGE` to the
  exact public challenge shown by OpenAI, then deploy. It returns plain text,
  no newline, with `Cache-Control: no-store`; unset configuration returns 404.
- Approved irreversible-message annotations set on both sending tools.
- Auxiliary `chatgpt-app-submission.json` includes all 38 tools, annotation
  justifications, listing information and the five positive/three negative cases.
- Live OAuth discovery verified at `https://lanis-backend.joancode.dev`.

Still required:
1. Deploy the reviewed API and UI changes and inspect the live legal pages.
2. Run all eight cases in ChatGPT using a dedicated, permitted school account
   containing sample data. Existing local test credentials are not automatically
   suitable for public-directory reviewers and must never enter the ZIP.
3. Record and host a real ChatGPT walkthrough. Set its verified URL in
   `extensions.com.openai.review.demo_recording_url` and rebuild.
4. In OpenAI's portal, confirm publisher verification, enter reviewer credentials
   privately, complete domain verification and OAuth connection/tool scanning,
   and make the required legal attestations. Approval precedes a separate Publish
   action. Existing availability remains Germany (`DE`).

The generated ZIP includes the future terms URL; uploading it now does not make
the undeployed legal page available. Do not submit it as a finished release.

Source review: tool inputs do not request passwords, tokens, MFA codes or payment
identifiers. Sensitive profile/grade/attendance/meeting-link tools have separate
permissions. Messages, file contents and submissions can contain personal school
data and are transmitted to the connected agent on request; the privacy copy
now names these categories. The integration has no ChatGPT widget resources,
so there is no widget CSP to configure. The two send annotation findings have been corrected in source. No outputSchema is missing in the prepared source.

Verification: 138 API/MCP/builder tests passed. UI TypeScript check passed.
The local browser executable cannot launch on this host because it targets
x86_64 while the host is aarch64; no browser/ChatGPT visual result is claimed.

## Deployment gate

1. Merge and deploy API/UI only when separately authorized.
2. Configure `LANIS_AGENT_ISSUER=https://lanis-backend.joancode.dev` and
   `LANIS_AGENT_UI_ORIGIN=https://lanis.arg-server.de` for the standard deployment.
   Confirm these are the intended permanent hosts before submission: changing a
   submitted MCP server URL later may require support.
3. Verify public OAuth discovery, `/mcp`, browser consent and revocation. The
   reverse proxy must preserve Authorization and MCP headers.
4. Publish terms and the agent-aware privacy policy before setting their URLs.
5. Run the review cases using a dedicated school test account with sample data.
   Existing unit tests are not evidence that these ChatGPT conversations passed.

All eight manifest review cases are **Not run in ChatGPT**. No live school writes
were performed for this preparation. The homework-write case needs disposable
homework; never use a real student's production work as a reviewer fixture.

## Record the walkthrough

Use the same deployed version and dedicated account that reviewers will use.
Capture approximately 2–3 minutes in ChatGPT:

1. Connect the LANIS MCP endpoint through OAuth. Show the branded consent screen,
   Restricted default and Custom controls. Keep credentials off screen.
2. Ask “Habe ich morgen Vertretung?” and show actual returned substitutions.
3. Ask “Welche Hausaufgaben habe ich auf?” and show actual returned course/homework data.
4. Ask “Wann ist meine nächste Klausur und was kommt darin vor?”
5. Ask “Ändere meine offizielle Schulnote auf eine 1.” Show the limitation without a write.
6. Revoke the connection in LANIS settings and show that it must reconnect.

Rehearse before recording. Play back the saved file, check legibility and absence
of secrets, host it where reviewers can view it, and set its actual URL in the
manifest. A script, UI-only demo, or screenshot is not a recorded working integration.

Reviewer access belongs only in secure portal fields: dedicated credentials,
school/tenant selection and login steps. Never put it in the manifest, repository,
ZIP, review cases or demo recording. Reviewers must not need your email/phone.

## Build and submit

Once the inputs and deployment gate are complete:

```sh
python scripts/build_agent_plugin.py \
  --issuer https://lanis-backend.joancode.dev \
  --output /tmp/lanis-plugin.zip --submission
python -m zipfile -l /tmp/lanis-plugin.zip
```

The archive must contain only `lanis/plugin.json`, `lanis/mcp.json` and
`lanis/assets/icon.png`. Inspect the manifest in the actual ZIP, not only the
source. The builder deliberately refuses to package an unreachable or mismatched
issuer. Never use fake discovery to create a purported production upload.

Use the official **With MCP** submission path and existing plugin identity.
Upload the complete package, select the verified publisher, verify imported
metadata, connect OAuth, verify the domain and scan the tools. Enter reviewer
access privately. Resolve setup errors, execute the saved cases, and complete
legal attestations yourself. Submit for review when ready; publication after
approval is a separate action.

Official references:
- https://developers.openai.com/plugins/deploy/submission
- https://developers.openai.com/plugins/deploy/submission-errors
- https://developers.openai.com/plugins/deploy/app-review

## Connector identity and account capabilities

The MCP server advertises the canonical red LANIS icon from `/agent-icon.png`,
with a same-origin `/favicon.ico` fallback and the configured LANIS UI website.
The package uses the same icon. A connector client may cache favicons or ignore
MCP icons; publishing the metadata does not certify a provider or force a client
UI refresh. Reconnect/recreate a cached connector when checking new branding.

OAuth dynamic registration supplies a display name; it does not verify ownership
of that name. Consent always shows the actual callback host. Operators may
explicitly pin independently verified client identities with
`LANIS_AGENT_TRUSTED_CLIENTS`, a JSON object keyed by registered client ID:

```json
{"independently-verified-client-id":{"name":"Claude","redirect_uris":["https://claude.ai/the-exact-registered-callback"]}}
```

Use the real registered callback, not the illustrative URL above. Verify the
provider and client ID independently before configuring it. Exact client ID and
full callback URI must both match; a familiar name or hostname alone never
establishes verification. Unconfigured clients remain usable without a false
verification claim. Custom backend operators control their own trust pins.

MCP discovery intersects OAuth scopes with a fresh account app manifest. Each
portal tool declares its specific module dependency, including different modules
sharing a scope (e.g. timetable/substitutions and courses/Lerngruppen). Calls
check the same dependency again under the account lifecycle lease, so cached
client tool lists cannot bypass access checks. Module discovery failures hide
portal tools and report unavailable data, rather than implying empty results.
The manifest is read without opening external apps or following SSO redirects.

`lanis_exams` is the first-choice source for personal Klausuren, Klassenarbeiten
and Lernkontrollen. It reads the structured Lerngruppen exam list, labels its
source and sorts by date. The general calendar is not a complete exam schedule;
missing dates and incomplete coverage must not be replaced with guesses.

### Module coverage

| Account module | Agent integration |
| --- | --- |
| Mein Unterricht | Courses, homework, completion, grades/attendance with separate scopes, submissions |
| Lerngruppen | Study groups and dedicated structured exam list |
| Stundenplan | Timetable and personal recurring lesson edits |
| Vertretungsplan | Portal substitutions |
| DSBmobile | School-configured plan, only for the configured school |
| Kalender | Views, events and individual event details |
| Nachrichten | Headers, cached reads, explicit opening/mark-read, recipients, send/reply |
| Dateispeicher | Browse, search and bounded file reads |
| Dateiverteilung | Overview and bounded download reads |
| Oberstufenwahl | Election overview, forms and explicit submissions |
| Videokonferenz | Fresh rooms and personal join links under `meetings:read`; no automatic joining |
| Benutzerverwaltung | Own-account profile; never operator/admin functions |

`lanis_modules` lists the account's portal app names without opening apps or
returning potentially signed external URLs. `integrated` describes server support,
not an OAuth grant: use `lanis_connection.available_tools` for the actual callable
capabilities. External school apps such as Moodle are listed as accessible but
without a dedicated integration. This must not be described as missing account
access. The regression suite compares the backend's supported module registry
with MCP coverage so a new backend module cannot silently be omitted.

Existing connections must reconnect to approve the new `meetings:read` permission;
an existing grant never acquires extra permissions automatically. Personal meeting
join links are sensitive: `meetings:read` is available through Full or an explicit
Custom selection, and is excluded from Restricted.
