# Fisherman Website

Public product site for Fisherman. It is intentionally separate from the
daemon, macOS app, self-hosted server, and Cloud/CVM deployment paths.

```bash
npm install
npm run dev
npm run build
```

GitHub Actions builds this directory through `.github/workflows/website.yml`.
Pushes to `main` deploy to Vercel once `VERCEL_TOKEN` is configured as a repo
secret. Website-only changes should not trigger the main product CI or CVM
deploy path.

Create a Vercel token at https://vercel.com/account/tokens and add it as the
GitHub repository secret `VERCEL_TOKEN`. The repo variables
`VERCEL_ORG_ID=team_428cohviSd5wIhBrS8WDSEp0` and
`VERCEL_PROJECT_ID=prj_6YZ4Ym4Zh52j8TizNcUIJZwmkFvc` are already configured.

## DNS

The Vercel project is `sxysuns-projects/fisherman-website`, currently deployed
at `https://fisherman-website-eight.vercel.app`.

Vercel has `app.fisherman.teleport.computer` attached to the project and needs
these DNS records:

```text
Type: TXT
Name: _vercel
Value: vc-domain-verify=app.fisherman.teleport.computer,82de78d026cbd864c010
```

```text
Type: CNAME
Name: app.fisherman
Target: cname.vercel-dns.com
Proxy: DNS only
```

If editing a delegated `fisherman.teleport.computer` zone instead of the root
`teleport.computer` zone, use `Name: app` for the CNAME. Keep the TXT record at
`_vercel.teleport.computer` unless Vercel shows a different verification name.
