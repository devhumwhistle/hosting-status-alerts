# Hosting status alerts

Every five minutes, a GitHub Action reads a public status page's API and
sends a phone notification through [ntfy](https://ntfy.sh) when an incident
or maintenance starts, changes or ends. It can also check that a website
answers.

## Set up

1. Install the ntfy app on your phone and subscribe to a long, random topic
   name. The topic is the only key, so treat it like a password.
2. Add the repository secrets:
   - `NTFY_TOPIC`: that topic.
   - `WATCH_SERVER` (optional): your server's name. Notices about other
     servers are skipped, and notices about yours are marked urgent.
   - `WATCH_SITE` (optional): a URL that should answer.
3. Actions › Check status › Run workflow, with **Send a test notification**
   ticked.

The first run records what's already on the status page and sends nothing.
After that, `state.json` holds what has been seen and the workflow commits it
when it changes.

To try it locally without sending anything:

```bash
NTFY_TOPIC=x python3 check.py --dry-run
```
