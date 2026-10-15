# Clips4U private YouTube import backend

This service lets your existing GitHub Pages mobile clipper import **short public YouTube videos that you own or have permission to reuse**. The backend accepts only canonical YouTube links, checks a private access key, retrieves a single video (10 minutes maximum, 90 MB maximum), converts it to MP4 if necessary, returns it to the browser, and deletes temporary files.

**Limits:** It cannot fetch private, member-only, DRM-protected, live, or login-restricted videos. YouTube may block data-center IP addresses and deny imports even for public videos. YouTube's terms and video copyrights may limit downloads. The official YouTube Data API does not give generic download access to other videos. When this happens, download your own original from its original storage / YouTube Studio and select it through the existing local file picker.

## Deploy to Render

1. Sign up or sign in at https://dashboard.render.com/ .
2. Create a **New Web Service**, connect GitHub, choose `8myztggm5j-eng/clips4u-website` and branch `main`.
3. Choose **Docker** as the Language/Runtime. Set **Root Directory** to `backend` and use the default `Dockerfile` inside that root.
4. Choose the service name `clips4u-import` or another unique name, and a plan you are comfortable paying for (a free tier may be very slow, sleep when idle, and fail with larger videos).
5. Under **Environment**, add `CLIPS4U_IMPORT_KEY` with a unique private random string **32 or more characters**. Do **not** commit the secret to GitHub or send it in a chat. Optionally add `CLIPS4U_ALLOWED_ORIGIN=https://8myztggm5j-eng.github.io`.
6. Deploy. Wait until the server is **Live**. Open `https://YOUR-SERVICE.onrender.com/health` and verify it returns `{"status":"ok","requires_access_key":true}`.
7. Open `https://8myztggm5j-eng.github.io/clips4u-website/mobile.html`. Under YouTube Import, paste the Render **service URL** (no `/api/import`), enter the same private access key, paste a public YouTube video link, check the permission box, and choose Import.
8. The imported MP4 becomes a local video in the clipper; set times and export as usual. The access key is kept in this browser tab's session storage only.

## Security and operations

- **Do not publish the access key** in the HTML, GitHub repo, posts, or screenshots.
- The browser sends it to the backend in the `X-Clips4U-Key` header over HTTPS.
- CORS only allows the configured GitHub Pages origin, but the secret access key is the actual access control.
- The server is a private import tool for trusted users, not a public general-purpose video downloader.
- Render charges may apply. Imports are rate-limited to one at a time per server instance and limited by duration, download size and timeouts.
- Update yt-dlp regularly for compatibility; YouTube may still refuse requests.
