# Fisherman Website

Public product site for Fisherman. It is intentionally separate from the
daemon, macOS app, self-hosted server, and Cloud/CVM deployment paths.

```bash
npm install
npm run dev
npm run build
```

GitHub Actions builds this directory through `.github/workflows/website.yml`.
Website-only changes should not trigger the main product CI or CVM deploy path.
