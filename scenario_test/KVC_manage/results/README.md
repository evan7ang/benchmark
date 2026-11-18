# Results

Reference result from 179.169.3.2:

- `off`: overall mean 2442.5 ms, p50 1139.8 ms
- `on`: overall mean 1800.1 ms (-26.3%), p50 1083.0 ms
- victim resume p50: 5010.7 -> 272.9 ms
- victim resume hit: 65/66 blocks, 98.4% token

Raw AISBench run directories are intentionally not committed; the summary and
reproduction steps are in `docs/EXPECTED_RESULTS.md`.
