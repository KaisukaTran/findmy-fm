# Bypass the Telegram SNI block with a Cloudflare Worker (free, no VPS)

This network blocks `api.telegram.org` at the TLS/SNI layer (the handshake is reset by
deep-packet inspection — verified: in-app TLS fragmentation does **not** get through, the DPI
reassembles the stream). `workers.dev` (Cloudflare) **is** reachable here, so a tiny Cloudflare
Worker that reverse-proxies the Bot API gives the bot a working 2-way path with a one-line
config change.

```
bot  --HTTPS to <name>.workers.dev (SNI not blocked)-->  Cloudflare Worker
Worker  --fetch from Cloudflare edge (not blocked)-->  api.telegram.org
```

## 1. Deploy the Worker (~5 min)
1. Sign in at https://dash.cloudflare.com → **Workers & Pages** → **Create** → **Create Worker**.
2. Replace the default code with this and **Deploy**:

```js
// Telegram Bot API reverse proxy. The bot calls
//   https://<name>.workers.dev/bot<token>/<method>
// and this forwards it to api.telegram.org unchanged.
export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = "https://api.telegram.org" + url.pathname + url.search;
    const resp = await fetch(target, {
      method: request.method,
      headers: request.headers,
      body: (request.method === "GET" || request.method === "HEAD") ? undefined : request.body,
    });
    // Pass the body + status straight back to the bot.
    return new Response(resp.body, { status: resp.status, headers: resp.headers });
  },
};
```

3. Copy the Worker URL, e.g. `https://findmy-tg.<your-subdomain>.workers.dev`.

## 2. Point the bot at it
In `.env`:

```
TELEGRAM_API_BASE=https://findmy-tg.<your-subdomain>.workers.dev
```

Restart the app. `app/notify.py` builds every call (`sendMessage`, long-poll `getUpdates`,
command replies) from `TELEGRAM_API_BASE`, so both **alerts and 2-way commands** now route
through the Worker. Default (unset) keeps the direct `https://api.telegram.org`.

## 3. Verify
- `POST /api/telegram/test` should return `{"sent": true}`.
- From the Telegram chat, send `/status` — you should get a reply.

## Notes / trade-offs
- **The bot token transits Cloudflare** (it's in the request path). Fine for a paper-trading
  bot you control; if you want the token to touch no third party, run the same reverse proxy
  on your own VPS (nginx/caddy `proxy_pass https://api.telegram.org;`) and set `TELEGRAM_API_BASE`
  to that host instead.
- Cloudflare's free Worker tier (100k req/day) is far above a 5s long-poll bot's needs.
- This does **not** need admin rights or a kernel driver (unlike GoodbyeDPI/zapret-style TCP
  desync). It's a pure config + 15-line Worker.
- Alternative already working with zero of this: the **Discord** channel ([[discord-notify-channel]]).
