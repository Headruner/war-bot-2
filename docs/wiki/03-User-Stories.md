# User Stories — EVE War Bot v2

| ID | User Story | Acceptance Criteria |
|---|---|---|
| **US-01** | As a **Corporation Leader**, I want to be notified immediately when a war is declared against my entity so that I can alert my officers. | - Notification appears in Discord < 2 mins after ESI update. - Message contains correct corp logos and names. |
| **US-02** | As a **Community Manager**, I want a visual "War Report" in Discord so that my members can see the current score without leaving the app. | - Embed shows "Ships Killed", "ISK Destroyed", and "Efficiency". - Scores update automatically as the war progresses. |
| **US-03** | As a **Player**, I want to see the "Hot Systems" of a war so that I can identify where the most intense combat is occurring. | - Report lists the top 3 systems. - Systems are clickable links to the EVE map. |
| **US-04** | As an **Administrator**, I want to see a summary of wars declared on the first of every month for monthly reporting. | - `stats` command returns a summary of the previous month's wars. |
| **US-05** | As a **Developer**, I want the bot to handle rate limits automatically so that the service remains stable during peak hours. | - Bot handles 429s with `Retry-After` logic. - Bot logs "Error Limit" warnings. |
