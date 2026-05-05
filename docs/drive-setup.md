# Google Drive backend setup

Fisherman doesn't ship its own OAuth client — that requires a verified
Google Cloud project + branded consent screen, which is real
operational cost. Until we have that, you BYO client.

One-time setup, ~10 minutes.

## 1. Create an OAuth client

1. Go to https://console.cloud.google.com/.
2. Create a new project (or use one you already own).
3. Enable the **Google Drive API**: APIs & Services → Library →
   "Google Drive API" → Enable.
4. Configure the OAuth consent screen (External, yourself as test user).
5. APIs & Services → Credentials → Create Credentials → OAuth client ID
   → Desktop app → name "fisherman".
6. Download the JSON. You need the `client_id` and `client_secret`.

## 2. Mint a refresh token

The simplest manual flow:

```sh
# Replace CLIENT_ID with yours
open "https://accounts.google.com/o/oauth2/v2/auth?client_id=CLIENT_ID\
&redirect_uri=urn:ietf:wg:oauth:2.0:oob\
&response_type=code\
&scope=https://www.googleapis.com/auth/drive.file\
&access_type=offline\
&prompt=consent"
```

Sign in, approve the scope, copy the verification code Google shows.

Then exchange the code for tokens:

```sh
curl -s -X POST https://oauth2.googleapis.com/token \
  -d code=CODE \
  -d client_id=CLIENT_ID \
  -d client_secret=CLIENT_SECRET \
  -d redirect_uri=urn:ietf:wg:oauth:2.0:oob \
  -d grant_type=authorization_code
```

Response includes `refresh_token`. Save it.

## 3. Configure fisherman

```sh
fisherman storage configure-drive \
  --client-id  YOUR_CLIENT_ID \
  --client-secret YOUR_CLIENT_SECRET \
  --refresh-token YOUR_REFRESH_TOKEN
```

Restart the daemon. Fisherman will create a top-level "fisherman"
folder in your Drive and put encrypted blobs there.

## Caveats

- **Quota**: Google Drive's API allows ~10k requests / day per user
  for free tier. The daemon batches uploads, but very high capture
  rates may hit limits. If you do, switch to S3 / R2 / Hetzner SB.
- **Scope**: We only request `drive.file` (apps see only files they
  create), so fisherman cannot read your other Drive content.
- **Refresh token expiry**: Tokens issued through "Testing" consent
  screens expire after 7 days. Either publish your OAuth client app
  ("In production" status — Google re-checks) or remint the token
  weekly. For long-running production deployments, S3 or WebDAV are
  less fiddly.
