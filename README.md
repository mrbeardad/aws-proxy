# CloudFront proxy

Deploy an Xray VLESS/WebSocket origin on an existing Ubuntu server, discover usable CloudFront edge IPs, and publish a Mihomo/Clash Verge Rev subscription through a separate CloudFront distribution backed by private S3.

AWS settings, origin details, edge selection and Clash behavior come from JSON files. Tokyo is an example, not a requirement. The origin can be Lightsail, EC2, or another publicly reachable Ubuntu host. AWS remains the hosting backend; this is not a multi-cloud provisioner.

## What runs

```text
Clash ── TLS + VLESS/WebSocket ── proxy CloudFront ── HTTPS ── Nginx ── Xray
  │
  └── HTTPS subscription / provider ── config CloudFront ── private S3
                                                               ▲
                              hourly publisher on origin ───────┘
```

- All discovered CloudFront nodes live in one proxy provider. No embedded bootstrap duplicates.
- The full profile stays unchanged when only edge IPs change. Both files are uploaded only when their bytes change.
- The default provider refreshes hourly. Set full-profile refresh to daily in Clash Verge Rev; that is a client setting.
- `AUTO` tests candidates from the client network. `PROXY` lets you select `AUTO`, an individual candidate, an optional direct node, or `DIRECT`.
- No candidate available means `AUTO` rejects connections rather than silently going direct. Use a Mihomo version supporting `empty-fallback`; legacy Clash is not supported.
- Xray is an upstream dependency downloaded as a pinned release with a required SHA256. This repo contains deployment and publisher source, not a copy of Xray-core.

## Files and configuration

| File | Purpose |
| --- | --- |
| `config/deployment.example.json` | AWS account/region, SSH target, origin certificate, discovery and client refresh settings |
| `config/clash.example.json` | Client port, IPv6, DNS, ordered Loyalsoldier rules, GEOIP and optional manual proxies |
| `deploy.py` | AWS resource creation, state journal, SSH deployment and verification |
| `server.py` | Ubuntu bootstrap, Xray/Nginx configuration and systemd units |
| `publisher.py` | Candidate discovery, TLS/health checks, provider rendering and change-only S3 uploads |

Copy examples to `config/deployment.json` and `config/clash.json`, then set `clash.base_file` to `clash.json`. This path is relative to the deployment config. Local files in `config/` and `.state/` are ignored by Git; only `*.example.json` is publishable by default.

`project` determines resource prefixes, `/opt/<project>` and systemd names. Use a different project and state directory for each deployment. Infrastructure settings are bound to the saved state; changing an account, server, or region requires a separate deployment. Discovery and Clash settings can be updated in place.

The Clash base accepts JSON, which is also valid YAML. Omit `proxy-groups` and `proxy-providers`: those are generated. The renderer preserves the other base settings and rule order, prepending one `DOMAIN,<config distribution>,DIRECT` rule for subscription access. It adds subscription DNS overrides from `subscription_dns`, and uses `AUTO` for rule downloads unless a rule provider explicitly sets another proxy. If you rename `PROXY`, update rule targets in the base too. Tests do not replace full Mihomo configuration validation.

The example uses port 7890, IPv6 disabled, mainland-oriented DNS and the agreed Loyalsoldier/GEOIP ordering. Replace those values for your own network. `gfw` and `tld-not-cn` are declared but unused, matching the original provider example; remove them if unwanted. TUN activation and UI overrides remain client settings.

You can add existing direct proxies to the base `proxies` list. This deployment installs only the CloudFront WebSocket service; it neither creates nor removes a direct REALITY service. Keep any direct node's credentials in the ignored local base file, and add its bypass rule yourself if needed.

## Prerequisites

