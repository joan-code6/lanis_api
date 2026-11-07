# Status incident investigation

Read-only investigation on 2026-11-07; no production changes or restarts.

The latest 2,000 production monitor observations contained 1,838 up, 110 recovery_pending and 52 confirmed degraded observations. Of those 52, 30 combined one login_failed account, one modules_request_failed account and two healthy accounts. The other 22 combined two login_failed accounts and two healthy accounts. These sampled observations do not prove a global SPH outage. No account identifiers or credentials are included here.

The monitor resolved every app redirect before opening modules again, including external apps. Monitoring now skips redirect resolution and stops on the first successful module, reducing avoidable requests and worker deadline pressure. Internal SDK exceptions produce unknown rather than downtime. Different failing features across accounts no longer corroborate a new shared incident.

Regression validation covers monitor exceptions, unrelated account failures, skipping redirect resolution, public status and incident details. Historical incident records are unchanged. Network failures remain observations from one host; no production latency benchmark was performed.
