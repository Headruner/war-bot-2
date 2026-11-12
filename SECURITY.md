# Security Policy

## Supported Versions

Use this section to tell people about which versions of your project are
currently being supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 5.1.x   | :white_check_mark: |
| 5.0.x   | :x:                |
| 4.0.x   | :white_check_mark: |
| < 4.0   | :x:                |

## Reporting a Vulnerability

Use this section to tell people how to report a vulnerability.

Tell them where to go, how often they can expect to get an update on a
reported vulnerability, what to expect if the vulnerability is accepted or
declined, etc.

## Privacy: access to in-game mail

The scope `esi-mail.read_mail.v1` lets the structure scout open the mails of a character. It exists to find structures
that pilots name in mails (a structure link followed by its owner corporation's link).

- **Read**: for every character that granted the scope, the mail headers and the body of each mail not parsed before
  (every sender and every mail, there is no filter; a mail sent to several characters is opened once). The body is
  parsed in memory for `showinfo` links and dropped.
- **Stored**: only the structure ID, its type ID, the link text (the structure's name), the owner corporation ID, the
  mail ID and the mail's timestamp (`structure_mail_finds`, `mail_reads`, `scout_mail`). Everyone who may open the
  Structures page sees the resulting structures, never which character's mail named them.
- **Never stored, cached or logged**: the mail body, subject, sender and recipients. Mail requests bypass the ESI
  response cache, and a body that cannot be parsed is reported without a piece of its text. A test dumps the database,
  the logs and the cache after a scan to prove it (`test_no_mail_text_reaches_the_database_the_logs_or_the_response_cache`).
- **Opt out**: EVE SSO grants all requested scopes or none, so there is no per-character choice at sign-in; a
  character added before the scope was requested is not read until it is added again. Per instance, set
  `structures.mail_enabled: false` or leave `esi-mail.read_mail.v1` out of `EVE_OPTIONAL_SCOPES`. The scan state of a character that
  no longer exists is pruned at the next scout run; the structures already found stay in the shared list.
