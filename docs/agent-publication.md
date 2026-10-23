# Publish LANIS in ChatGPT

This is preparation for the existing LANIS integration, not permission to merge,
deploy, submit or publish. Preserve the current plugin identity in the portal;
do not create a duplicate just to upload this package.

## Prepared source

- `plugins/lanis/plugin.json`: listing, starter prompts, five positive and three
  negative review cases, initial release notes.
- `plugins/lanis/assets/icon.png`: existing ruby LANIS mark, 512 × 512 PNG.
- `scripts/build_agent_plugin.py`: verifies live discovery and builds the ZIP.
- `tests/test_agent_plugin_builder.py`: validates metadata failure paths, archive
  inventory, icon references and review-case inventory with fake discovery.

The English base listing describes actual capabilities and limitations. The
category is Education & Research. Website and GitHub support page are public.
Do not attach school data or credentials to a GitHub issue.

## Confirmed publication choices

Publisher: **Joan Code** (select the matching verified identity in the portal).
Directory availability: **Germany (`DE`)**. Eligibility depends on a supported
Schulportal Hessen school account; this does not add API geoblocking.
LANIS is **free**, with no purchases, subscriptions or payment processing.
The manifest includes a German listing translation.

## Inputs still needed

Before building the submission version, fill these confirmed values under
`extensions.com.openai` in the manifest:

| Field | Needed value |
| --- | --- |
| `interface.privacyPolicyURL` | Published policy covering external agent access |
| `interface.termsOfServiceURL` | Published applicable terms; an imprint is not a substitute |
| `review.demo_recording_url` | Verified reviewer-accessible recording |

Do not invent publisher verification or attestations. Optional dark icons and
brand colors are omitted; the supplied existing mark works on both themes.

On 23 October 2026, the live UI privacy and imprint pages rendered correctly in
a real browser, but agent-specific privacy coverage is still a deployment check.
No terms page was found in the source. Do not call that field complete by guessing
a URL. The public API discovery URL returned HTTP 404 during preparation.

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
2. Ask “What can you access in my LANIS account?” and show the returned scope limits.
3. Ask “What homework do I have in LANIS?” and show actual returned course/homework data.
4. Ask “Help me plan my next school day using my timetable and substitutions.”
5. Ask “Change my official school grade to an A.” Show the limitation without a write.
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
  --output /tmp/lanis-plugin.zip
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
