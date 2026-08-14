# Vendored: xterm.js

The one third-party code in this repo. Everything else - the Python backend
and the rest of the web bundle - is hand-written specifically so there is
nothing here to audit but this app's own code. A correct VT100/xterm terminal
emulator (cursor addressing, SGR colour, the alternate screen buffer, unicode
width, kitty keyboard protocol) is a multi-week, easy-to-get-subtly-wrong
undertaking on its own, and it is exactly what `xterm.js` already is and is
tested to be - the same library VS Code, Hyper and most browser-based
terminals (ttyd, Wetty, code-server) build on. Writing an equivalent by hand
for this one dialog was judged not worth reproducing that risk.

Used for exactly one thing: signing in to a CLI whose login is a full-screen
terminal session (Antigravity, as of writing - see `login_tui` in
`aicouncil/config.py`) and cannot be driven through the app's ordinary
scrollback pane. See `app.js`'s `openTuiTerminal` and `connections.py`'s
`SetupSession` for the pty on the other end.

## Provenance

Fetched from jsDelivr (which serves straight from npm) and committed as
static files - no build step, no npm, no CDN dependency at runtime. This app
is meant to run fully offline, including air-gapped; a `<script src="https://...">`
would have broken that the first time Settings opened without a network.

| File | Package | Version | SHA-256 |
|---|---|---|---|
| `xterm.js` | [`@xterm/xterm`](https://www.npmjs.com/package/@xterm/xterm) | 6.0.0 | `14903579ff54664cd72f8e8699e6961a6272c21863ec1c3b118cdc8af5d4a972` |
| `xterm.css` | `@xterm/xterm` | 6.0.0 | `854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6` |
| `addon-fit.js` | [`@xterm/addon-fit`](https://www.npmjs.com/package/@xterm/addon-fit) | 0.11.0 | `ba3ea256ce0620a0992a197d6c9baea64823fc93d8da07a9e366ca9943c18527` |

To re-fetch or update:

```bash
curl -fsSL -o xterm.js       "https://cdn.jsdelivr.net/npm/@xterm/xterm@<version>/lib/xterm.js"
curl -fsSL -o xterm.css      "https://cdn.jsdelivr.net/npm/@xterm/xterm@<version>/css/xterm.css"
curl -fsSL -o addon-fit.js   "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@<version>/lib/addon-fit.js"
sha256sum xterm.js xterm.css addon-fit.js   # update the table above
```

MIT licensed - full text in `LICENSE`.
