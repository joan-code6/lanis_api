# Android companion app support

The Capacitor frontend and native widgets live in `lanis_ui`, on its `feat/android-app` branch. The backend branch adds Android FCM delivery alongside the existing Web Push transport. Production has not been changed.

## Configuration

Install the updated `requirements.txt` and configure:

- `LANIS_FIREBASE_PROJECT_ID`: Firebase project ID for the Android package `de.lanis.app`.
- `GOOGLE_APPLICATION_CREDENTIALS`: path to a server-side Google service-account JSON file authorized to send FCM messages.

Keep that key outside the repository and frontend assets. The Android client separately uses the Firebase-generated `google-services.json`, which contains client configuration, at `android/app/google-services.json` in `lanis_ui`.

Android push can be configured without VAPID. Existing browser push still uses its existing VAPID configuration. The public browser `/notifications/config` response stays unchanged.

## Authenticated endpoints

All endpoints use the same `X-Session-Token` authentication as other app endpoints.

- `GET /notifications/android/config` → `{ "success": true, "configured": boolean }`.
- `POST /notifications/android` accepts `token`, a per-login UUID `binding`, and optional `previous_token`; saves the registration and removes the previous token belonging to this account.
- `DELETE /notifications/android` accepts `token` and removes only this account's registration.

Android devices share the existing `push_subscriptions` store through an internal `fcm://` endpoint marker. No new database migration is needed. Browser subscription validation still rejects this scheme. Existing logout, account deletion, canonical identity handling, per-device delivery tracking, notification preview settings, and retry queues apply to Android devices too.

The FCM data payload includes the per-login binding. The Android receiver rejects notifications whose binding no longer matches the local login; an old queued notification cannot be shown for a newly signed-in account. A binding is routing metadata, not an authentication credential.

Delivery uses FCM HTTP v1 with a short-lived OAuth token, bounded request times and bounded payload sizes. Unregistered devices are removed. Permission/configuration/quota failures retain the registration for retry. Logs do not include FCM authorization credentials or notification contents.

## Refresh and limitations

Timetable widgets use the existing `GET /stundenplan/view?view_mode=rolling&plan_mode=personal`. Android background workers authenticate with the normal refresh endpoint. No special widget credentials or school credentials are embedded in the app.

The existing server poller still determines when SPH changes are detected. Notifications remain subject to configured categories, class selection, preview settings, time windows, server availability and FCM/Android delivery behavior. No production service restart or deployment is part of these source changes.

## Tests

```sh
python -m pytest tests/test_android_push.py tests/test_message_notifications.py tests/test_nachrichten_routes.py tests/test_account_data.py
```
