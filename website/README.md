# Fisherman Website

Public product site for Fisherman. It is intentionally separate from the
daemon, macOS app, self-hosted server, and Cloud/CVM deployment paths.

```bash
npm install
npm run dev
npm run build
```

GitHub Actions builds this directory through `.github/workflows/website.yml`.
Pushes to `main` deploy the built site to
`https://app.fisherman.teleport.computer`. Website-only changes should not
trigger the main product CI or CVM deploy path.

## DNS

GitHub Pages is configured with `app.fisherman.teleport.computer` as the custom
domain. The DNS provider must have:

```text
Type: CNAME
Name: app
Target: sxysun.github.io
Proxy: DNS only
```

After the record resolves, enable/enforce HTTPS in GitHub Pages.
