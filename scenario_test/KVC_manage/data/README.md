# Data

`source/contend_lru60_S32.jsonl` is the frozen 32-session 8.4k prompt source.
`gen_lpstd.py` builds the 16-victim + 4-attacker P180 dataset.

```bash
python3 data/gen_lpstd.py --out data/generated/lpstd_large_v16_a4x16.jsonl
```

Generated dataset MD5 is recorded in `config/scenario.json`.
