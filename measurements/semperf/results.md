Bulk values are median rows/s across three fresh processes, each with eight
batches. Parentheses show the one-minute load range at sample boundaries;
the raw records retain all three load averages and every batch time.

| Bulk fixture | 0.4.9 | Before | After |
|---|---:|---:|---:|
| CPU, 200 tokens | 44.73 (9.68–12.60) | 36.70 (9.73–16.95) | 34.41 (9.68–21.65) |
| CPU, mixed | 116.64 (8.08–9.73) | 48.46 (7.38–9.68) | 82.32 (8.08–9.29) |
| Metal, 200 tokens | 307.84 (9.91–12.40) | 116.98 (9.91–12.87) | 155.39 (10.44–11.88) |
| Metal, mixed | 600.87 (10.51–12.87) | 88.31 (9.77–12.87) | 314.28 (10.51–12.12) |

Fresh-process query values are p50 / p95 milliseconds, with the same load
notation. There are twenty samples per mode. The reference uses its
default Apple-silicon MLX lane with daemon use disabled.

| Query mode | 0.4.9 (contended) | Before | After |
|---|---:|---:|---:|
| semantic | 252.433 / 4712.384 (56.75–62.18) | 167.837 / 185.536 (9.16–11.27) | 120.036 / 925.172 (8.62–11.63) |
| hybrid | 885.185 / 9458.152 (55.17–62.18) | 414.670 / 7281.839 (9.16–11.27) | 440.373 / 7193.407 (8.49–12.67) |
| keyword | 180.824 / 2019.307 (55.17–62.18) | 33.948 / 6857.987 (9.16–11.27) | 37.093 / 6807.355 (8.49–17.64) |

The reference query run was explicitly contended after the host supplied no quiet window. Its values are context only, not a matched-host speedup comparison. Before/after query and all bulk sampling conditions were unchanged.

Inside the after-hybrid command, the keyword stage was 382.48 ms p50 / 7144.67 ms p95. This is measured directly; it is not a subtraction of medians.

At the measured mixed-length rate, 130,000 rows project to **6.9 minutes on Metal** and **26.3 minutes on CPU**. These are model-only estimates, including tokenization but excluding indexing/publication, with one window per row.
