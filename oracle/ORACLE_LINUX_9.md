# Running Clips4U on Oracle Linux 9 (ARM64)

Oracle Linux 9 is supported for this deployment. You do **not** need to change the image to Ubuntu. The same `oracle/compose.yaml` and `oracle/configure.sh` deployment work after Docker Engine and Docker Compose are installed. The Oracle Cloud **VM.Standard.A1.Flex** shape is ARM64; ensure the selected image is Oracle Linux **aarch64**, with **2 OCPUs / 12 GB RAM** within Always Free allowances.

## Connect over SSH

For the official Oracle Linux image, the default SSH user is `opc` (Ubuntu images typically use `ubuntu`). Use the downloaded private key locally, never share it. You will need the VM's assigned **public IPv4 address**.

## Install Docker Engine, Docker Compose, Git, and OpenSSL

Run on the Oracle VM after connecting as `opc`:

```bash
sudo dnf update -y
sudo dnf install -y dnf-plugins-core git openssl
sudo dnf config-manager --add-repo https://download.docker.com/linux/oracle/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
```

The official Docker package repository publishes Oracle Linux 9 **aarch64** packages. Verify any Docker package-signing key prompt against Docker's official published fingerprint before accepting it. If dependency conflicts occur, stop and resolve them rather than deleting random system packages.

## Firewall and network rules

In Oracle Cloud VCN/security list or network security group, allow **inbound TCP 80 and 443** from the internet and **SSH TCP 22** only from your trusted IP if possible. Keep backend TCP **10000 private**, because Caddy proxies public HTTPS requests to it.

If firewalld is running on the VM:

```bash
if sudo systemctl is-active --quiet firewalld; then
  sudo firewall-cmd --permanent --add-service=http
  sudo firewall-cmd --permanent --add-service=https
  sudo firewall-cmd --reload
fi
```

Oracle images may also have extra iptables firewall rules: verify that the host firewall permits inbound TCP 80/443 without exposing the private API port. Avoid disabling the firewall wholesale.

## Start Clips4U

```bash
git clone https://github.com/8myztggm5j-eng/clips4u-website.git
cd clips4u-website
bash oracle/configure.sh YOUR_ORACLE_PUBLIC_IPV4
```

The script securely creates `oracle/.env`, starts the API and Caddy, and prints an HTTPS URL based on your public IP using sslip.io. Allow time for first Docker build and TLS certificate provisioning, then check:

```bash
sudo docker compose --env-file oracle/.env -f oracle/compose.yaml ps
sudo docker compose --env-file oracle/.env -f oracle/compose.yaml logs --tail=40
```

Do not commit/share `oracle/.env` or SSH private keys. Keep Render running until the Oracle service can process and download a test clip.

Sources: Docker's official Oracle Linux repository (https://download.docker.com/linux/oracle/), Docker Engine package installation guidance (https://docs.docker.com/engine/install/rhel/), and Oracle's container-hosting documentation (https://docs.oracle.com/en/operating-systems/oracle-linux/podman/install.html).
