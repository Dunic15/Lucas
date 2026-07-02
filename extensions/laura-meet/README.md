# Laura for Google Meet

Chrome extension that adds a small Laura button inside `meet.google.com`.

It does not use Google Meet's native **Add people** dialog. Instead, it sends the
current meeting URL to Laura's backend, which asks Recall.ai to join the call.
For Google Meet, the host may still need to admit Laura from the waiting room.

## Install locally

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select this folder:
   `/Users/duccioo/Desktop/Laura/extensions/laura-meet`
5. Join a Google Meet call.
6. Click **Send Laura** in the bottom-right Meet overlay.

## Configure backend

The default backend is:

```text
https://laura-avatar.onrender.com
```

To use a custom production domain later, open the extension's **Details** page in
Chrome, click **Extension options**, and save the new HTTPS backend URL.

The extension requests HTTPS host permission so it can keep working after the
backend moves from Render to the custom domain.
