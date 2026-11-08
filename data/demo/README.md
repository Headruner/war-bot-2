# Demo database

`evedm.sqlite3` holds 10 fictional characters at real systems (Jita, Perimeter, Uedama, Dodixie, Amarr, Old Man Star,
1DQ1-A, a wormhole, Hek; one needs re-authorization, one lacks the ship scope). Tokens are dummies encrypted with the key
`demo-secret-key-change-me`.

```
# Docker (Linux / Windows): mount this folder as /app/data (Linux: chown -R 10001:10001 data/demo first)
docker run --rm -p 8080:8080 -v "$PWD/data/demo:/app/data" -e EVEDM_DEMO=1 -e EVEDM_SECRET_KEY=demo-secret-key-change-me evedm:0.3.0
# local
EVEDM_DATA_DIR=data/demo evedm --demo serve
```

Recreate it with `EVEDM_DATA_DIR=data/demo evedm --demo db seed-demo --force`.
