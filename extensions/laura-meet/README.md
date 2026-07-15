# Laura for Meetings

Chrome extension that adds a small Laura button inside `meet.google.com`,
`*.zoom.us`, `teams.microsoft.com`, and `teams.live.com`.

It sends the current meeting URL to Laura's backend, which asks Recall.ai to
join the call. The host may still need to admit Laura from the waiting room /
lobby (Meet, Zoom, and Teams can all gate guests).

Per platform:
- **Google Meet** — works from inside the call (the room URL is the join URL).
- **Zoom** — works on `/j/<id>` join pages and the `/wc/<id>` web client; the
  `?pwd=` passcode is preserved.
- **Teams** — works on the `/l/meetup-join/...` or `/meet/<id>` join-link page
  (the in-app URL after joining doesn't carry the meeting link, so send Laura
  from the join page).

## Install locally

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select this folder:
   `extensions/laura-meet`
5. Open a meeting (or its join link).
6. Click **Send Laura** in the bottom-right overlay.

## Configure backend

The default backend is:

```text
https://dhfgfe6yw6.eu-central-1.awsapprunner.com
```

If Chrome already saved the old Render URL, open the extension's **Details** page,
click **Extension options**, and save the AWS URL above. To use a custom
production domain later, save the new HTTPS backend URL in the same options page.

The extension requests HTTPS host permission so it can keep working after the
backend moves from AWS App Runner to the custom domain.
