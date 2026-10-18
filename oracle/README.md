# Clips4U backend on Oracle Cloud Always Free (ARM64)

This moves ONLY the private video-analysis backend to Oracle. The iPhone editor remains on GitHub Pages. Render is left unchanged until you confirm the Oracle endpoint and a complete clip export work.

## Create the free Oracle server

1. Create an Oracle Cloud Infrastructure Free Tier account: https://www.oracle.com/cloud/free/
2. Open **Compute → Instances → Create instance**. Use your **home region**.
3. Choose **Ubuntu 24.04 aarch64/ARM64** (22.04 is fine too).
4. Change shape to **Ampere VM.Standard.A1.Flex (Always Free eligible)**. Use **2 OCPUs and 12 GB RAM**. Do not accidentally select a billable shape. Check the Always Free label.
5. Use an Always Free-eligible boot volume (normally 50 GB) and a **public subnet with an assigned public IPv4 address**.
6. Download the SSH private key in the browser and store it securely, or provide your own SSH public key. Never paste a private key or Oracle credentials into chat.
7. Add **stateful TCP 80 and TCP 443 ingress** on the instance's VCN Network Security Group or security list (source `0.0.0.0/0`). Keep SSH 22 restricted to your IP if possible; do not expose port 10000.
8. On Ubuntu, also allow ports 80/443 in the OS firewall if enabled.

Oracle's current Always Free ARM budget is **2 OCPUs and 12 GB** total across A1 instances (1500 OCPU-hours, 9000 GB-hours monthly). Provision only Always Free eligible resources. Availability can be limited; "Out of host capacity" means try a different availability domain or wait. Other paid features and excess resource usage can incur charges on paid accounts.

## Install Docker and deploy (on the Oracle VM)

Use the official Docker installation steps for Ubuntu:
https://docs.docker.com/engine/install/ubuntu/

They install `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, and `docker-compose-plugin`, all of which support Ubuntu ARM64.

Install git and openssl:

```bash
sudo apt-get update
sudo apt-get install -y git openssl
git clone https://github.com/8myztggm5j-eng/clips4u-website.git
cd clips4u-website
bash oracle/configure.sh YOUR_ORACLE_PUBLIC_IPV4
```

The script creates `oracle/.env` with a new random private API key and a free IP-based test hostname, e.g. `clips4u.203-0-113-10.sslip.io`, then builds your backend and starts Caddy reverse proxy on HTTPS. You do not need to buy a domain for this test. Free sslip.io DNS is a third-party service and may be less stable than a domain you control.

**Never commit `oracle/.env` or share its contents.** You will enter the newly generated key directly into your iPhone's Clips4U site. Find the key locally on the server with `cat oracle/.env`; keep it private.

Verify:

```bash
sudo docker compose --env-file oracle/.env -f oracle/compose.yaml ps
sudo docker compose --env-file oracle/.env -f oracle/compose.yaml logs --tail=40
curl -fsS https://YOUR_HOSTNAME/health
```

Expected health response: `{"status":"ok","requires_access_key":true}`.

In Clips4U (GitHub Pages) set **Backend URL (from Render)** to `https://YOUR_HOSTNAME` (despite its old label; it accepts any HTTPS backend). Enter the new secret key. Try a 29-second test first, then your seven-minute original. Direct YouTube imports can still be blocked by YouTube, independent of hosting.

## Maintain, update and rollback

Update Oracle to the latest GitHub version:

```bash
git pull --ff-only
sudo docker compose --env-file oracle/.env -f oracle/compose.yaml up -d --build
```

The Caddy service manages the HTTPS certificate automatically, provided ports 80/443 are open and DNS resolves. Its certificate storage persists in a Docker volume. Do not delete that volume.

**Keep Render online until Oracle successfully generates, previews, and exports a video.** To roll back quickly, just restore `https://clips4u-import.onrender.com` and your original Render access key in the iPhone editor.

To stop the Oracle services:

```bash
sudo docker compose --env-file oracle/.env -f oracle/compose.yaml down
```
