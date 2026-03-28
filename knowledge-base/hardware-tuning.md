## Optimal Download Workers for MacBook Pro i9
**Date:** 2026-03-26
**Context:** Tuning concurrent download workers for a MacBook Pro with Intel i9-9880H (8 cores / 16 threads), 64 GB RAM
**Best Practice:**
- File downloads are network I/O bound, not CPU bound — can safely exceed core count
- Optimal worker count: 1.5× logical cores = 12 workers for 16-thread CPU
- Going beyond 16 workers gives diminishing returns and risks hitting Slack rate limits
- Set `DOWNLOAD_MAX_RETRIES=7` for overnight runs over unstable connections
- File descriptor limit (`ulimit -n`) must be well above worker count — macOS default of 1,048,575 is fine
- Monitor with `Activity Monitor → Network` tab to verify bandwidth saturation
- For smaller machines (4 cores / 8 threads): use 6–8 workers
**Keywords:** hardware, workers, concurrency, download, i9, macbook, performance-tuning
