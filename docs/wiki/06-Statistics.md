# Statistics — EVE War Bot v2

The bot tracks the number of wars declared on the 1st of each month to provide a snapshot of the "War Meta."

## Data Collection
- **Metric:** Total unique war IDs declared on the 1st of the month.
- **Source:** ESI `/wars/` and internal SQLite `wars` table.
- **Filter:** Only wars declared between `00:00:00` and `23:59:59` UTC on the first day of the month.

## Output Format
The `stats` command provides the following:
- **Monthly Count:** Number of wars for the month.
- **Trend:** Comparison with the previous month (if available).
- **Top Entities:** Most frequently appearing corporations in these wars.

## How to Run
Run the following command via Docker:
```bash
docker compose run --rm eve-war-bot stats --months 12
```
