# Ideas
1. Create in #api.py a system that takes Tasks and runs them one by one and makes sure it only takes a specific amount of recources at a time. Its supposed to be running alongside the main api server and shouldnt impact the apis performance.

2. add to the new Que system that per api request for a login it creates a task that takes the users benutzerdaten (/benutzter) and stores them in a db. Before it should do that it should check if the user already exists in the db and only update the data if something changed.


# ToDo:

# Done:
- [x] Created `task_queue.py` - Background task queue system with:
  - Priority-based task scheduling (HIGH, NORMAL, LOW)
  - Configurable max concurrent tasks (default: 2)
  - Retry logic with exponential backoff
  - Task status tracking (pending, running, completed, failed)
  - Queue statistics endpoint
  
- [x] Created `user_metrics_db.py` - SQLite database for user metrics:
  - Stores user data from /benutzerverwaltung.php
  - Hash-based change detection (only updates if data changed)
  - Tracks first_seen, last_updated, update_count
  - Stats endpoint for aggregate data
  
- [x] Integrated into `api.py`:
  - Task queue starts on API startup, stops gracefully on shutdown
  - Login endpoint queues a LOW priority task to fetch user data
  - New `/metrics/stats` endpoint shows database and queue statistics
  
- [x] Added `aiosqlite` to requirements.txt