1. An existing Ubuntu 22.04/24.04 host with a stable public IPv4 address and outbound internet access. Server creation, IP allocation and firewall changes are manual.
2. An origin DNS name resolving to that address and eligible for a public certificate. A domain you control is preferable; an IP-based DNS service is possible but adds that service's availability and certificate-policy constraints.
3. TCP 80 reachable for ACME validation/renewal; the configured HTTPS origin port (8443 by default) reachable by CloudFront. Keep the loopback WebSocket port private and SSH restricted to your administration source. This tool does not change firewalls.
4. SSH key authentication, passwordless sudo, and a verified host key already in `known_hosts`. Compare the fingerprint through a trusted console channel before accepting it. Configure custom ports or identity files in `~/.ssh/config`; `server.ssh_target` can be an alias. Do not disable host-key verification.
5. Linux/WSL or AWS CloudShell for deployment, Python 3.10+, OpenSSH, and AWS credentials for the intended account. Boto3 uses its standard credential chain, including `AWS_PROFILE`. Deployment needs the S3, CloudFront, IAM and STS operations used in `deploy.py`; the runtime publisher gets a separate, narrowly scoped IAM key.
6. Choose an exact [Xray release](https://github.com/XTLS/Xray-core/releases), matching Linux archive, and verified SHA256 from that release. Enter its tag, archive and hash in the config. Placeholder values deliberately prevent bootstrap.

Prefer a fresh host. Bootstrap installs Nginx, Certbot, curl, Python/boto3 and Xray. It refuses to overwrite an existing Xray binary. If those dependencies and the `xray` system user already exist, inspect their compatibility and skip bootstrap. Origin deployment owns files named after `project`; avoid collisions with existing Nginx virtual hosts, ports and services.

## Deploy

Run from the repository directory. These commands create billable AWS resources and accept the certificate authority's terms when obtaining the origin certificate. The tool does not manage AWS credits or enforce a spending cap.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config/deployment.example.json config/deployment.json
cp config/clash.example.json config/clash.json
# Edit both local files and set clash.base_file to clash.json.
python deploy.py check
python deploy.py bootstrap    # fresh host only
python deploy.py hosting      # private S3 + config CloudFront + OAC
python deploy.py origin       # certificate, Nginx/Xray + proxy CloudFront
python deploy.py status       # wait until both distributions show Deployed
python deploy.py publish      # installs publisher, runs it, prints your secret URL
python deploy.py verify       # services, S3 settings, CloudFront and cert renewal dry run
```

`check` is offline and does not install anything. `publish` refuses to proceed while either distribution is still deploying. With no verified candidate, it fails without overwriting previously published files. An initial failure means no subscription is ready yet; inspect the publisher logs and discovery settings, then rerun.

Import the printed URL as a remote profile in Clash Verge Rev. Set its refresh interval to one day and select `PROXY → AUTO`. Test Google, GitHub and your required services from your actual network before relying on the connection. The built-in server verification does not prove client reachability or successful VLESS traffic.

For another installation, use `--config config/another.json --state-dir .state/another` on every command. Keep state directories under `.state/` or outside the Git repository. Preserve an encrypted backup of the state; it contains resource IDs and credentials needed for recovery.

## Discovery and updates

The server combines OS DNS, Google DNS-over-HTTPS (including EDNS client-subnet hints derived from configured AWS regions), configured seed IPs and previously published IPs. It probes at most 64 public IPv4 addresses with four workers, validating TLS for the distribution hostname, the exact health response, and allowed `x-amz-cf-pop` prefixes. The default pool is up to eight candidates balanced across observed POPs.

`pop_prefixes` controls accepted locations; `hint_regions` controls DNS hints. They are independent of the origin/S3 region. `seed_ips`, `preferred_ips` and `excluded_ips` let you incorporate observations from the client network. No fixed production IPs are included in the example. Preferred IPs are only a tie-breaker within the selection strategy; POP diversity is retained.

This is best-effort discovery, not an enumeration of all AWS edges and not a measurement of Beijing routing. A server-side success cannot prove mainland reachability; CloudFront can also change where an IP is served. Client-side `AUTO` checks determine the usable choice. If discovery finds only disallowed POPs, add independently tested seed IPs or revise the allowed locations. Keep a last-known subscription/provider cache on the client.

After changing discovery, the Clash base or publisher source, run:

```bash
python deploy.py check
python deploy.py publish
```

Updates reuse the installed publisher key and restart the hourly publisher. The VLESS UUID, WebSocket path and subscription prefix remain stable; **hourly discovery does not rotate credentials**. CloudFront may cache changed files for five minutes, followed by the client's refresh interval. The server writes `status.json` with the last successful publication time and changed object names.

On the server (substitute your project name):

```bash
sudo systemctl status edge-proxy.service edge-proxy-publish.timer
sudo journalctl -u edge-proxy-publish.service -n 50 --no-pager
sudo cat /opt/edge-proxy/status.json
sudo systemctl start edge-proxy-publish.service
sudo python3 /opt/edge-proxy/publisher.py --root /opt/edge-proxy --discover-only
```

`--discover-only` makes network probes but performs no S3 writes. A failed run preserves the last published data; check for a stale last-success timestamp. The hourly job has a process lock and time limit. No monitoring service is created.

## Secrets, recovery and cleanup

The config bucket blocks public access. CloudFront OAC permits reads only through its config distribution. Runtime IAM credentials can put exactly two S3 object keys and cannot read or list the bucket. Credentials and generated server files use restricted permissions; Nginx access logging is disabled for the proxy origin.

The subscription URL contains a random 256-bit path. It is a bearer secret: anyone obtaining it can read the proxy credentials. It is hard to guess, but is not user authentication. Keep it out of issues, screenshots, public config files and logs. Never publish `.state/`, private keys, generated subscriptions or real local config files.

Deployments journal resource IDs after creation and normally resume with the same state. AWS creation and local journal writes are not one transaction: if interrupted between them, inspect AWS for the resource/key before retrying. Do not discard the state to work around an error. Rerunning `publish` uses the existing remote credential after a successful installation; a lost host/key requires explicit IAM key recovery or rotation. Rotation is intentionally manual.

Nginx/Xray configs are validated before service activation, but multi-file server updates are not transactional. Xray keeps `server.json.previous` for manual recovery. Review existing host configuration before deploying over a used server. S3 keeps previous object versions for seven days; restoring a version is manual. The publisher assumes its two objects have no other writer. After restoring/deleting an S3 object, remove the corresponding local acknowledgement file (`/opt/<project>/clash.yaml` or `nodes.yaml`) so the next successful run uploads it again.

Cleanup is deliberately explicit: stop/disable the project's publisher timer and origin service; deactivate/delete its IAM access key and inline policy/user; disable both distributions and wait for deployment before deleting them; remove the OAC when unused; empty **all versions and delete markers** in the dedicated S3 bucket and delete it. Remove only the project's Nginx/systemd/Certbot files and `/opt/<project>` data. Finally delete a dedicated VM and release its static IP if no longer needed. State records the exact IDs; do not use name wildcards to delete shared resources. Stopping a VM alone does not remove other chargeable resources.

## Test and publish the repository

```bash
python -m unittest discover -v
python deploy.py check --config config/deployment.example.json
git status --short
git diff --cached
```

Tests cover discovery failures, POP diversity, region overrides, static full configs, partial uploads, IAM scope, private S3, WebSocket forwarding and generated Bash syntax. They use mocks and do not create AWS resources. CI runs the same tests. They do not replace a fresh AWS deployment, Xray/Mihomo validation or client-network testing.

The repo has no GitHub remote configured. Review the staged files before adding your own remote and pushing. Choose a license before offering the code for third-party reuse. Upstream dependencies retain their own licenses.

Reference documentation: [CloudFront WebSockets](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/distribution-working-with.websockets.html), [Mihomo proxy providers](https://wiki.metacubex.one/en/config/proxy-providers/), [Mihomo URL-test groups](https://wiki.metacubex.one/en/config/proxy-groups/url-test/), [Loyalsoldier rules](https://github.com/Loyalsoldier/clash-rules).
