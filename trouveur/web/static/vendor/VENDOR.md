# Vendored third-party assets

Everything here is served from our own origin on purpose.

A `<script>` or `@font-face` pointing at a CDN puts a third party on the critical path of a page
whose entire content is somebody's job search: the provider learns the reader's IP, the page they
were on and when, on every single load — and if it is slow, blocked by a corporate proxy or simply
down, htmx never arrives and every saved/applied/dismissed button silently does nothing, with no
error and nothing on the page to say so. Neither failure is one an operator would ever see.

These are committed rather than fetched at build time so that a fresh clone works offline and
local development loads exactly the bytes production serves. That is also why there is no build
step: see the no-Node rule in `AGENTS.md`.

Re-fetching any of these changes what every browser downloads, so treat it like a dependency bump:
run the command, check the digest below changed only if you meant it to, and say so in the commit.

| File | Version | Licence | Fetched |
|---|---|---|---|
| `htmx.min.js` | htmx 2.0.4 | 0BSD (`LICENSE-htmx.txt`) | 2026-10-06 |
| `inter-latin.woff2` | Inter v20, variable 100–900, `latin` subset | SIL OFL 1.1 (`LICENSE-inter.txt`) | 2026-10-06 |
| `inter-latin-ext.woff2` | Inter v20, variable 100–900, `latin-ext` subset | SIL OFL 1.1 (`LICENSE-inter.txt`) | 2026-10-06 |

## Reproducing them

```bash
cd trouveur/web/static/vendor

curl -sfL -o htmx.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
# Must print the hash that base.html carried while htmx was still loaded from unpkg:
openssl dgst -sha384 -binary htmx.min.js | openssl base64 -A
# sha384-HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+

# The two subset URLs come out of the Google Fonts stylesheet, which serves woff2 only to a
# browser-shaped User-Agent. The FILES are OFL and ours to host; the stylesheet is only how
# their URLs are discovered, and nothing at runtime touches that origin.
curl -sfL -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36' \
  'https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap'
```

## Both Latin subsets, and why that is not 133 KB

`app.css` declares each subset with its own `unicode-range`, so a browser downloads `latin-ext`
only when the page actually contains a character in it. An ordinary German or Austrian edition
costs 48 KB and never asks for the second file.

It is there because the corpus is European and company and city names arrive spelled as their
boards spell them — Kraków, Plzeň, Timișoara, İstanbul. Without that subset those characters fall
back to the system font mid-word, which is exactly the kind of seam this typeface was adopted to
remove.
