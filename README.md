# CASA watcher

Detects new housing offers and pushes them to your Android phone with ntfy. Runs on GitHub Actions (free on public repos).

## Setup
1. Install the **ntfy** app (Google Play or F-Droid) and subscribe to a long random topic, e.g. `casa-amel-k8f3x9q2vz`.
2. Create a **public** GitHub repo and push this folder.
3. Repo > Settings > Secrets and variables > Actions > **Secrets**: add `NTFY_TOPIC` = your topic.
4. Optional **Variables**: `MAX_PRICE`, `MIN_SURFACE`, `DEPARTMENTS` (e.g. `91,78`), `CASA_URL` (+ `ASSUME_SORTED=1`).
5. Repo > Settings > Actions > General > Workflow permissions: **Read and write**.
6. Actions tab > "CASA watcher" > **Run workflow** (first run records existing offers silently, then you get a "watcher activé" push).

## Local checks
    pip install -r requirements.txt
    python casa_watcher.py --dry-run
    NTFY_TOPIC=your-topic python casa_watcher.py --test-ntfy
